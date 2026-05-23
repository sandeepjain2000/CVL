# =============================================
# send_linkedin_campaigns.py
# Cold Email Sender — reads from linkedin_data.db
#
# Replaces send_multi_profile_validated.py for the new
# LinkedIn scraper database.
#
# DB: linkedin_data.db  (same folder as this script)
#   companies  — company_name, company_domain, country,
#                company_type, headquarters, industry
#   employees  — employee_name, job_title,
#                company_linkedin_url, company_name
#
# Config files (same as before — no changes needed):
#   credentials_FINAL.json   — profile list
#   email_config.json        — Gmail App Passwords
#   email_template_with_link.htm — HTML template
#
# Progress file:
#   email_progress_linkedin.json  — separate from old progress
#                                   so old campaign history is preserved
# =============================================

import smtplib
import ssl
import sqlite3
import json
import time
import os
import re
import sys
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from itertools import zip_longest
import argparse


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def emit_run_summary(
    *,
    run_started_perf: float,
    queued: int,
    success: int,
    failed: int,
    progress: dict,
    outcome: str,
    country_filter: str | None,
    per_profile: int,
    n_profiles: int,
) -> None:
    """
    One clear block at end of run (console + log) so you can see sent/failed
    without scrolling. outcome: completed | interrupted | no_queue
    """
    elapsed = _format_elapsed(time.perf_counter() - run_started_perf)
    unique = len(progress.get("sent_emails") or [])
    total_field = progress.get("total_sent")
    if total_field is None:
        total_field = unique
    lines = [
        "",
        "=" * 70,
        "  RUN SUMMARY",
        "=" * 70,
        f"  Finished            : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"  Elapsed             : {elapsed}",
        f"  Outcome             : {outcome}",
        f"  Country filter      : {country_filter or 'All'}",
        f"  Profiles × cap      : {n_profiles} × {per_profile} (max {n_profiles * per_profile} sends)",
        "-" * 70,
        f"  Queued this run     : {queued}",
        f"  Sent successfully   : {success}",
        f"  Failed (send error) : {failed}",
        "-" * 70,
        f"  Unique in progress  : {unique} recipient(s)",
        f"  Progress file         : {os.path.abspath(PROGRESS_FILE)}",
        f"  total_sent counter  : {total_field}",
        f"  Log file            : {os.path.abspath(LOG_FILE)}",
        "  (To reset resume state, delete or edit the progress JSON.)",
        "=" * 70,
    ]
    for line in lines:
        logger.info(line)


# =============================================
# CONFIGURATION — edit these as needed
# =============================================

_SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
DB_PATH           = os.path.join(_SCRIPT_DIR, "data", "db", "linkedin_data.db")
SMTP_CONFIG_FILE  = r"C:\Users\sandeep\Downloads\Claudes\EmailJson\email_config.json"
TEMPLATE_FILE     = os.path.join(_SCRIPT_DIR, "templates", "email_template_with_link.htm")
PROGRESS_FILE     = os.path.join(_SCRIPT_DIR, "data", "json", "email_progress_linkedin.json")
EXCLUSION_FILE    = os.path.join(_SCRIPT_DIR, "data", "csv", "exclusion_list.csv")

