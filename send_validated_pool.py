#!/usr/bin/env python3
"""
send_validated_pool.py
======================
Send cold emails for addresses that are already marked allowlisted in
``zerobounce_validation`` (same rules as send_linkedin_campaigns_params.py)
but have **never** been recorded in ``email_attempts``.

This bypasses the LinkedIn company walk / resume-on-company logic: it
directly drains the validated pool.

On each successful SMTP send:
  • INSERT into ``email_attempts`` (status ``sent``)
  • UPDATE ``zerobounce_validation`` (pool_campaign_sent_at, pool_campaign_from_profile)
  • Optional: same progress JSON as the main sender (sent_emails) so both paths stay aligned

Default: ``--per-profile 5`` (same as main campaign script).

Usage:
  python send_validated_pool.py
  python send_validated_pool.py -n 10
  python send_validated_pool.py --no-progress-json
  python send_validated_pool.py --ignore-progress-json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import smtplib
import ssl
import sqlite3
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from itertools import zip_longest
from contextlib import contextmanager
from typing import Generator

# ── Paths (match send_linkedin_campaigns_params.py) ───────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_SCRIPT_DIR, "data", "db", "linkedin_data.db")
SMTP_CONFIG_FILE = r"C:\Users\sandeep\Downloads\Claudes\EmailJson\email_config.json"
TEMPLATE_FILE = os.path.join(_SCRIPT_DIR, "templates", "email_template_with_link.htm")
PROGRESS_FILE = os.path.join(_SCRIPT_DIR, "data", "json", "email_progress_linkedin.json")
EXCLUSION_FILE = os.path.join(_SCRIPT_DIR, "data", "csv", "exclusion_list.csv")
_LOG_DIR = os.path.join(_SCRIPT_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(
    _LOG_DIR,
    f"send_validated_pool_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log",
)

# Throttling (same as main sender)
EMAIL_SEND_DELAY = 5
EMAIL_BATCH_SIZE = 10
EMAIL_BATCH_BREAK = 30
PROFILE_SWITCH_DELAY = 5
SAME_DOMAIN_DELAY = 20

CUSTOM_VALID_STATUSES = {"ok", "valid"}
ZB_VALID_STATUSES = {"valid"}

ENABLE_PROGRESS = True

domain_last_sent: dict[str, float] = {}

# ── Logging ───────────────────────────────────────────────────────────────
def setup_logger() -> logging.Logger:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    log = logging.getLogger("validated_pool_sender")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.handlers.clear()
    log.addHandler(ch)
    log.addHandler(fh)
    return log


logger = setup_logger()


# ---------------------------------------------------------------------------
# WINDOWS SLEEP PREVENTION  —  keeps script running by preventing idle sleep
# ---------------------------------------------------------------------------
@contextmanager
def prevent_windows_sleep() -> Generator[None, None, None]:
    """
    Context manager to prevent Windows from entering sleep mode (system suspend)
    due to inactivity while the sender is running. The display is still
    allowed to turn off normally.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            logger.info("Setting Windows thread execution state to prevent sleep...")
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            yield
        except Exception as e:
            logger.warning("Could not set Windows execution state to prevent sleep: %s", e)
            yield
        finally:
            logger.info("Restoring Windows sleep behavior...")
            try:
                import ctypes
                ES_CONTINUOUS = 0x80000000
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception as e:
                logger.error("Failed to restore Windows sleep state: %s", e)
    else:
        yield


def clean_domain(raw: str) -> str:
    d = re.sub(r"^https?://(www\.)?", "", (raw or ""))
    return d.split("/")[0].strip().lower()


