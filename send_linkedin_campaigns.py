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

# =============================================
# CONFIGURATION — edit these as needed
# =============================================

DB_PATH           = "linkedin_data.db"
CREDENTIALS_FILE  = "credentials_FINAL.json"
SMTP_CONFIG_FILE  = "email_config.json"
TEMPLATE_FILE     = "email_template_with_link.htm"
PROGRESS_FILE     = "email_progress_linkedin.json"
LOG_FILE          = "send_linkedin_campaigns.log"

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

    # File handler — appends so history is preserved across runs
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
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

def get_db_conn():
    """Open linkedin_data.db in same folder as this script."""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "linkedin_data.db")
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


def build_email_address(first: str, last: str, domain: str, fmt: str) -> str:
    """Generate email address for a given format."""
    f = first.lower().strip()
    l = last.lower().strip().split()[0]  # first word of last name only
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


def build_email_list(companies: list, progress: dict, db_conn=None) -> list:
    """
    For each company build the list of emails to send.
    Priority:
      1. Next unused/non-bounced format for each employee (exec-level first)
         Formats tried in order: firstname.lastname → firstname →
                                 firstinitial.lastname → firstname.lastinitial
      2. info@domain as fallback if no employee found or all formats exhausted
    """
    emails = []

    for company in companies:
        name   = company["company_name"]
        domain = clean_domain(company["company_domain"])

        if not domain:
            continue

        # Skip if company already contacted via progress file
        if already_sent(progress, "", name):
            logger.info(f"  ⏭️  Skipping {name} (already contacted)")
            continue

        if not domain_resolves(domain):
            logger.error(f"  ❌ {name}: domain {domain!r} does not resolve — skipped")
            continue

        logger.info(f"\n  📍 {name} ({domain})")

        added_for_company = 0

        # Staff emails — bounce-aware format selection
        for emp in company["employees"]:
            if len(emails) >= MAX_EMAILS:
                break

            first = emp["first_name"]
            last  = emp["last_name"].split()[0] if emp["last_name"] else ""
            if not first.isalpha() or not last.isalpha():
                logger.warning(f"    ⚠️  Skipping bad name: {emp['full_name']!r}")
                continue

            # Find which formats have already bounced for this person
            bounced = get_bounced_formats(db_conn, domain, emp["full_name"])

            # Pick the first format not yet bounced and not yet attempted
            chosen_email = None
            chosen_format = None
            for fmt in EMAIL_FORMATS:
                if fmt in bounced:
                    logger.info(f"    ↩️  {fmt} bounced previously — skipping")
                    continue
                candidate = build_email_address(first, last, domain, fmt)
                if already_attempted(db_conn, candidate):
                    logger.info(f"    ⏭️  {candidate} already attempted")
                    continue
                if already_sent(progress, candidate, name):
                    logger.info(f"    ⏭️  {candidate} already sent (progress file)")
                    continue
                chosen_email  = candidate
                chosen_format = fmt
                break

            if not chosen_email:
                logger.info(f"    ⊘  All formats exhausted for {emp['full_name']}")
                continue

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
            logger.info(f"    ✅ [{chosen_format}] {chosen_email} ({emp['job_title']})")

        # Generic info@ fallback disabled — staff emails only
        # if added_for_company == 0 and len(emails) < MAX_EMAILS:
        #     info_email = f"info@{domain}"
        #     if not already_attempted(db_conn, info_email) and         #        not already_sent(progress, info_email, name):
        #         emails.append({
        #             "recipient":       info_email,
        #             "salutation":      f"{name} Team",
        #             "company":         name,
        #             "company_context": generate_company_context(name),
        #             "subject":         generate_subject(name),
        #             "email_type":      "generic_info",
        #             "domain":          domain,
        #             "person_name":     "",
        #             "job_title":       "",
        #             "email_format":    "generic_info",
        #         })
        #         logger.info(f"    🔄 Fallback: {info_email}")
        if added_for_company == 0:
            logger.info(f"    ⊘  No employees found for {name} — skipped (generic disabled)")

        if len(emails) >= MAX_EMAILS:
            break

    return emails[:MAX_EMAILS]

# =============================================
# SMTP / SENDING
# =============================================

def load_credentials(path: str) -> dict:
    if not os.path.exists(path):
        logger.error("❌ Credentials file not found: {path}")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f).get("profiles", {})


def get_active_profiles(profiles_dict: dict) -> list:
    active = []
    for key, info in profiles_dict.items():
        if "✅" in info.get("status", "") and info.get("gmail_email"):
            active.append({
                "key":   key,
                "email": info["gmail_email"],
                "name":  info.get("profile_name", key),
            })
    return active