# Each run gets its own timestamped log file inside a logs/ subfolder
_LOG_DIR  = os.path.join(_SCRIPT_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
LOG_FILE  = os.path.join(
    _LOG_DIR,
    f"send_linkedin_params_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
)

MAX_EMAILS        = 200   # cap per run
ENABLE_RESUME     = True  # skip already-sent emails

# Throttling (same values as old script)
EMAIL_SEND_DELAY        = 5    # seconds between emails from same profile
EMAIL_BATCH_SIZE        = 10   # break after this many sends
EMAIL_BATCH_BREAK       = 30   # seconds for the break
PROFILE_SWITCH_DELAY    = 5    # seconds between profile switches
SAME_DOMAIN_DELAY       = 20   # seconds before re-emailing same domain

# Job title keywords that indicate a senior/decision-maker contact
# Employees matching these titles are preferred over generic info@
EXEC_TITLE_KEYWORDS = [
    "ceo", "chief executive", "managing director", "geschäftsführer",
    "founder", "president", "owner", "director", "head of", "vp ",
    "vice president", "partner", "principal",
]

domain_last_sent: dict = {}

# =============================================
# LOGGING SETUP
# =============================================

def setup_logger() -> logging.Logger:
    """Configure logger to write to both console and log file."""
    logger = logging.getLogger("linkedin_sender")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    # File handler — write mode so each run gets a clean file
    fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger

logger = setup_logger()

# =============================================
# DATABASE HELPERS
# =============================================

def open_db(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        logger.error("❌  Database not found: {db_path}")
        logger.info(f"    Make sure {db_path} is in the same folder as this script.")
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# EMPLOYEE NAME CLEANER (handles dirty records from old scraper runs)
# ---------------------------------------------------------------------------
import re as _re
_DEGREE_RE_S = _re.compile(r'^[🔹·\s�]*(1st|2nd|3rd)', _re.I)
_CONN_RE_S   = _re.compile(r'degree connection', _re.I)
_FOLLOW_RE_S = _re.compile(r'^[\d.,]+\s*[KkMm]?\s*followers', _re.I)

def _clean_employee(raw_name: str, raw_title: str) -> tuple:
    """Extract real name and title from mangled LinkedIn DOM scrape output."""
    is_badge = bool(_DEGREE_RE_S.match((raw_name or "").strip()))
    blob_lines = [l.strip() for l in (raw_title or "").split("\n") if l.strip()]
    if is_badge:
        name, title, after = "", "", False
        for line in blob_lines:
            if not name:
                if not _DEGREE_RE_S.match(line):
                    name = line
            elif _CONN_RE_S.search(line) or _DEGREE_RE_S.match(line):
                after = True
            elif after:
                if not _FOLLOW_RE_S.match(line):
                    title = line
                    break
        return name, title
    else:
        name, title, after = (raw_name or "").strip(), "", False
        for line in blob_lines:
            if _CONN_RE_S.search(line) or _DEGREE_RE_S.match(line):
                after = True
            elif after:
                if not _FOLLOW_RE_S.match(line):
                    title = line
                    break
        return name, title or (blob_lines[0] if blob_lines else "")


def load_companies(conn: sqlite3.Connection,
                   country_filter: str = None) -> list:
    """
    Load companies from linkedin_data.db.
    Joins with employees to attach up to 2 senior contacts per company.
    Returns list of dicts ready for email generation.
    """
    where = "WHERE (c.company_domain IS NOT NULL AND c.company_domain != '')"
    params = []
    if country_filter:
        where += " AND c.country = ?"
        params.append(country_filter)

    # All companies with a domain
    cur = conn.execute(f"""
        SELECT c.company_name,
               c.company_domain,
               c.country,
               c.company_type,
               c.headquarters,
               c.industry,
               c.linkedin_url
        FROM   companies c
        {where}
        ORDER  BY c.company_name
    """, params)

    companies = []
    for row in cur.fetchall():
        company = dict(row)

        # Fetch employees — exclude LinkedIn UI artefacts like "· 2nd", "· 3rd"
        emp_cur = conn.execute("""
            SELECT employee_name, job_title
            FROM   employees
            WHERE  (company_name = ?
               OR   company_linkedin_url = ?)
              AND   employee_name IS NOT NULL
              AND   length(trim(employee_name)) > 3
              AND   employee_name NOT LIKE '%·%'
              AND   employee_name NOT LIKE '% 2nd%'
              AND   employee_name NOT LIKE '% 3rd%'
              AND   instr(trim(employee_name), ' ') > 0
            ORDER  BY
                CASE WHEN lower(job_title) LIKE '%ceo%'
                          OR lower(job_title) LIKE '%chief%'
                          OR lower(job_title) LIKE '%managing director%'
                          OR lower(job_title) LIKE '%geschäftsführer%'
                          OR lower(job_title) LIKE '%founder%'
                          OR lower(job_title) LIKE '%owner%'
                          OR lower(job_title) LIKE '%director%'
                     THEN 0 ELSE 1 END,
                employee_name
            LIMIT 5
        """, (company["company_name"], company.get("linkedin_url", "")))

        employees = []
        for emp in emp_cur.fetchall():
            # Clean up mangled names from old scraper runs
            clean_name, clean_title = _clean_employee(
                emp["employee_name"] or "", emp["job_title"] or ""
            )
            name_parts = clean_name.strip().split()
            if len(name_parts) >= 2:
                employees.append({
                    "first_name": name_parts[0],
                    "last_name":  " ".join(name_parts[1:]),
                    "full_name":  clean_name,
                    "job_title":  clean_title,
                })

        company["employees"] = employees
        companies.append(company)

    return companies


def get_available_countries(conn: sqlite3.Connection) -> list:
    cur = conn.execute("""
        SELECT DISTINCT country, COUNT(*) as cnt
        FROM   companies
        WHERE  country IS NOT NULL AND country != ''
        GROUP  BY country
        ORDER  BY cnt DESC
    """)
    return [(r["country"], r["cnt"]) for r in cur.fetchall()]


def db_stats(conn: sqlite3.Connection) -> dict:
    companies = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    with_domain = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE company_domain IS NOT NULL AND company_domain != ''"
    ).fetchone()[0]
    employees = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    return {"companies": companies, "with_domain": with_domain, "employees": employees}

# =============================================
# PROGRESS TRACKING
# =============================================

def load_progress() -> dict:
    if not ENABLE_RESUME or not os.path.exists(PROGRESS_FILE):
        return {"sent_emails": set(), "sent_companies": set(),
                "last_run": None, "total_sent": 0}
    try:
        with open(PROGRESS_FILE, "r") as f:
            data = json.load(f)
        data["sent_emails"]    = set(data.get("sent_emails", []))
        data["sent_companies"] = set(data.get("sent_companies", []))
        if data.get("total_sent") is None:
            data["total_sent"] = len(data["sent_emails"])
        return data
    except Exception as e:
        logger.info(f"⚠️  Could not load progress file: {e}")
        return {"sent_emails": set(), "sent_companies": set(),
                "last_run": None, "total_sent": 0}


def save_progress(progress: dict):
    if not ENABLE_RESUME:
        return
    try:
        data = {
            "sent_emails":    list(progress["sent_emails"]),
            "sent_companies": list(progress["sent_companies"]),
            "last_run":       datetime.now().isoformat(),
            "total_sent":     progress["total_sent"],
        }
        with open(PROGRESS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"💾 Progress saved — {progress['total_sent']} emails sent total")
    except Exception as e:
        logger.info(f"⚠️  Could not save progress: {e}")


def update_progress(progress: dict, email: str, company: str):
    progress["sent_emails"].add(email.lower())
    progress["sent_companies"].add(company.lower())
    progress["total_sent"] += 1
    if progress["total_sent"] % 5 == 0:
        save_progress(progress)


def already_sent(progress: dict, email: str, company: str) -> bool:
    return (email.lower()   in progress["sent_emails"] or
            company.lower() in progress["sent_companies"])

# =============================================
# EMAIL ATTEMPTS DB
# =============================================

EMAIL_FORMATS = [
    "firstname.lastname",       # john.smith@domain
    "firstname",                # john@domain
    "firstinitial.lastname",    # j.smith@domain
    "firstname.lastinitial",    # john.s@domain
]

# Queue order when several formats are valid for the same employee (all are sent).
FORMAT_SELECTION_PRIORITY = [
    "firstname",
    "firstinitial.lastname",
    "firstname.lastinitial",
    "firstname.lastname",
]

CUSTOM_VALID_STATUSES = {"ok", "valid"}
ZB_VALID_STATUSES = {"valid"}

def get_db_conn():
    """Open linkedin_data.db in same folder as this script."""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "db", "linkedin_data.db")
    if not os.path.exists(db_path):
        return None
    import sqlite3 as _sq
    conn = _sq.connect(db_path)
    conn.row_factory = _sq.Row
    # Create table if not yet migrated
    conn.execute("""
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
    """)
    conn.commit()
    return conn