def load_exclusions(path: str) -> dict:
    """
    Load exclusion_list.csv.
    Returns dict with three sets: domains, emails, names.
    Any match on any of these will suppress the email.
    """
    exclusions = {"domains": set(), "emails": set(), "names": set()}
    if not os.path.exists(path):
        logger.info("  ℹ️  No exclusion file found at %s — skipping exclusion check.", path)
        return exclusions

    content = None
    for enc in ("utf-8-sig", "windows-1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                content = f.read()
            break
        except UnicodeDecodeError:
            continue

    if content is None:
        logger.warning("  ⚠️  Could not decode exclusion list file with any supported encoding.")
        return exclusions

    try:
        import io
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            if row.get("company_domain", "").strip():
                exclusions["domains"].add(row["company_domain"].strip().lower())
            if row.get("email_address", "").strip():
                exclusions["emails"].add(row["email_address"].strip().lower())
            if row.get("employee_name", "").strip():
                exclusions["names"].add(row["employee_name"].strip().lower())
        logger.info(
            "  📋 Exclusion list loaded: %s domain(s), %s email(s), %s name(s)",
            len(exclusions["domains"]),
            len(exclusions["emails"]),
            len(exclusions["names"]),
        )
    except Exception as e:
        logger.warning("  ⚠️  Could not load exclusion list: %s", e)
    return exclusions


def is_excluded(exclusions: dict, domain: str, email: str, name: str) -> str | None:
    """
    Returns a reason string if this contact should be excluded, else None.
    Checks domain, email address, and employee name (all case-insensitive).
    """
    if domain.lower() in exclusions["domains"]:
        return f"domain '{domain}' in exclusion list"
    if email.lower() in exclusions["emails"]:
        return f"email '{email}' in exclusion list"
    if name.lower() in exclusions["names"]:
        return f"name '{name}' in exclusion list"
    return None


def log_exclusion(conn: sqlite3.Connection, employee_name: str, company_name: str,
                  company_domain: str, email_address: str, reason: str) -> None:
    """Record a suppressed email in exclusion_log table."""
    if not conn:
        return
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exclusion_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_name     TEXT,
                company_name      TEXT,
                company_domain    TEXT,
                email_address     TEXT,
                exclusion_reason  TEXT,
                logged_at         TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO exclusion_log
                (employee_name, company_name, company_domain,
                 email_address, exclusion_reason, logged_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (employee_name, company_name, company_domain,
             email_address, reason, datetime.now().isoformat())
        )
        conn.commit()
    except Exception as e:
        logger.warning("Could not log exclusion: %s", e)


def validation_allows_send(zb: str | None, mv: str | None) -> tuple[bool, str]:
    mv_s = (mv or "").strip().lower()
    zb_s = (zb or "").strip().lower()
    ok_parts: list[str] = []
    bad_parts: list[str] = []
    if mv_s in CUSTOM_VALID_STATUSES:
        ok_parts.append(f"custom mv_status '{mv_s}'")
    elif mv_s:
        bad_parts.append(f"custom mv_status '{mv_s}'")
    if zb_s in ZB_VALID_STATUSES:
        ok_parts.append(f"ZeroBounce zb_status '{zb_s}'")
    elif zb_s:
        bad_parts.append(f"ZeroBounce zb_status '{zb_s}'")
    if ok_parts:
        return True, " + ".join(ok_parts)
    if bad_parts:
        return False, " + ".join(bad_parts)
    return False, "validation status is blank"


def check_domain_delay(domain: str) -> None:
    global domain_last_sent
    if domain in domain_last_sent:
        elapsed = time.time() - domain_last_sent[domain]
        if elapsed < SAME_DOMAIN_DELAY:
            wait = SAME_DOMAIN_DELAY - elapsed
            logger.info(f"  ⏸️  Domain cooldown: waiting {int(wait)}s for {domain}...")
            time.sleep(wait)
    domain_last_sent[domain] = time.time()


def get_emails_sent_today(conn: sqlite3.Connection, from_email: str) -> int:
    """Return count of emails sent from this profile today."""
    if not conn:
        return 0
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM email_attempts WHERE lower(from_profile) = ? AND sent_timestamp LIKE ?",
            (from_email.lower(), f"{today_prefix}%")
        )
        return cur.fetchone()[0]
    except Exception as e:
        logger.warning("Could not check today's sent count for %s: %s", from_email, e)
        return 0