def load_smtp_config(path: str) -> dict:
    if not os.path.exists(path):
        logger.error("❌ {path} not found — create it with Gmail App Passwords.")
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        passwords = data.get("profiles", {})
        logger.info(f"✓ Loaded {len(passwords)} SMTP password(s)")
        return passwords
    except Exception as e:
        logger.error("❌ Could not read {path}: {e}")
        return {}


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
        logger.error("    ✗ ERROR: {e}")
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
    # Clear terminal for a clean start
    os.system("cls" if os.name == "nt" else "clear")

    logger.info("=" * 70)
    logger.info("  LINKEDIN DB — COLD EMAIL SENDER")
    logger.info(f"  Run started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"  Log file   : {os.path.abspath(LOG_FILE)}")
    logger.info("=" * 70)

    # ── Open DB ──────────────────────────────────────────────────────────
    conn  = open_db(DB_PATH)
    stats = db_stats(conn)
    logger.info(f"\n  Database : {os.path.abspath(DB_PATH)}")
    logger.info(f"  Companies: {stats['companies']}  (with domain: {stats['with_domain']})")
    logger.info(f"  Employees: {stats['employees']}")

    # ── Country filter ───────────────────────────────────────────────────
    countries = get_available_countries(conn)
    if not countries:
        logger.info("\n  ❌ No companies in database yet — run the scraper first.")
        conn.close()
        return

    logger.info("\n  Countries available:")
    for i, (country, cnt) in enumerate(countries, 1):
        logger.info(f"    {i:2d}. {country}  ({cnt} companies)")
    logger.info(f"    {len(countries)+1:2d}. All countries")

    choice = input("\n  Select country (number, or Enter for all): ").strip()
    country_filter = None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(countries):
            country_filter = countries[idx][0]
    except (ValueError, IndexError):
        pass

    logger.info(f"\n  Country filter: {country_filter or 'All'}")

    # ── Load SMTP / profiles ─────────────────────────────────────────────
    smtp_passwords   = load_smtp_config(SMTP_CONFIG_FILE)
    profiles_dict    = load_credentials(CREDENTIALS_FILE)
    active_profiles  = get_active_profiles(profiles_dict)
    active_profiles  = [p for p in active_profiles if p["email"] in smtp_passwords]

    if not active_profiles:
        logger.info("\n  ❌ No active profiles with SMTP passwords found.")
        conn.close()
        return
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
    all_emails = build_email_list(companies, progress, db_conn)

    if not all_emails:
        logger.info("\n  ✅ All companies already contacted.")
        logger.info(f"     Delete {PROGRESS_FILE} to start fresh.")
        return

    staff_count   = sum(1 for e in all_emails if e["email_type"].startswith("staff"))
    generic_count = sum(1 for e in all_emails if e["email_type"].startswith("generic"))

    logger.info("\n" + "=" * 70)
    logger.info("  EMAIL BREAKDOWN")
    logger.info("=" * 70)
    logger.info(f"  Staff emails   : {staff_count}")
    logger.info(f"    → firstname.lastname@domain (LinkedIn employees)")
    logger.info(f"    → sent to named individuals found in the DB")
    logger.info(f"  Generic info@  : {generic_count}")
    logger.info(f"    → info@domain (fallback — no employee found)")
    logger.info(f"    → sent to company inbox, not a named person")
    logger.info(f"  Total available: {len(all_emails)}")
    logger.info("=" * 70)

    # ── Ask how many emails per profile ──────────────────────────────────
    max_per_profile = 5  # default
    raw = input(f"\n  Emails per profile? (default {max_per_profile}, "
                f"{len(active_profiles)} profile(s) → "
                f"{max_per_profile * len(active_profiles)} total): ").strip()
    try:
        if raw:
            max_per_profile = max(1, int(raw))
    except ValueError:
        logger.info("  Invalid number — using default.")

    max_total = max_per_profile * len(active_profiles)
    all_emails = all_emails[:max_total]
    logger.info(f"\n  Sending: {max_per_profile} per profile × {len(active_profiles)} profiles = {len(all_emails)} emails")

    logger.info("\n  Full list:")
    for i, e in enumerate(all_emails, 1):
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
    input("  Press Enter to start sending  (Ctrl+C to cancel)...")

    # ── Interleaved send loop ─────────────────────────────────────────────
    interleaved = []
    for round_emails in zip_longest(*batches):
        for prof_idx, email in enumerate(round_emails):
            if email is not None:
                interleaved.append((prof_idx, email))

    total_success = 0
    total_failed  = 0
    prev_prof_idx = None

    logger.info(f"\n  Sending {len(interleaved)} emails across {len(active_profiles)} profile(s)...")

    for send_num, (prof_idx, email_data) in enumerate(interleaved, 1):
        profile      = active_profiles[prof_idx]
        smtp_password = smtp_passwords[profile["email"]]

        if prev_prof_idx is not None and prof_idx != prev_prof_idx:
            logger.info(f"\n  🔄 Switching to Profile {prof_idx+1} ({profile['name']}) "
f"— waiting {PROFILE_SWITCH_DELAY}s...")
            time.sleep(PROFILE_SWITCH_DELAY)

        person = f" — {email_data['person_name']}" if email_data["person_name"] else ""
        logger.info(f"\n[{send_num}/{len(interleaved)}] P{prof_idx+1} | "
f"{email_data['company']}{person}")
        logger.info(f"  → {email_data['recipient']}")

        if send_one(smtp_password, profile["email"], email_data, progress, db_conn):
            total_success += 1
            # Batch break
            if total_success > 0 and total_success % EMAIL_BATCH_SIZE == 0:
                logger.info(f"\n  🛑 Batch break after {total_success} sent — "
f"waiting {EMAIL_BATCH_BREAK}s...")
                time.sleep(EMAIL_BATCH_BREAK)
        else:
            total_failed += 1

        prev_prof_idx = prof_idx

    # ── Final save & summary ──────────────────────────────────────────────
    save_progress(progress)

    logger.info("\n" + "=" * 70)
    logger.info("  DONE")
    logger.info("=" * 70)
    logger.info(f"  ✓ Sent this run : {total_success}")
    logger.info(f"  ✗ Failed        : {total_failed}")
    logger.info(f"  📧 Grand total  : {progress['total_sent']}")
    logger.info(f"  💡 To start fresh, delete: {PROGRESS_FILE}")
    logger.info("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n\n  Cancelled — progress saved.")
    except Exception as e:
        import traceback
        logger.info(f"\n  ✗ Fatal error: {e}")
        traceback.print_exc()
    finally:
        input("\n  Press Enter to exit...")