def log_email_attempt(conn, employee_name: str, company_name: str,
                      company_domain: str, email_address: str,
                      email_format: str, from_profile: str):
    """Record a sent email in email_attempts."""
    if not conn:
        return
    try:
        conn.execute("""
            INSERT OR IGNORE INTO email_attempts
                (employee_name, company_name, company_domain,
                 email_address, email_format, status,
                 sent_timestamp, from_profile)
            VALUES (?, ?, ?, ?, ?, 'sent', ?, ?)
        """, (employee_name, company_name, company_domain,
              email_address, email_format,
              datetime.now().isoformat(), from_profile))
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not log email attempt: {e}")


def get_bounced_formats(conn, company_domain: str, employee_name: str) -> set:
    """Return set of email formats already bounced for this person/domain."""
    if not conn:
        return set()
    try:
        cur = conn.execute("""
            SELECT email_format FROM email_attempts
            WHERE company_domain = ?
              AND employee_name  = ?
              AND status = 'bounced'
        """, (company_domain, employee_name))
        return {row["email_format"] for row in cur.fetchall()}
    except Exception:
        return set()


def already_attempted(conn, email_address: str) -> bool:
    """Return True if this exact email address was already sent."""
    if not conn:
        return False
    try:
        cur = conn.execute(
            "SELECT 1 FROM email_attempts WHERE email_address = ?", (email_address,)
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def get_email_validation(conn, email_address: str) -> dict | None:
    """Return stored validation metadata for an email address, if it exists."""
    if not conn:
        return None
    try:
        cur = conn.execute("""
            SELECT zb_status,
                   mv_status,
                   mv_quality,
                   mv_resultcode,
                   mv_subresult
            FROM   zerobounce_validation
            WHERE  lower(email_address) = ?
            LIMIT  1
        """, (email_address.lower(),))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.warning(f"Could not read validation result for {email_address}: {e}")
        return None


def validation_allows_send(conn, email_address: str) -> tuple[bool, str]:
    """
    Only allow sends to addresses explicitly marked valid by verification.

    The validation data is a partial allowlist. Existing ZeroBounce valid results
    and custom valid results can both allow sending. Unvalidated, catch-all,
    invalid, and unknown addresses are skipped without attempt/progress writes.
    """
    validation = get_email_validation(conn, email_address)
    if not validation:
        return False, "no validation result"

    mv_status = (validation.get("mv_status") or "").strip().lower()
    zb_status = (validation.get("zb_status") or "").strip().lower()

    valid_reasons = []
    invalid_reasons = []

    if mv_status in CUSTOM_VALID_STATUSES:
        valid_reasons.append(f"custom validation status '{mv_status}'")
    elif mv_status:
        invalid_reasons.append(f"custom validation status '{mv_status}'")

    if zb_status in ZB_VALID_STATUSES:
        valid_reasons.append(f"ZeroBounce status '{zb_status}'")
    elif zb_status:
        invalid_reasons.append(f"ZeroBounce status '{zb_status}'")

    if valid_reasons:
        return True, " + ".join(valid_reasons)
    if invalid_reasons:
        return False, " + ".join(invalid_reasons)
    return False, "validation status is blank"


def _to_ascii(s: str) -> str:
    """Strip diacritics and transliterate to ASCII (ö→o, ü→u, ä→a, etc.)"""
    import unicodedata
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")


# =============================================
# EXCLUSION LIST
# =============================================

def load_exclusions(path: str) -> dict:
    """
    Load exclusion_list.csv.
    Returns dict with three sets: domains, emails, names.
    Any match on any of these will suppress the email.
    """
    import csv
    exclusions = {"domains": set(), "emails": set(), "names": set()}
    if not os.path.exists(path):
        logger.info(f"  ℹ️  No exclusion file found at {path} — skipping exclusion check.")
        return exclusions
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("company_domain", "").strip():
                    exclusions["domains"].add(row["company_domain"].strip().lower())
                if row.get("email_address", "").strip():
                    exclusions["emails"].add(row["email_address"].strip().lower())
                if row.get("employee_name", "").strip():
                    exclusions["names"].add(row["employee_name"].strip().lower())
        logger.info(
            f"  📋 Exclusion list loaded: "
            f"{len(exclusions['domains'])} domain(s), "
            f"{len(exclusions['emails'])} email(s), "
            f"{len(exclusions['names'])} name(s)"
        )
    except Exception as e:
        logger.warning(f"  ⚠️  Could not load exclusion list: {e}")
    return exclusions


def is_excluded(exclusions: dict, domain: str, email: str, name: str):
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


def log_exclusion(conn, employee_name: str, company_name: str,
                  company_domain: str, email_address: str, reason: str):
    """Record a suppressed email in exclusion_log table."""
    if not conn:
        return
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS exclusion_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_name     TEXT,
                company_name      TEXT,
                company_domain    TEXT,
                email_address     TEXT,
                exclusion_reason  TEXT,
                logged_at         TEXT
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO exclusion_log
                (employee_name, company_name, company_domain,
                 email_address, exclusion_reason, logged_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (employee_name, company_name, company_domain,
              email_address, reason, datetime.now().isoformat()))
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not log exclusion: {e}")


def sort_validated_formats(
    valid_options: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """
    Return all (format, email, validation_reason) that passed validation_allows_send,
    ordered by FORMAT_SELECTION_PRIORITY for consistent send order.
    """
    rank = {f: i for i, f in enumerate(FORMAT_SELECTION_PRIORITY)}
    return sorted(valid_options, key=lambda x: rank.get(x[0], 99))


def collect_valid_format_options(
    emp: dict,
    company_name: str,
    domain: str,
    bounced: set,
    exclusions: dict,
    progress: dict,
    db_conn,
    *,
    log_decisions: bool = True,
) -> list[tuple[str, str, str]]:
    """All format variants for *emp* that pass gates and validation_allows_send."""
    first = emp["first_name"]
    last = emp["last_name"].split()[0] if emp["last_name"] else ""
    if not first.isalpha() or not last.isalpha():
        if log_decisions:
            logger.warning(f"    ⚠️  Skipping bad name: {emp['full_name']!r}")
        return []

    name_reason = is_excluded(exclusions, "", "", emp["full_name"])
    if name_reason:
        if log_decisions:
            candidate = build_email_address(first, last, domain, EMAIL_FORMATS[0])
            logger.info(f"    🚫 {emp['full_name']} — EXCLUDED ({name_reason})")
            log_exclusion(
                db_conn, emp["full_name"], company_name, domain, candidate, name_reason
            )
        return []

    valid_options: list[tuple[str, str, str]] = []
    for fmt in EMAIL_FORMATS:
        if fmt in bounced:
            if log_decisions:
                logger.info(f"    ↩️  [{fmt}] bounced — skipping")
            continue
        candidate = build_email_address(first, last, domain, fmt)

        email_reason = is_excluded(exclusions, "", candidate, "")
        if email_reason:
            if log_decisions:
                logger.info(f"    🚫 [{fmt}] {candidate} — EXCLUDED ({email_reason})")
                log_exclusion(
                    db_conn, emp["full_name"], company_name, domain, candidate, email_reason
                )
            continue

        if already_attempted(db_conn, candidate):
            if log_decisions:
                logger.info(f"    ⏭️  [{fmt}] {candidate} already attempted")
            continue
        if candidate.lower() in progress["sent_emails"]:
            if log_decisions:
                logger.info(f"    ⏭️  [{fmt}] {candidate} already in progress file")
            continue

        can_send, validation_reason = validation_allows_send(db_conn, candidate)
        if can_send:
            valid_options.append((fmt, candidate, validation_reason))
            if log_decisions:
                logger.info(
                    f"    ✓ [{fmt}] {candidate} — valid for sending ({validation_reason})"
                )
        elif log_decisions:
            logger.info(
                f"    ⏭️  [{fmt}] {candidate} not valid for sending ({validation_reason})"
            )

    return valid_options


def company_has_pending_validated_staff(
    company: dict,
    progress: dict,
    db_conn,
    exclusions: dict,
) -> bool:
    """True if any employee still has a validated address not yet sent."""
    domain = clean_domain(company.get("company_domain") or "")
    if not domain:
        return False
    name = company["company_name"]
    for emp in company["employees"]:
        bounced = get_bounced_formats(db_conn, domain, emp["full_name"])
        if collect_valid_format_options(
            emp, name, domain, bounced, exclusions, progress, db_conn, log_decisions=False
        ):
            return True
    return False


def build_email_address(first: str, last: str, domain: str, fmt: str) -> str:
    """Generate email address for a given format."""
    f = _to_ascii(first.lower().strip())
    l = _to_ascii(last.lower().strip().split()[0])  # first word of last name only
    if fmt == "firstname.lastname":
        return f"{f}.{l}@{domain}"
    elif fmt == "firstname":
        return f"{f}@{domain}"
    elif fmt == "firstinitial.lastname":
        return f"{f[0]}.{l}@{domain}"
    elif fmt == "firstname.lastinitial":
        return f"{f}.{l[0]}@{domain}"
    return f"{f}.{l}@{domain}"


# =============================================
# DOMAIN HELPERS
# =============================================

def clean_domain(raw_domain: str) -> str:
    """Strip protocol/www and path from a domain string."""
    d = re.sub(r'^https?://(www\.)?', '', (raw_domain or ""))
    return d.split('/')[0].strip()


def domain_resolves(domain: str, timeout: int = 3) -> bool:
    import socket
    try:
        socket.setdefaulttimeout(timeout)
        socket.gethostbyname(domain)
        return True
    except Exception:
        return False


def check_domain_delay(domain: str):
    global domain_last_sent
    if domain in domain_last_sent:
        elapsed = time.time() - domain_last_sent[domain]
        if elapsed < SAME_DOMAIN_DELAY:
            wait = SAME_DOMAIN_DELAY - elapsed
            logger.info(f"  ⏸️  Domain cooldown: waiting {int(wait)}s for {domain}...")
            time.sleep(wait)
    domain_last_sent[domain] = time.time()

# =============================================
# EMAIL GENERATION
# =============================================

def generate_subject(company_name: str) -> str:
    return (f"Delivery & Project Management Leader profile — "
            f"Interest in international scaling for {company_name}")


def generate_company_context(company_name: str) -> str:
    return (f"I follow {company_name}'s mission and recognise the operational "
            f"rigour needed to scale delivery & projects across new markets.")


def log_no_queue_diagnostics(
    companies: list,
    progress: dict,
    db_conn,
    exclusions: dict,
) -> None:
    """
    When build_email_list returns empty, explain why 148 ZB-valid rows != sends.
    The sender never drains zerobounce_validation directly; it walks companies
    and only queues candidates that pass validation AND resume/bounce gates.
    """
    sent_emails = progress.get("sent_emails") or set()
    sent_companies = progress.get("sent_companies") or set()

    n_no_domain = 0
    n_excluded_domain = 0
    n_skip_contacted_no_bounce = 0
    n_bounce_retry = 0
    n_bad_dns = 0
    n_reached_staff_scan = 0

    for company in companies:
        name = company["company_name"]
        domain = clean_domain(company.get("company_domain") or "")
        if not domain:
            n_no_domain += 1
            continue
        if is_excluded(exclusions, domain, "", ""):
            n_excluded_domain += 1
            continue
        if already_sent(progress, "", name):
            has_bounced = False
            if db_conn:
                cur = db_conn.execute(
                    "SELECT 1 FROM email_attempts WHERE company_domain = ? AND status = 'bounced' LIMIT 1",
                    (domain,),
                )
                has_bounced = cur.fetchone() is not None
            if not has_bounced:
                n_skip_contacted_no_bounce += 1
                continue
            n_bounce_retry += 1
        if not domain_resolves(domain):
            n_bad_dns += 1
            continue
        n_reached_staff_scan += 1

    logger.info("\n" + "=" * 70)
    logger.info("  WHY NOTHING WAS QUEUED (read this)")
    logger.info("=" * 70)
    logger.info(
        "  • The 148 'valid' rows are in zerobounce_validation — that is a VERIFICATION"
    )
    logger.info(
        "    pool, not a to-do list. The script does not send 'the next unused valid row'."
    )
    logger.info(
        "  • It walks your LinkedIn DB (companies → employees → guessed formats), then"
    )
    logger.info(
        "    only queues an address if that exact address is allowlisted in validation"
    )
    logger.info(
        "    AND not blocked by resume / already-attempted / bounce rules."
    )
    logger.info(
        "  • Your progress file marks companies as contacted. Those companies are"
    )
    logger.info(
        "    skipped entirely unless the DOMAIN has at least one 'bounced' attempt"
    )
    logger.info(
        "    in email_attempts — so after ~5268 sends, almost everyone is skipped."
    )
    logger.info("-" * 70)
    logger.info(
        f"  Companies in this run's list     : {len(companies)}"
    )
    logger.info(
        f"  Skipped (already contacted, no domain bounce) : {n_skip_contacted_no_bounce}"
    )
    logger.info(
        f"  Not skipped for that reason (bounce-retry or never contacted) : "
        f"{len(companies) - n_skip_contacted_no_bounce}"
    )
    logger.info(
        f"  …of which: no domain={n_no_domain}, excluded domain={n_excluded_domain}, "
        f"DNS fail={n_bad_dns}, reached staff scan≈{n_reached_staff_scan}"
    )
    logger.info(
        f"  Companies in bounce-retry mode : {n_bounce_retry}"
    )
    logger.info(
        f"  Unique recipients in progress    : {len(sent_emails)}"
    )
    logger.info(
        f"  Companies marked in progress     : {len(sent_companies)}"
    )

    if db_conn:
        try:
            cur = db_conn.execute(
                """
                SELECT lower(email_address)
                FROM   zerobounce_validation
                WHERE  lower(trim(COALESCE(zb_status, ''))) = 'valid'
                """
            )
            zb_valid = {r[0] for r in cur.fetchall()}
            in_progress = len(zb_valid & sent_emails)
            cur = db_conn.execute(
                """
                SELECT COUNT(*) FROM zerobounce_validation zv
                INNER JOIN email_attempts ea
                  ON lower(ea.email_address) = lower(zv.email_address)
                WHERE lower(trim(COALESCE(zv.zb_status, ''))) = 'valid'
                """
            )
            zb_valid_with_attempt = cur.fetchone()[0]
            logger.info("-" * 70)
            logger.info(
                f"  ZB-valid addresses in DB         : {len(zb_valid)}"
            )
            logger.info(
                f"  …already in progress sent_emails : {in_progress}"
            )
            logger.info(
                f"  …with an email_attempts row      : {zb_valid_with_attempt}"
            )
        except Exception as e:
            logger.info(f"  (Could not read zerobounce stats: {e})")

    logger.info("=" * 70)


def build_email_list(companies: list, progress: dict,
                     db_conn=None, exclusions: dict = None) -> list:
    """
    For each company build the list of emails to send.
    Priority:
      1. Per employee: evaluate every EMAIL_FORMATS variant (bounce/exclusion/
         attempted/progress gates), queue every address that passes
         validation_allows_send (all valid formats, not just one).
      2. info@domain as fallback if no employee found or all formats exhausted
    Exclusions: any match on domain, email address, or employee name in
                exclusion_list.csv suppresses the email and logs to exclusion_log.
    Validation: only addresses explicitly marked valid are queued; every other
                validation state is skipped without attempt/progress writes.
    """
    if exclusions is None:
        exclusions = {"domains": set(), "emails": set(), "names": set()}

    emails = []

    for company in companies:
        name   = company["company_name"]
        domain = clean_domain(company["company_domain"])

        if not domain:
            continue

        # ── Domain-level exclusion check ─────────────────────────────────
        domain_reason = is_excluded(exclusions, domain, "", "")
        if domain_reason:
            logger.info(f"  🚫 {name} — EXCLUDED ({domain_reason})")
            # Log each employee as excluded in the DB
            for emp in company["employees"]:
                candidate = build_email_address(
                    emp["first_name"],
                    emp["last_name"].split()[0] if emp["last_name"] else "",
                    domain, "firstname.lastname"
                )
                log_exclusion(db_conn, emp["full_name"], name, domain,
                              candidate, domain_reason)
            continue

        # Skip if company already contacted — unless bounce retry or more valid formats
        if already_sent(progress, "", name):
            has_bounced = False
            if db_conn:
                cur = db_conn.execute(
                    "SELECT 1 FROM email_attempts WHERE company_domain = ? AND status = 'bounced' LIMIT 1",
                    (domain,)
                )
                has_bounced = cur.fetchone() is not None
            pending_valid = company_has_pending_validated_staff(
                company, progress, db_conn, exclusions
            )
            if not has_bounced and not pending_valid:
                logger.info(f"  ⏭️  Skipping {name} (already contacted, no bounces)")
                continue
            if has_bounced:
                logger.info(
                    f"  🔄 {name} — bounced email detected, checking for retry formats..."
                )
            elif pending_valid:
                logger.info(
                    f"  🔄 {name} — already contacted, additional validated format(s) to send"
                )

        if not domain_resolves(domain):
            logger.error(f"  ❌ {name}: domain {domain!r} does not resolve — skipped")
            continue

        logger.info("\n  " + "." * 60)
        logger.info(f"  📍 {name} ({domain})")

        added_for_company = 0

        # Staff emails — queue every validated format variant
        for emp in company["employees"]:
            if len(emails) >= MAX_EMAILS:
                break

            bounced = get_bounced_formats(db_conn, domain, emp["full_name"])
            logger.info(f"    🔍 {emp['full_name']} — bounced formats: {bounced or 'none'}")

            valid_options = collect_valid_format_options(
                emp, name, domain, bounced, exclusions, progress, db_conn
            )
            if not valid_options:
                logger.info(f"    ⊘  No validated sendable format for {emp['full_name']}")
                continue

            queued_formats = sort_validated_formats(valid_options)
            if len(queued_formats) > 1:
                summary = ", ".join(f"{f}:{e}" for f, e, _ in queued_formats)
                logger.info(
                    f"    ➡️  Queuing {len(queued_formats)} validated format(s): {summary}"
                )

            for chosen_format, chosen_email, validation_reason in queued_formats:
                if len(emails) >= MAX_EMAILS:
                    break
                emails.append({
                    "recipient":       chosen_email,
                    "salutation":      emp["full_name"],
                    "company":         name,
                    "company_context": generate_company_context(name),
                    "subject":         generate_subject(name),
                    "email_type":      f"staff_{chosen_format}",
                    "domain":          domain,
                    "person_name":     emp["full_name"],
                    "job_title":       emp["job_title"],
                    "email_format":    chosen_format,
                })
                added_for_company += 1
                logger.info(
                    f"    ✅ [{chosen_format}] {chosen_email} ({validation_reason})"
                )

        # Fallback: info@ if no staff emails added
        if added_for_company == 0 and len(emails) < MAX_EMAILS:
            info_email = f"info@{domain}"
            if not already_attempted(db_conn, info_email) and \
               not already_sent(progress, info_email, name):
                can_send, validation_reason = validation_allows_send(db_conn, info_email)
                if can_send:
                    emails.append({
                        "recipient":       info_email,
                        "salutation":      f"{name} Team",
                        "company":         name,
                        "company_context": generate_company_context(name),
                        "subject":         generate_subject(name),
                        "email_type":      "generic_info",
                        "domain":          domain,
                        "person_name":     "",
                        "job_title":       "",
                        "email_format":    "generic_info",
                    })
                    logger.info(f"    🔄 Fallback: {info_email} ({validation_reason})")
                else:
                    logger.info(f"    ⏭️  Fallback {info_email} not valid for sending ({validation_reason})")

        if len(emails) >= MAX_EMAILS:
            break

    return emails[:MAX_EMAILS]

# =============================================
# SMTP / SENDING
# =============================================

def load_active_profiles(path: str) -> tuple:
    """
    Load sending profiles directly from email_config.json.
    Returns (active_profiles list, smtp_passwords dict).
    No credentials_FINAL.json needed — every account in email_config.json is active.
    """
    if not os.path.exists(path):
        logger.error(f"❌ {path} not found — create it with Gmail App Passwords.")
        return [], {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        smtp_passwords = data.get("profiles", {})
        active_profiles = [
            {"email": email, "name": email}
            for email, password in smtp_passwords.items()
            if email.strip() and password.strip()
        ]
        logger.info(f"✓ Loaded {len(active_profiles)} sending profile(s) from {path}")
        return active_profiles, smtp_passwords
    except Exception as e:
        logger.error(f"❌ Could not read {path}: {e}")
        return [], {}


def read_template(path: str, company: str,
                  company_context: str, salutation: str) -> str | None:
    if not os.path.exists(path):
        logger.info(f"⚠️  Template not found: {path}")
        return None
    for enc in ("utf-8", "windows-1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                html = f.read()
            html = (html
                    .replace("{COMPANY}",         company)
                    .replace("{COMPANY_CONTEXT}",  company_context)
                    .replace("{NAME}",             salutation))
            return html
        except UnicodeDecodeError:
            continue
    return None


def send_one(smtp_password: str, from_email: str,
             email_data: dict, progress: dict,
             db_conn=None) -> bool:
    domain  = email_data["domain"]
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
    msg["From"]    = from_email
    msg["To"]      = email_data["recipient"]
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(from_email, smtp_password)
            server.send_message(msg)
        logger.info(f"    ✓ SENT → {email_data['recipient']}")
        update_progress(progress, email_data["recipient"], email_data["company"])
        log_email_attempt(
            db_conn,
            employee_name  = email_data.get("person_name", ""),
            company_name   = email_data.get("company", ""),
            company_domain = email_data.get("domain", ""),
            email_address  = email_data["recipient"],
            email_format   = email_data.get("email_format", ""),
            from_profile   = from_email,
        )
        time.sleep(EMAIL_SEND_DELAY)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(f"    ✗ AUTH ERROR for {from_email} — check App Password")
        return False
    except Exception as e:
        logger.error(f"    ✗ ERROR: {e}")
        return False


def split_round_robin(emails: list, n_profiles: int) -> list:
    buckets = [[] for _ in range(n_profiles)]
    for i, email in enumerate(emails):
        buckets[i % n_profiles].append(email)
    return [b for b in buckets if b]

# =============================================
# MAIN
# =============================================

def main():
    # ── Parse command-line arguments ──────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="LinkedIn DB — Cold Email Sender (batch mode)",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python send_linkedin_campaigns_params.py --country Austria --per-profile 10\n"
            "  python send_linkedin_campaigns_params.py -c Germany -n 5\n"
            "  python send_linkedin_campaigns_params.py -n 8\n"
            "  python send_linkedin_campaigns_params.py --list-countries"
        )
    )
    parser.add_argument(
        "--country", "-c",
        default=None,
        metavar="COUNTRY",
        help="Country name to filter by (e.g. 'Austria'). Omit to send to all countries."
    )
    parser.add_argument(
        "--per-profile", "-n",
        type=int,
        default=5,
        metavar="N",
        help="Number of emails to send per Gmail profile (default: 5)."
    )
    parser.add_argument(
        "--list-countries",
        action="store_true",
        help="Print available countries from the database and exit."
    )
    args = parser.parse_args()
    run_started_perf = time.perf_counter()

    logger.info("=" * 70)
    logger.info("  LINKEDIN DB — COLD EMAIL SENDER  [batch mode]")
    logger.info(f"  Run started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"  Log file   : {os.path.abspath(LOG_FILE)}")
    logger.info("=" * 70)

    # ── Open DB ──────────────────────────────────────────────────────────
    conn  = open_db(DB_PATH)
    stats = db_stats(conn)
    logger.info(f"\n  Database : {os.path.abspath(DB_PATH)}")
    logger.info(f"  Companies: {stats['companies']}  (with domain: {stats['with_domain']})")
    logger.info(f"  Employees: {stats['employees']}")

    # ── Country list / filter ─────────────────────────────────────────────
    countries = get_available_countries(conn)
    if not countries:
        logger.info("\n  ❌ No companies in database yet — run the scraper first.")
        conn.close()
        sys.exit(1)

    # --list-countries: print and exit
    if args.list_countries:
        logger.info("\n  Countries available:")
        for i, (country, cnt) in enumerate(countries, 1):
            logger.info(f"    {i:2d}. {country}  ({cnt} companies)")
        conn.close()
        logger.info("\n  Done (listed countries — no emails sent).")
        logger.info(f"  Log file: {os.path.abspath(LOG_FILE)}")
        sys.exit(0)

    # Resolve country argument
    country_names  = [c[0] for c in countries]
    country_filter = None
    if args.country:
        match = next((c for c in country_names if c.lower() == args.country.lower()), None)
        if not match:
            logger.error(f"\n  ❌ Country '{args.country}' not found in database.")
            logger.info(f"     Available: {', '.join(country_names)}")
            logger.info(f"     Use --list-countries to see all options.")
            conn.close()
            sys.exit(1)
        country_filter = match

    logger.info(f"\n  Country filter  : {country_filter or 'All'}")
    logger.info(f"  Emails/profile  : {args.per_profile}")

    # ── Load SMTP / profiles (directly from email_config.json) ───────────
    active_profiles, smtp_passwords = load_active_profiles(SMTP_CONFIG_FILE)

    if not active_profiles:
        logger.info("\n  ❌ No sending profiles found in email_config.json.")
        conn.close()
        sys.exit(1)
    logger.info(f"  ✓ {len(active_profiles)} sending profile(s) ready")

    # ── Load progress ────────────────────────────────────────────────────
    progress = load_progress()
    if progress["sent_emails"]:
        logger.info(f"\n  📋 Resume mode: {len(progress['sent_emails'])} emails already sent")
        logger.info(f"     Companies already contacted: {len(progress['sent_companies'])}")

    # ── Load companies from DB ───────────────────────────────────────────
    logger.info("\n  Loading companies from database...")
    companies = load_companies(conn, country_filter)
    conn.close()
    logger.info(f"  ✓ {len(companies)} companies loaded")

    # ── Build email list ─────────────────────────────────────────────────
    logger.info("\n" + "=" * 70)
    logger.info("  BUILDING EMAIL LIST")
    logger.info("=" * 70)

    db_conn    = get_db_conn()
    exclusions = load_exclusions(EXCLUSION_FILE)
    all_emails = build_email_list(companies, progress, db_conn, exclusions)

    if not all_emails:
        logger.info("\n  ✅ No validated emails available to send.")
        logger.info("     Only zb_status=valid or custom mv_status=ok/valid addresses are queued.")
        log_no_queue_diagnostics(companies, progress, db_conn, exclusions)
        if db_conn:
            db_conn.close()
        emit_run_summary(
            run_started_perf=run_started_perf,
            queued=0,
            success=0,
            failed=0,
            progress=progress,
            outcome="no_queue (nothing to send after validation / resume filters)",
            country_filter=country_filter,
            per_profile=args.per_profile,
            n_profiles=len(active_profiles),
        )
        sys.exit(0)

    staff_count   = sum(1 for e in all_emails if e["email_type"].startswith("staff"))
    generic_count = sum(1 for e in all_emails if e["email_type"].startswith("generic"))

    logger.info("\n" + "=" * 70)
    logger.info("  EMAIL BREAKDOWN")
    logger.info("=" * 70)
    logger.info(f"  Staff emails   : {staff_count}")
    logger.info(f"    → all validated format variants queued per employee")
    logger.info(f"    → sent to named individuals found in the DB")
    logger.info(f"  Generic info@  : {generic_count}")
    logger.info(f"    → info@domain (fallback — no employee found)")
    logger.info(f"    → sent to company inbox, not a named person")
    logger.info(f"  Total available: {len(all_emails)}")
    logger.info("=" * 70)

    # ── Apply per-profile cap ─────────────────────────────────────────────
    max_per_profile = args.per_profile
    max_total       = max_per_profile * len(active_profiles)
    all_emails      = all_emails[:max_total]
    logger.info(f"\n  Sending: {max_per_profile} per profile × {len(active_profiles)} profiles = {len(all_emails)} emails")

    logger.info("\n  Full list:")
    last_company = None
    for i, e in enumerate(all_emails, 1):
        if last_company and last_company != e["company"]:
            logger.info("    " + "." * 60)
        last_company = e["company"]
        
        kind   = "STAFF  " if e["email_type"].startswith("staff") else "GENERIC"
        person = f"  ← {e['person_name']} ({e['job_title']})" if e["person_name"] else ""
        logger.info(f"    {i:3d}. [{kind}] {e['recipient']}{person}")

    # ── Template check ───────────────────────────────────────────────────
    if not os.path.exists(TEMPLATE_FILE):
        logger.info(f"\n  ⚠️  WARNING: Template file not found: {TEMPLATE_FILE}")
    else:
        logger.info(f"\n  ✓ Template: {TEMPLATE_FILE}")

    # ── Distribution across profiles ─────────────────────────────────────
    batches = split_round_robin(all_emails, len(active_profiles))
    logger.info(f"\n  Distribution across {len(active_profiles)} profile(s):")
    for i, (profile, batch) in enumerate(zip(active_profiles, batches), 1):
        logger.info(f"    Profile {i} ({profile['name']}): {len(batch)} emails")

    logger.info("\n" + "=" * 70)
    logger.info("  STARTING SEND...")
    logger.info("=" * 70)

    # ── Interleaved send loop ─────────────────────────────────────────────
    interleaved = []
    for round_emails in zip_longest(*batches):
        for prof_idx, email in enumerate(round_emails):
            if email is not None:
                interleaved.append((prof_idx, email))

    total_success = 0
    total_failed  = 0
    prev_prof_idx = None
    queued_count = len(interleaved)
    interrupted = False

    logger.info(f"\n  Sending {queued_count} emails across {len(active_profiles)} profile(s)...")

    try:
        for send_num, (prof_idx, email_data) in enumerate(interleaved, 1):
            profile       = active_profiles[prof_idx]
            smtp_password = smtp_passwords[profile["email"]]

            if prev_prof_idx is not None and prof_idx != prev_prof_idx:
                logger.info(f"\n  🔄 Switching to Profile {prof_idx+1} ({profile['name']}) "
                            f"— waiting {PROFILE_SWITCH_DELAY}s...")
                time.sleep(PROFILE_SWITCH_DELAY)

            # Progress indicator
            pct = int((send_num / queued_count) * 100)
            bar_len = 20
            filled = int((send_num / queued_count) * bar_len)
            bar = "█" * filled + "-" * (bar_len - filled)

            person = f" — {email_data['person_name']}" if email_data["person_name"] else ""

            logger.info(f"\n" + "-" * 60)
            logger.info(f"  [{send_num}/{queued_count}] {pct}% |{bar}|")
            logger.info(f"  P{prof_idx+1} | {email_data['company']}{person}")
            logger.info(f"  → {email_data['recipient']}")

            if send_one(smtp_password, profile["email"], email_data, progress, db_conn):
                total_success += 1
                if total_success % EMAIL_BATCH_SIZE == 0:
                    logger.info(f"\n  🛑 Batch break after {total_success} sent — "
                                f"waiting {EMAIL_BATCH_BREAK}s...")
                    time.sleep(EMAIL_BATCH_BREAK)
            else:
                total_failed += 1

            logger.info("") # Empty line after transaction
            prev_prof_idx = prof_idx
    except KeyboardInterrupt:
        interrupted = True
        logger.info("\n  ⚠️  Interrupted — saving progress and closing DB...")
    finally:
        save_progress(progress)
        if db_conn:
            db_conn.close()
        outcome = (
            "interrupted (Ctrl+C) — partial sends may have completed"
            if interrupted
            else "completed"
        )
        emit_run_summary(
            run_started_perf=run_started_perf,
            queued=queued_count,
            success=total_success,
            failed=total_failed,
            progress=progress,
            outcome=outcome,
            country_filter=country_filter,
            per_profile=args.per_profile,
            n_profiles=len(active_profiles),
        )

    if interrupted:
        raise KeyboardInterrupt


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n\n  Cancelled — progress saved.")
        sys.exit(0)
    except Exception as e:
        import traceback
        logger.info(f"\n  ✗ Fatal error: {e}")
        traceback.print_exc()
        sys.exit(1)