def migrate_zerobounce_pool_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(zerobounce_validation)")
    cols = {r[1] for r in cur.fetchall()}
    if "pool_campaign_sent_at" not in cols:
        conn.execute(
            "ALTER TABLE zerobounce_validation ADD COLUMN pool_campaign_sent_at TEXT"
        )
    if "pool_campaign_from_profile" not in cols:
        conn.execute(
            "ALTER TABLE zerobounce_validation ADD COLUMN pool_campaign_from_profile TEXT"
        )
    conn.commit()


def ensure_email_attempts_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_attempts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name       TEXT,
            company_name        TEXT,
            company_domain      TEXT,
            email_address       TEXT    UNIQUE NOT NULL,
            email_format        TEXT,
            status              TEXT    DEFAULT 'sent',
            sent_timestamp      TEXT,
            bounce_detected_at  TEXT,
            from_profile        TEXT
        )
        """
    )
    conn.commit()


def load_progress() -> dict:
    if not ENABLE_PROGRESS or not os.path.exists(PROGRESS_FILE):
        return {"sent_emails": set(), "sent_companies": set(), "total_sent": 0}
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["sent_emails"] = {e.lower() for e in data.get("sent_emails", [])}
        data["sent_companies"] = {c.lower() for c in data.get("sent_companies", [])}
        if data.get("total_sent") is None:
            data["total_sent"] = len(data["sent_emails"])
        return data
    except Exception as e:
        logger.warning("Could not load progress: %s", e)
        return {"sent_emails": set(), "sent_companies": set(), "total_sent": 0}


def save_progress(progress: dict) -> None:
    if not ENABLE_PROGRESS:
        return
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "sent_emails": sorted(progress["sent_emails"]),
                    "sent_companies": sorted(progress["sent_companies"]),
                    "last_run": datetime.now().isoformat(),
                    "total_sent": progress["total_sent"],
                },
                f,
                indent=2,
            )
        logger.info("💾 Progress saved — total_sent=%s", progress["total_sent"])
    except Exception as e:
        logger.warning("Could not save progress: %s", e)


def load_active_profiles(path: str) -> tuple[list[dict], dict[str, str]]:
    if not os.path.exists(path):
        logger.error("❌ %s not found", path)
        return [], {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    smtp_passwords = data.get("profiles", {})
    profiles = [
        {"email": e, "name": e}
        for e, pw in smtp_passwords.items()
        if (e or "").strip() and (pw or "").strip()
    ]
    return profiles, smtp_passwords


def read_template(path: str, company: str, company_context: str, salutation: str) -> str | None:
    if not os.path.exists(path):
        logger.error("Template not found: %s", path)
        return None
    for enc in ("utf-8", "windows-1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                html = f.read()
            return (
                html.replace("{COMPANY}", company)
                .replace("{COMPANY_CONTEXT}", company_context)
                .replace("{NAME}", salutation)
            )
        except UnicodeDecodeError:
            continue
    return None


def generate_company_context(company_name: str) -> str:
    return (
        f"I follow {company_name}'s mission and recognise the operational "
        f"rigour needed to scale delivery & projects across new markets."
    )


def generate_subject(company_name: str) -> str:
    return (
        f"Delivery & Project Management Leader profile — "
        f"Interest in international scaling for {company_name}"
    )


def lookup_company_context(conn: sqlite3.Connection, email: str) -> tuple[str, str, str]:
    """
    Returns (company_name, company_domain, salutation) for template.
    Best-effort from companies / employees by domain.
    """
    if "@" not in email:
        return ("Your organisation", "", "there")
    local, _, domain = email.partition("@")
    domain = domain.strip().lower()
    row = conn.execute(
        """
        SELECT company_name, company_domain
        FROM companies
        WHERE lower(trim(company_domain)) = ?
        LIMIT 1
        """,
        (domain,),
    ).fetchone()
    if row:
        cname = row["company_name"] or domain
        erow = conn.execute(
            """
            SELECT employee_name FROM employees
            WHERE company_name = ?
            ORDER BY LENGTH(employee_name) ASC
            LIMIT 1
            """,
            (cname,),
        ).fetchone()
        sal = erow["employee_name"] if erow else local.replace(".", " ").replace("_", " ").title()
        return (cname, domain, sal)
    sal = local.replace(".", " ").replace("_", " ").title()
    return (domain or "Your organisation", domain, sal)


def fetch_pool_queue(
    conn: sqlite3.Connection, progress: dict, *, ignore_progress_file: bool = False
) -> list[dict]:
    """
    Rows in zerobounce_validation that pass allowlist rules and have no
    email_attempts row; also skip addresses already in main progress file.
    """
    exclusions = load_exclusions(EXCLUSION_FILE)

    cur = conn.execute(
        """
        SELECT email_address, zb_status, mv_status
        FROM zerobounce_validation
        ORDER BY lower(email_address)
        """
    )
    out: list[dict] = []
    sent_prog = progress.get("sent_emails") or set()
    for row in cur.fetchall():
        email = (row["email_address"] or "").strip()
        if not email or "@" not in email:
            continue
        ok, reason = validation_allows_send(row["zb_status"], row["mv_status"])
        if not ok:
            continue
        el = email.lower()
        hit = conn.execute(
            "SELECT 1 FROM email_attempts WHERE lower(email_address) = ? LIMIT 1",
            (el,),
        ).fetchone()
        if hit:
            continue
        if not ignore_progress_file and el in sent_prog:
            logger.debug("Skip (already in progress sent_emails): %s", email)
            continue
        cname, cdom, sal = lookup_company_context(conn, email)
        
        # Check if the contact or domain is excluded
        exclusion_reason = is_excluded(exclusions, cdom, email, sal)
        if exclusion_reason:
            logger.info("  🚫 %s — EXCLUDED (%s)", email, exclusion_reason)
            log_exclusion(conn, sal, cname, cdom, email, exclusion_reason)
            continue

        out.append(
            {
                "recipient": email,
                "domain": cdom or (email.split("@")[-1].lower() if "@" in email else ""),
                "company": cname,
                "salutation": sal,
                "company_context": generate_company_context(cname),
                "subject": generate_subject(cname),
            }
        )
    return out


def record_send(conn: sqlite3.Connection, email: str, domain: str, company: str, from_profile: str) -> None:
    ts = datetime.now().isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO email_attempts
            (employee_name, company_name, company_domain,
             email_address, email_format, status, sent_timestamp, from_profile)
        VALUES (?, ?, ?, ?, 'validated_pool', 'sent', ?, ?)
        """,
        ("Validated pool", company, domain, email, ts, from_profile),
    )
    conn.execute(
        """
        UPDATE zerobounce_validation
        SET pool_campaign_sent_at = ?,
            pool_campaign_from_profile = ?
        WHERE lower(email_address) = lower(?)
        """,
        (ts, from_profile, email),
    )
    conn.commit()


def send_one(
    smtp_password: str,
    from_email: str,
    email_data: dict,
    conn: sqlite3.Connection,
    progress: dict,
) -> bool:
    domain = email_data.get("domain") or ""
    check_domain_delay(domain)

    html = read_template(
        TEMPLATE_FILE,
        email_data["company"],
        email_data["company_context"],
        email_data["salutation"],
    )
    if not html:
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = email_data["subject"]
    msg["From"] = from_email
    msg["To"] = email_data["recipient"]
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(from_email, smtp_password)
            server.send_message(msg)
        logger.info("    ✓ SENT → %s", email_data["recipient"])
        record_send(
            conn,
            email_data["recipient"],
            email_data.get("domain", ""),
            email_data.get("company", ""),
            from_email,
        )
        if ENABLE_PROGRESS:
            progress["sent_emails"].add(email_data["recipient"].lower())
            progress["sent_companies"].add(email_data["company"].lower())
            progress["total_sent"] = int(progress.get("total_sent") or 0) + 1
            if progress["total_sent"] % 5 == 0:
                save_progress(progress)
        time.sleep(EMAIL_SEND_DELAY)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("    ✗ AUTH ERROR for %s — check App Password", from_email)
        return False
    except Exception as e:
        logger.error("    ✗ ERROR: %s", e)
        return False


def split_round_robin(emails: list, n_profiles: int) -> list:
    buckets = [[] for _ in range(n_profiles)]
    for i, email in enumerate(emails):
        buckets[i % n_profiles].append(email)
    return [b for b in buckets if b]


def print_db_summary_to_logger() -> None:
    try:
        import sqlite3
        import os
        if not os.path.exists(DB_PATH):
            return
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT * FROM v_validation_pipeline_summary").fetchone()
        if not row:
            conn.close()
            return
        names = [d[0] for d in conn.execute("SELECT * FROM v_validation_pipeline_summary").description]
        logger.info("")
        logger.info("=" * 70)
        logger.info("  DATABASE PIPELINE SUMMARY (v_validation_pipeline_summary)")
        logger.info("=" * 70)
        for name, val in zip(names, row):
            logger.info(f"  {name:<30}: {val:,}" if isinstance(val, int) else f"  {name:<30}: {val}")
        logger.info("=" * 70)
        conn.close()
    except Exception as e:
        logger.warning("Could not print database validation summary: %s", e)


def emit_summary(
    started: float,
    queued: int,
    success: int,
    failed: int,
    outcome: str,
    per_profile: int,
    n_profiles: int,
) -> None:
    elapsed = time.perf_counter() - started
    if elapsed < 60:
        es = f"{elapsed:.1f}s"
    else:
        m, s = divmod(int(elapsed), 60)
        es = f"{m}m {s}s" if m < 60 else f"{m // 60}h {m % 60}m {s}s"
    for line in (
        "",
        "=" * 70,
        "  RUN SUMMARY (validated pool sender)",
        "=" * 70,
        f"  Finished      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"  Elapsed       : {es}",
        f"  Outcome       : {outcome}",
        f"  Profiles × cap: {n_profiles} × {per_profile}",
        "-" * 70,
        f"  Queued        : {queued}",
        f"  Sent OK       : {success}",
        f"  Failed        : {failed}",
        "-" * 70,
        f"  Log file      : {os.path.abspath(LOG_FILE)}",
        f"  Progress file : {os.path.abspath(PROGRESS_FILE)}",
        "=" * 70,
    ):
        logger.info(line)
    
    print_db_summary_to_logger()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send to allowlisted zerobounce_validation rows not in email_attempts."
    )
    parser.add_argument(
        "--per-profile",
        "-n",
        type=int,
        default=5,
        help="Max sends per Gmail profile this run (default: 5).",
    )
    parser.add_argument(
        "--no-progress-json",
        action="store_true",
        help="Do not read/write email_progress_linkedin.json (DB only).",
    )
    parser.add_argument(
        "--ignore-progress-json",
        action="store_true",
        help="Do not skip recipients that appear in progress JSON but have no "
        "email_attempts row (use to fix drift; risk of duplicate if they were "
        "actually sent).",
    )
    args = parser.parse_args()
    global ENABLE_PROGRESS
    if args.no_progress_json:
        ENABLE_PROGRESS = False

    t0 = time.perf_counter()
    logger.info("=" * 70)
    logger.info("  VALIDATED POOL SENDER")
    logger.info("  Run started: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("  Log file   : %s", os.path.abspath(LOG_FILE))
    logger.info("=" * 70)

    if not os.path.exists(DB_PATH):
        logger.error("Database not found: %s", DB_PATH)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    migrate_zerobounce_pool_columns(conn)
    ensure_email_attempts_table(conn)

    progress = load_progress()
    profiles, passwords = load_active_profiles(SMTP_CONFIG_FILE)
    if not profiles:
        logger.error("No SMTP profiles in %s", SMTP_CONFIG_FILE)
        conn.close()
        sys.exit(1)

    # ── Check daily send limits (max 30 per profile) ─────────────────────
    logger.info("\n  Checking daily send limits (max 30 per profile):")
    remaining_profiles = []
    for p in profiles:
        sent_today = get_emails_sent_today(conn, p["email"])
        p["sent_today"] = sent_today
        status_str = f"{sent_today}/30 sent today"
        if sent_today >= 30:
            logger.info("    Profile %s: %s 🛑 (LIMIT REACHED - skipping profile for this run)", p["email"], status_str)
        else:
            logger.info("    Profile %s: %s ✅ (%s remaining)", p["email"], status_str, 30 - sent_today)
            remaining_profiles.append(p)
    profiles = remaining_profiles

    if not profiles:
        logger.error("❌ No profiles available to send (all reached daily limit of 30).")
        conn.close()
        sys.exit(0)

    queue = fetch_pool_queue(
        conn, progress, ignore_progress_file=args.ignore_progress_json
    )
    max_total = args.per_profile * len(profiles)
    queue = queue[:max_total]

    logger.info("\n  Pool queue (after caps): %s message(s) (max %s)", len(queue), max_total)
    if not queue:
        emit_summary(t0, 0, 0, 0, "no_queue (no valid unsent addresses)", args.per_profile, len(profiles))
        conn.close()
        sys.exit(0)

    batches = split_round_robin(queue, len(profiles))
    interleaved: list[tuple[int, dict]] = []
    for rnd in zip_longest(*batches):
        for prof_idx, em in enumerate(rnd):
            if em is not None:
                interleaved.append((prof_idx, em))

    ok = fail = 0
    prev = None
    interrupted = False

    try:
        for i, (prof_idx, email_data) in enumerate(interleaved, 1):
            prof = profiles[prof_idx]
            pw = passwords[prof["email"]]

            # Enforce hard daily limit of 30 sends
            if prof.get("sent_today", 0) >= 30:
                logger.warning("\n  ⚠️ Profile %s has reached the daily limit of 30 sends (sent today: %s). Skipping email to %s.", prof["email"], prof["sent_today"], email_data["recipient"])
                continue

            if prev is not None and prof_idx != prev:
                logger.info(
                    "\n  🔄 Profile %s — waiting %ss...",
                    prof_idx + 1,
                    PROFILE_SWITCH_DELAY,
                )
                time.sleep(PROFILE_SWITCH_DELAY)
            logger.info("\n  [%s/%s] %s → %s", i, len(interleaved), prof["email"], email_data["recipient"])
            if send_one(pw, prof["email"], email_data, conn, progress):
                ok += 1
                prof["sent_today"] = prof.get("sent_today", 0) + 1
                if ok % EMAIL_BATCH_SIZE == 0:
                    logger.info("\n  🛑 Batch break — %ss", EMAIL_BATCH_BREAK)
                    time.sleep(EMAIL_BATCH_BREAK)
            else:
                fail += 1
            
            pct = (i / len(interleaved)) * 100
            logger.info("    Progress: %d/%d (%.1f%%)", i, len(interleaved), pct)
            prev = prof_idx
    except KeyboardInterrupt:
        interrupted = True
        logger.info("\n  Interrupted.")
    finally:
        save_progress(progress)
        conn.close()
        outcome = "interrupted" if interrupted else "completed"
        emit_summary(t0, len(interleaved), ok, fail, outcome, args.per_profile, len(profiles))

    if interrupted:
        raise KeyboardInterrupt


if __name__ == "__main__":
    try:
        with prevent_windows_sleep():
            main()
    except KeyboardInterrupt:
        logger.info("\nStopped.")
        sys.exit(130)
    except SystemExit as e:
        raise e
    except Exception as e:
        logger.exception("Fatal: %s", e)
        sys.exit(1)
