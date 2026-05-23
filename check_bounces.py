#!/usr/bin/env python3
"""
check_bounces.py
================
Two responsibilities in one run:

  1. BOUNCE DETECTION
     Finds bounce/NDR emails via IMAP, marks email_attempts as 'bounced'
     in linkedin_data.db.  Uses Gmail labels (PROCESSED-CAREER, BOUNCE-CAREER, …)
     so each INBOX message is read once per window — no UNSEEN / \\Seen reliance.

  2. CAREER-REPLY FORWARDING
     Same INBOX sweep: messages without PROCESSED-CAREER get a header pass;
     non-bounce threads are checked for career keywords and may be forwarded to
     TARGET_FORWARD_EMAIL.  Outcomes use REPLY-CAREER, NON-CAREER, etc.

Run this once a day, ideally the morning after a send run.

Config files needed (same folder as this script):
  email_config.json   — Gmail App Passwords  (key: profiles dict)
  linkedin_data.db    — the scraper / campaign database
"""

import imaplib
import smtplib
import email
import json
import os
import re
import sqlite3
import ssl
import sys
import time
import logging
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr

# Reconfigure stdout/stderr to UTF-8 to prevent UnicodeEncodeError on Windows console when printing emojis
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# =============================================
# CONFIG
# =============================================
_SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
SMTP_CONFIG_FILE = r"C:\Users\sandeep\Downloads\Claudes\EmailJson\email_config.json"
DB_PATH          = os.path.join(_SCRIPT_DIR, "data", "db", "linkedin_data.db")

# Each run gets its own timestamped log file inside a logs/ subfolder
_LOG_DIR = os.path.join(_SCRIPT_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(
    _LOG_DIR,
    f"check_bounces_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
)


def _profile_progress_label(sr_no: int, total: int) -> str:
    """1-based index in email_config.json ``profiles`` order → ``[i/N — pct%]``."""
    if total <= 0:
        return "[0/0 — 0.0%]"
    pct = 100.0 * sr_no / total
    return f"[{sr_no}/{total} — {pct:.1f}%]"


# IMAP / SMTP settings for Gmail
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# Career replies are forwarded here
TARGET_FORWARD_EMAIL = "sandeepjain200019@gmail.com"

# Max career emails to forward per account per run (avoids spam flag risk)
MAX_FORWARDS_PER_ACCOUNT = 3
FORWARD_DELAY_SECS       = 60   # seconds between individual forwards

# Gmail label namespace (mirrors Campus *-CAMPUS pattern; CAREER for this script)
GMAIL_LABEL_PROCESSED = "PROCESSED-CAREER"
GMAIL_LABEL_BOUNCE = "BOUNCE-CAREER"
GMAIL_LABEL_BOUNCE_UNMATCHED = "BOUNCE-CAREER-UNMATCHED"
GMAIL_LABEL_REPLY = "REPLY-CAREER"
GMAIL_LABEL_AUTOREPLY = "AUTOREPLY-CAREER"
GMAIL_LABEL_NON_CAREER = "NON-CAREER"
GMAIL_LABEL_FORWARDED = "FORWARDED-CAREER"
GMAIL_LABEL_FWD_PENDING = "FWD-PEND-CAREER"

CAREER_GMAIL_LABELS = (
    GMAIL_LABEL_PROCESSED,
    GMAIL_LABEL_BOUNCE,
    GMAIL_LABEL_BOUNCE_UNMATCHED,
    GMAIL_LABEL_REPLY,
    GMAIL_LABEL_AUTOREPLY,
    GMAIL_LABEL_NON_CAREER,
    GMAIL_LABEL_FORWARDED,
    GMAIL_LABEL_FWD_PENDING,
)

# How far back to scan INBOX via IMAP SINCE (then filter with X-GM-LABELS)
LOOKBACK_DAYS = 30

# Pass 1: UIDs per one ``UID FETCH`` (fewer round-trips).
HEADER_FETCH_BATCH = 40

# Bounce sender patterns — emails from these senders indicate a bounce/NDR
BOUNCE_SENDERS = [
    "mailer-daemon@googlemail.com",
    "mailer-daemon@google.com",
    "postmaster@",
    "mailer-daemon@",
    "noreply@",
    "no-reply@",
    "mailer@",
    "system@",
    "administrator@",
]

# Subject patterns that indicate a bounce/NDR
BOUNCE_SUBJECTS = [
    "delivery status notification",
    "undeliverable",
    "mail delivery failed",
    "returned mail",
    "failure notice",
    "delivery failure",
    "non-delivery",
    "message not delivered",
    "unable to deliver",
    "bounce",
    "out of office",
    "auto-reply",
    "autoreply",
]

# Automated / notification sender prefixes — these are tools/platforms,
# not real humans replying to an email.  Forwarding is skipped for these.
AUTOMATED_SENDER_PREFIXES = [
    "alerts@",
    "alert@",
    "notifications@",
    "notification@",
    "info@",
    "news@",
    "hello@",
    "team@",
    "support@",
    "contact@",
    "donotreply@",
    "do-not-reply@",
    "no.reply@",
    "updates@",
    "update@",
    "newsletter@",
    "digest@",
    "campaigns@",
    "campaign@",
    "marketing@",
    "promo@",
]

# Keywords that strongly suggest a genuine career-related reply.
# NOTE: these are intentionally specific — generic business words like
# "opportunity" or "profile" are NOT included because they appear in
# marketing emails too.
CAREER_KEYWORDS = [
    "job application",
    "your application",
    "we received your",
    "thank you for applying",
    "interview",
    "job offer",
    "offer letter",
    "hiring",
    "recruiter",
    "recruitment",
    "candidate",
    "vacancy",
    "job opening",
    "open position",
    "freelance",
    "full-time",
    "part-time",
    "onboarding",
    "talent acquisition",
    "campus placement",
    "sandeep jain",        # always forward if his name is in subject/body
]

# Keywords that indicate marketing / sales / newsletter junk.
# If ANY of these appear in the subject, the email is skipped.
JUNK_KEYWORDS = [
    "newsletter",
    "black friday",
    "cyber monday",
    "sale",
    "discount",
    "limited time offer",
    "buy now",
    "promo code",
    "promotion",
    "marketing",
    "unsubscribe",
    "special offer",
    "leads",
    "more leads",
    "meetings",
    "more meetings",
    "demo",
    "free trial",
    "sign up",
    "sign-up",
    "webinar",
    "saas",
    "platform",
    "automate",
    "automation",
    "productivity",
    "click here",
    "one link",
    "boost your",
    "grow your",
]

# =============================================
# LOGGING
# =============================================
_LOG_FMT = "%(asctime)s [%(levelname)-7s] %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"
_EMAIL_IN_LOG_RE = re.compile(
    r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}",
    re.I,
)


class _RedactEmailsStreamFormatter(logging.Formatter):
    """Terminal handler: strip email addresses; file handler keeps full text."""

    def format(self, record: logging.LogRecord) -> str:
        return _EMAIL_IN_LOG_RE.sub("<redacted>", super().format(record))


def _setup_logging() -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATE))

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(_RedactEmailsStreamFormatter(_LOG_FMT, datefmt=_LOG_DATE))

    root.addHandler(fh)
    root.addHandler(sh)
    return logging.getLogger("bounce_checker")


logger = _setup_logging()
logger.info(f"📄 Log file: {LOG_FILE}")

# =============================================
# NVIDIA NIM KEY ROTATION & LLM CLASSIFICATION
# =============================================

def load_nvidia_keys() -> list:
    keys_dir = r"C:\Users\sandeep\Downloads\Claudes\code-review-tool\nvidia_keys"
    keys = []
    if not os.path.exists(keys_dir):
        logger.warning(f"Nvidia keys directory not found: {keys_dir}")
        return keys
    for fn in os.listdir(keys_dir):
        if fn.startswith("key") and fn.endswith(".json"):
            fp = os.path.join(keys_dir, fn)
            try:
                with open(fp, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                    if data.get("api_key"):
                        keys.append(data["api_key"])
            except Exception as e:
                logger.warning(f"Failed to read Nvidia key file {fn}: {e}")
    logger.info(f"Loaded {len(keys)} Nvidia API keys for rotation.")
    return keys

NVIDIA_KEYS = load_nvidia_keys()
NVIDIA_KEY_INDEX = 0

def get_next_nvidia_key() -> str:
    global NVIDIA_KEY_INDEX
    if not NVIDIA_KEYS:
        return ""
    key = NVIDIA_KEYS[NVIDIA_KEY_INDEX % len(NVIDIA_KEYS)]
    NVIDIA_KEY_INDEX += 1
    return key

def call_nvidia_llm_with_rotation(system_prompt: str, user_prompt: str) -> str:
    import urllib.request
    
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    payload = {
        "model": "meta/llama-3.3-70b-instruct",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 512
    }
    
    max_attempts = min(5, len(NVIDIA_KEYS)) if NVIDIA_KEYS else 1
    for attempt in range(max_attempts):
        api_key = get_next_nvidia_key()
        if not api_key:
            logger.error("No Nvidia API key available.")
            return ""
            
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            },
            method="POST"
        )
        
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                return res_data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning(f"Nvidia LLM call failed with key attempt {attempt + 1}: {e}. Rotating key.")
            time.sleep(1)
            
    logger.error("All Nvidia LLM key attempts failed.")
    return ""

def classify_email_via_llm(subject: str, from_addr: str, body: str, has_reply_headers: bool) -> dict:
    """
    Sends email metadata and body content to Nvidia NIM LLM to determine if it should be forwarded.
    Returns a dict with: is_reply, is_auto_response, is_decline, should_forward, reason
    """
    truncated_body = body[:4000]
    
    system_prompt = (
        "You are an email analysis assistant. You are analyzing an incoming email to decide if it should be forwarded.\n"
        "The email was received by Sandeep Jain, who sent out outbound career/placement outreach.\n\n"
        "Evaluate the email against these rules:\n"
        "1. MUST be a reply to Sandeep's email (a response/reply, e.g. thread context, Re: in subject, or referencing prior outreach). Do NOT forward if it is a new cold outreach, spam, newsletter, notifications, alerts, or not a reply.\n"
        "2. MUST NOT be an auto-response (automated out-of-office, automated delivery receipts, auto-replies, mailer-daemon, automated follow-ups, or automated confirmations).\n"
        "3. MUST NOT be a decline (declined, rejected, not interested, no openings, unsubscribe, not hiring, or direct rejection). We only want positive replies, questions, requests for info/resume, interest, or direct human discussion.\n\n"
        "Analyze the email and output a JSON response in the following format:\n"
        "{\n"
        "  \"is_reply\": true/false,\n"
        "  \"is_auto_response\": true/false,\n"
        "  \"is_decline\": true/false,\n"
        "  \"should_forward\": true/false,\n"
        "  \"reason\": \"short explanation of your decision\"\n"
        "}\n"
        "Rules: Output ONLY the raw JSON block without markdown formatting or code blocks. If you are unsure, default should_forward to false."
    )
    
    user_prompt = (
        f"EMAIL DETAILS:\n"
        f"- From: {from_addr}\n"
        f"- Subject: {subject}\n"
        f"- Has Reply-To/References Headers: {'Yes' if has_reply_headers else 'No'}\n\n"
        f"BODY:\n"
        f"{truncated_body}"
    )
    
    fallback_res = {
        "is_reply": False,
        "is_auto_response": False,
        "is_decline": False,
        "should_forward": False,
        "reason": "Fallback due to LLM failure or parse error"
    }
    
    llm_output = call_nvidia_llm_with_rotation(system_prompt, user_prompt)
    if not llm_output:
        return fallback_res
        
    try:
        cleaned = llm_output.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("\n", 1)[0]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
            
        data = json.loads(cleaned)
        return {
            "is_reply": bool(data.get("is_reply", False)),
            "is_auto_response": bool(data.get("is_auto_response", False)),
            "is_decline": bool(data.get("is_decline", False)),
            "should_forward": bool(data.get("should_forward", False)),
            "reason": str(data.get("reason", "Success"))
        }
    except Exception as parse_err:
        logger.warning(f"Failed to parse LLM JSON response: {parse_err}. Response was: {llm_output}")
        return fallback_res

# =============================================
# DB HELPERS
# =============================================

def open_db() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        logger.error(f"Database not found: {DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE IF NOT EXISTS send_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            script          TEXT,
            started_at      TEXT,
            finished_at     TEXT,
            emails_sent     INTEGER DEFAULT 0,
            emails_failed   INTEGER DEFAULT 0,
            bounces_found   INTEGER DEFAULT 0,
            country_filter  TEXT,
            notes           TEXT
        )
    """)

    # Tracks which emails have already been forwarded — avoids duplicate sends
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_emails (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id    INTEGER,
            account         TEXT,
            original_sender TEXT,
            subject         TEXT,
            status          TEXT,
            processed_at    TEXT
        )
    """)

    conn.commit()
    return conn


def mark_bounced(conn: sqlite3.Connection, email_address: str) -> bool:
    """Mark an email address as bounced. Returns True if a record was updated."""
    cur = conn.execute(
        "SELECT id, status FROM email_attempts WHERE email_address = ?",
        (email_address.lower(),)
    )
    row = cur.fetchone()
    if not row:
        return False
    if row["status"] == "bounced":
        return False  # already marked

    conn.execute("""
        UPDATE email_attempts
        SET    status = 'bounced',
               bounce_detected_at = ?
        WHERE  email_address = ?
    """, (datetime.now().isoformat(), email_address.lower()))
    conn.commit()
    return True


def get_all_sent_addresses(conn: sqlite3.Connection) -> set:
    """Return all email addresses we sent to (status = sent or bounced)."""
    cur = conn.execute("SELECT email_address FROM email_attempts")
    return {row["email_address"].lower() for row in cur.fetchall()}


def already_forwarded(conn: sqlite3.Connection, account: str,
                      subject: str, sender: str) -> bool:
    """True if this exact message was already forwarded in a previous run."""
    cur = conn.execute(
        """SELECT id FROM processed_emails
           WHERE account = ? AND subject = ? AND original_sender = ?""",
        (account, subject, sender)
    )
    return cur.fetchone() is not None


def log_processed(conn: sqlite3.Connection, execution_id: int,
                  account: str, sender: str, subject: str, status: str):
    conn.execute("""
        INSERT INTO processed_emails
               (execution_id, account, original_sender, subject, status, processed_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (execution_id, account, sender, subject, status,
          datetime.now().isoformat()))
    conn.commit()


# =============================================
# IMAP / SSL HELPERS
# =============================================

def _make_ssl_context() -> ssl.SSLContext:
    """Return an explicit TLS 1.2+ context — avoids SSL EOF on older defaults."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _imap_connect(gmail_address: str, app_password: str) -> imaplib.IMAP4_SSL:
    """Open a fresh IMAP4_SSL connection with an explicit TLS context."""
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=_make_ssl_context())
    mail.login(gmail_address, app_password)
    mail.select("INBOX")
    return mail


def ensure_gmail_labels(mail: imaplib.IMAP4_SSL) -> None:
    """CREATE each label if missing (Gmail exposes labels as IMAP folders)."""
    for label in CAREER_GMAIL_LABELS:
        try:
            typ, dat = mail.create(label)
            if typ == "OK":
                logger.info(f"  Created Gmail label {label!r}")
                continue
            blob = b" ".join(dat) if dat else b""
            if b"ALREADYEXISTS" in blob or b"exists" in blob.lower():
                continue
            logger.warning(f"  CREATE {label!r}: {typ} {dat}")
        except imaplib.IMAP4.error as e:
            err = str(e).upper()
            if "ALREADYEXISTS" in err or "[ALREADYEXISTS]" in err:
                continue
            logger.warning(f"  CREATE {label!r}: {e}")


def _imap_since_str(days: int) -> str:
    dt = datetime.now() - timedelta(days=days)
    mon = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{dt.day}-{mon[dt.month - 1]}-{dt.year}"


def _fetch_xgm_labels_blob(mail: imaplib.IMAP4_SSL, uid: bytes) -> bytes:
    try:
        typ, data = mail.uid("FETCH", uid, "(X-GM-LABELS)")
    except Exception:
        return b""
    if typ != "OK" or not data:
        return b""
    chunks = []
    for item in data:
        if isinstance(item, tuple):
            for part in item:
                if isinstance(part, bytes):
                    chunks.append(part)
        elif isinstance(item, bytes):
            chunks.append(item)
    return b" ".join(chunks)


def _inbox_uids_since_without_processed_label(
    mail: imaplib.IMAP4_SSL,
    days: int,
    processed_label: str,
    forwarded_label: str,
) -> set:
    since = _imap_since_str(days)
    try:
        typ, data = mail.uid("SEARCH", "SINCE", since)
    except imaplib.IMAP4.error as e:
        logger.warning(f"  IMAP SINCE search failed: {e!r}")
        return set()
    if typ != "OK" or not data or not data[0]:
        return set()
    blob = data[0]
    if not isinstance(blob, bytes) or blob.strip() == b"":
        return set()
    candidates = set(blob.split())
    if not candidates:
        return set()
    needle1 = processed_label.encode("utf-8")
    needle2 = forwarded_label.encode("utf-8")
    out: set = set()
    for uid in sorted(candidates, key=lambda u: int(u)):
        labels_blob = _fetch_xgm_labels_blob(mail, uid)
        if needle1 not in labels_blob and needle2 not in labels_blob:
            out.add(uid)
    return out


def _career_inbox_unprocessed_uids(mail: imaplib.IMAP4_SSL) -> set:
    """
    INBOX UIDs in the lookback window that do not yet have PROCESSED-CAREER or FORWARDED-CAREER.

    Uses only standard ``UID SEARCH SINCE`` plus ``FETCH X-GM-LABELS`` (no
    ``X-GM-RAW``). Many Python + Gmail setups return ``BAD Could not parse
    command`` for ``X-GM-RAW``, which produced noisy warnings and skipped work.
    """
    logger.info(
        f"  Inbox scan: SINCE last {LOOKBACK_DAYS} days, "
        f"excluding label {GMAIL_LABEL_PROCESSED!r} and {GMAIL_LABEL_FORWARDED!r} (X-GM-LABELS)…"
    )
    return _inbox_uids_since_without_processed_label(
        mail, LOOKBACK_DAYS, GMAIL_LABEL_PROCESSED, GMAIL_LABEL_FORWARDED
    )


def _imap_uid_copy_to_label(mail: imaplib.IMAP4_SSL, uid: bytes, label: str) -> bool:
    try:
        typ, _ = mail.uid("COPY", uid, label)
        return typ == "OK"
    except Exception as e:
        logger.warning(f"  UID COPY {uid!r} → {label!r}: {e}")
        return False


def _imap_uid_remove_label(mail: imaplib.IMAP4_SSL, uid: bytes, label: str) -> bool:
    try:
        typ, _ = mail.uid("STORE", uid, "-X-GM-LABELS", label)
        return typ == "OK"
    except Exception as e:
        logger.warning(f"  UID STORE/REMOVE LABEL {uid!r} ➔ {label!r}: {e}")
        return False


def _tag_career_message(
    mail: imaplib.IMAP4_SSL,
    uid: bytes,
    *outcome_labels: str,
) -> None:
    """Apply PROCESSED-CAREER plus outcome labels (BOUNCE-CAREER, NON-CAREER, …)."""
    for mbox in (GMAIL_LABEL_PROCESSED,) + outcome_labels:
        if not _imap_uid_copy_to_label(mail, uid, mbox):
            logger.warning(f"  Could not add label {mbox!r} to UID {uid!r}")


def _imap_uid_header_fetch_map(data) -> dict:
    """Parse ``UID FETCH`` header response: uid bytes → raw header bytes."""
    out = {}
    if not data:
        return out
    for part in data:
        if not isinstance(part, tuple) or len(part) < 2:
            continue
        meta, lit = part[0], part[1]
        if not isinstance(meta, bytes) or not isinstance(lit, bytes):
            continue
        m = re.search(rb"\bUID (\d+)\b", meta)
        if m:
            uid_b = m.group(1)
        else:
            m2 = re.match(rb"^(\d+) \(", meta)
            if not m2:
                continue
            uid_b = m2.group(1)
        out[uid_b] = lit
    return out


def _career_autoreply_hint(msg) -> bool:
    """True if subject looks like OOO / auto-reply (for AUTOREPLY-CAREER label)."""
    subj = (msg.get("Subject") or "").lower()
    return (
        "out of office" in subj
        or "auto-reply" in subj
        or "auto reply" in subj
        or "automatic reply" in subj
    )


# =============================================
# BOUNCE DETECTION HELPERS
# =============================================

def is_bounce(msg) -> bool:
    """Check if an email message is a bounce/NDR."""
    sender  = (msg.get("From", "") or "").lower()
    subject = (msg.get("Subject", "") or "").lower()

    for pattern in BOUNCE_SENDERS:
        if pattern in sender:
            return True
    for pattern in BOUNCE_SUBJECTS:
        if pattern in subject:
            return True
    return False


def extract_bounced_addresses(msg, known_addresses: set) -> list:
    """
    Extract the original recipient address from a bounce email.
    Looks in dedicated headers first, then walks the body parts.
    Only returns addresses we actually sent to (matched against known_addresses).
    """
    found = []

    # Check headers first (fast path)
    for header in ["X-Failed-Recipients", "Final-Recipient", "Original-Recipient"]:
        val = msg.get(header, "") or ""
        for addr in re.findall(r'[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}', val):
            if addr.lower() in known_addresses:
                found.append(addr.lower())

    # Walk body parts if headers didn't resolve it
    if not found:
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("text/plain", "message/delivery-status"):
                try:
                    body = part.get_payload(decode=True)
                    if body:
                        body_str = body.decode("utf-8", errors="replace")
                        for addr in re.findall(
                            r'[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}', body_str
                        ):
                            if addr.lower() in known_addresses:
                                found.append(addr.lower())
                except Exception:
                    pass

    return list(set(found))


# =============================================
# CAREER-REPLY FORWARDING HELPERS
# =============================================

def is_system_or_bounce_msg(msg) -> bool:
    """
    Return True if the message is a bounce / system / auto-reply / automated
    notification — these should never be forwarded as career replies.
    """
    _, sender_email = parseaddr(msg.get("From", "") or "")
    sender  = sender_email.lower()
    subject = (msg.get("Subject", "") or "").lower()

    if subject == "":           # empty subject = likely system mail
        return True

    # Bounce / NDR senders
    for pattern in BOUNCE_SENDERS:
        if pattern in sender:
            return True

    # Automated platform / notification senders (tools, CRMs, SaaS mailers)
    for prefix in AUTOMATED_SENDER_PREFIXES:
        if sender.startswith(prefix):
            return True

    # Bounce / auto-reply subject lines
    for pattern in BOUNCE_SUBJECTS:
        if pattern in subject:
            return True

    return False


def is_career_related(subject: str, body: str) -> bool:
    """
    Return True ONLY if the email is a genuine career / job-application reply.

    Rules (in order):
      1. 'Sandeep Jain' anywhere in subject → always forward (personal reply).
      2. ANY junk keyword in subject → never forward (marketing/sales tool).
      3. Career keyword in SUBJECT → forward (strong signal).
      4. Career keyword in body but NOT in subject → do NOT forward.
         Body-only matches produce too many false positives from marketing
         emails whose bodies happen to contain words like 'opportunity'.
    """
    subject_lower = subject.lower()
    body_lower    = body.lower()

    # Rule 1 — personal reply addressed to Sandeep
    if "sandeep jain" in subject_lower or "sandeep jain" in body_lower:
        return True

    # Rule 2 — junk/marketing subject → hard skip
    for word in JUNK_KEYWORDS:
        if word in subject_lower:
            return False

    # Rule 3 — career keyword must appear in the SUBJECT
    for word in CAREER_KEYWORDS:
        if word in subject_lower:
            return True

    # Rule 4 — body-only match is not enough
    return False


def extract_body(msg, html: bool = False) -> str:
    """Extract plain-text or HTML body from an email message."""
    target_type = "text/html" if html else "text/plain"
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == target_type
                    and not str(part.get("Content-Disposition", "")).startswith("attachment")):
                payload = part.get_payload(decode=True)
                if payload:
                    body += payload.decode(errors="replace") + "\n"
    else:
        if msg.get_content_type() == target_type:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(errors="replace")
    return body


def create_forward_message(original_msg, from_addr: str, to_addr: str) -> EmailMessage:
    """Build a forwarded EmailMessage from the original."""
    fwd = EmailMessage()
    orig_subject = (original_msg.get("Subject", "No Subject") or "No Subject")
    orig_subject = orig_subject.replace("\r", "").replace("\n", "")
    fwd["Subject"] = f"Fwd: {orig_subject}"
    fwd["From"]    = from_addr
    fwd["To"]      = to_addr

    comment = (
        f"--- Automated Forwarding from {from_addr} ---\n"
        f"Original Sender: {original_msg.get('From', 'Unknown')}\n"
        f"Date: {original_msg.get('Date', 'Unknown')}\n"
    )

    plain_body = extract_body(original_msg, html=False)
    html_body  = extract_body(original_msg, html=True)

    if not plain_body and html_body:
        fwd.set_content(comment + "\n[Original email was HTML. See below.]")
        comment_html = comment.replace("\n", "<br>")
        fwd.add_alternative(
            f"<div style='background:#f0f0f0;padding:10px;margin-bottom:10px;'>"
            f"<b>{comment_html}</b></div>{html_body}",
            subtype="html"
        )
    elif plain_body and html_body:
        fwd.set_content(comment + "\n\n" + plain_body)
        comment_html = comment.replace("\n", "<br>")
        fwd.add_alternative(
            f"<div style='background:#f0f0f0;padding:10px;margin-bottom:10px;'>"
            f"<b>{comment_html}</b></div>{html_body}",
            subtype="html"
        )
    else:
        fwd.set_content(comment + "\n\n" + (
            plain_body if plain_body else "[Could not decode original email body]"
        ))

    return fwd


# =============================================
# PER-ACCOUNT PROCESSING
# =============================================

def process_account(gmail_address: str, app_password: str,
                    known_addresses: set,
                    conn: sqlite3.Connection,
                    execution_id: int,
                    account_progress: str = "") -> dict:
    """
    One Gmail account: unified INBOX sweep (no UNSEEN / \\Seen).

    Messages in ``newer_than:LOOKBACK_DAYS in:inbox`` without PROCESSED-CAREER
    are header-scanned; bounce candidates get a full fetch for DB updates;
    others get a career pass (forward + labels) unless this account is the
    target inbox (tag only).

    Returns: bounces_detected, bounces_new, forwards_sent, errors
    """
    stats = dict(bounces_detected=0, bounces_new=0, forwards_sent=0, errors=0)
    ap = f"{account_progress} " if account_progress else ""

    try:
        logger.info(f"\n  {ap}Connecting to {gmail_address}...")
        mail = _imap_connect(gmail_address, app_password)
        ensure_gmail_labels(mail)
    except imaplib.IMAP4.error as e:
        logger.error(f"  IMAP auth error for {gmail_address}: {e}")
        stats["errors"] += 1
        return stats
    except Exception as e:
        logger.error(f"  Connection error for {gmail_address}: {e}")
        stats["errors"] += 1
        return stats

    skip_career_forward = gmail_address.lower() == TARGET_FORWARD_EMAIL.lower()
    if skip_career_forward:
        logger.info(
            f"  {ap}This account is TARGET_FORWARD_EMAIL — career forwards skipped; "
            "unlabeled mail still tagged NON-CAREER."
        )

    message_uids = _career_inbox_unprocessed_uids(mail)
    logger.info(
        f"  {ap}INBOX without {GMAIL_LABEL_PROCESSED!r}: {len(message_uids)} UID(s) "
        f"(newer_than:{LOOKBACK_DAYS}d)"
    )

    ids_list = sorted(message_uids, key=lambda u: int(u))
    reconnects = 0
    tagged_non_career_headers = 0

    HDRFETCH = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT AUTO-SUBMITTED X-FAILED-RECIPIENTS)])"
    bounce_uids: list = []
    career_uids: list = []

    logger.info(
        f"  {ap}Pass 1: headers for {len(ids_list)} email(s) "
        f"(batch size {HEADER_FETCH_BATCH})..."
    )

    def _single_uid_header(uid_b: bytes):
        typ, hdr_data = mail.uid("FETCH", uid_b, HDRFETCH)
        if typ != "OK" or not hdr_data or not isinstance(hdr_data[0], tuple):
            return None
        raw = hdr_data[0][1]
        return raw if isinstance(raw, bytes) else None

    bstart = 0
    while bstart < len(ids_list):
        batch = ids_list[bstart : bstart + HEADER_FETCH_BATCH]
        try:
            logger.info(
                f"    … {min(bstart + HEADER_FETCH_BATCH, len(ids_list))}/"
                f"{len(ids_list)} headers"
            )
            chunk = b",".join(batch)
            typ, hdr_data = mail.uid("FETCH", chunk, HDRFETCH)
            hmap = _imap_uid_header_fetch_map(hdr_data) if typ == "OK" and hdr_data else {}
        except (ssl.SSLError, OSError, imaplib.IMAP4.abort) as e:
            if reconnects >= 3:
                logger.warning(
                    f"  Reconnect limit (pass1); skipping batch @ {bstart}: {e}"
                )
                bstart += HEADER_FETCH_BATCH
                reconnects = 0
                continue
            reconnects += 1
            logger.warning(f"  Reconnecting ({reconnects}/3)...")
            time.sleep(2 * reconnects)
            try:
                mail = _imap_connect(gmail_address, app_password)
                ensure_gmail_labels(mail)
            except Exception as re_err:
                logger.error(f"  Reconnect failed: {re_err}")
                break
            continue
        except Exception as e:
            logger.warning(f"  Batch header FETCH error @ {bstart}: {e}")
            hmap = {}

        for uid in batch:
            raw_hdr = hmap.get(uid)
            if raw_hdr is None:
                try:
                    raw_hdr = _single_uid_header(uid)
                except Exception as ex:
                    logger.warning(f"  Header read error {uid!r}: {ex}")
                    continue
            if raw_hdr is None:
                continue

            h        = email.message_from_bytes(raw_hdr)
            sender   = (h.get("From", "") or "").lower()
            subject  = (h.get("Subject", "") or "").lower()
            failed_r = (h.get("X-Failed-Recipients", "") or "").lower()

            is_bounce_hdr = (
                any(p in sender for p in BOUNCE_SENDERS)
                or any(p in subject for p in BOUNCE_SUBJECTS)
                or bool(failed_r)
            )
            if is_bounce_hdr:
                bounce_uids.append(uid)
            else:
                career_uids.append(uid)

        bstart += HEADER_FETCH_BATCH
        reconnects = 0

    logger.info(
        f"  {ap}Pass 1 done — bounce branch: {len(bounce_uids)}, "
        f"career branch: {len(career_uids)}"
    )

    # ── Pass 2a: bounces (BODY.PEEK[]) ───────────────────────────
    reconnects = 0
    i = 0
    while i < len(bounce_uids):
        uid = bounce_uids[i]
        try:
            _, data = mail.uid("FETCH", uid, "(BODY.PEEK[])")
            if not data or not isinstance(data[0], tuple):
                i += 1
                continue
            raw = data[0][1]
            if not isinstance(raw, bytes):
                i += 1
                continue

            msg = email.message_from_bytes(raw)

            if is_bounce(msg):
                addresses = extract_bounced_addresses(msg, known_addresses)
                if addresses:
                    for addr in addresses:
                        logger.info(f"  {ap}📧 Bounce detected: {addr}")
                        stats["bounces_detected"] += 1
                        if mark_bounced(conn, addr):
                            stats["bounces_new"] += 1
                            logger.info(f"  {ap}✓  Marked bounced: {addr}")
                        else:
                            logger.info(f"  {ap}⏭️  Already marked: {addr}")
                    _tag_career_message(mail, uid, GMAIL_LABEL_BOUNCE)
                else:
                    _tag_career_message(mail, uid, GMAIL_LABEL_BOUNCE_UNMATCHED)
            else:
                _tag_career_message(mail, uid, GMAIL_LABEL_NON_CAREER)

            i += 1

        except (ssl.SSLError, OSError, imaplib.IMAP4.abort) as e:
            if reconnects >= 3:
                logger.warning(f"  Reconnect limit (pass2a); skipping {uid!r}: {e}")
                i += 1
                continue
            reconnects += 1
            logger.warning(f"  Reconnecting ({reconnects}/3)...")
            time.sleep(2 * reconnects)
            try:
                mail = _imap_connect(gmail_address, app_password)
                ensure_gmail_labels(mail)
            except Exception as re_err:
                logger.error(f"  Reconnect failed: {re_err}")
                break
        except Exception as e:
            logger.warning(f"  Could not read bounce UID {uid!r}: {e}")
            i += 1

    # ── Pass 2b: career (BODY.PEEK[]) ────────────────────────────
    smtp_conn = None
    if career_uids and not skip_career_forward:
        try:
            smtp_conn = smtplib.SMTP_SSL(
                SMTP_HOST, SMTP_PORT, context=_make_ssl_context()
            )
            smtp_conn.login(gmail_address, app_password)
        except Exception as smtp_err:
            logger.error(f"  SMTP connect failed for {gmail_address}: {smtp_err}")
            smtp_conn = None

    forward_count = 0
    reconnects = 0
    i = 0
    while i < len(career_uids):
        uid = career_uids[i]
        try:
            if skip_career_forward:
                _tag_career_message(mail, uid, GMAIL_LABEL_NON_CAREER)
                tagged_non_career_headers += 1
                i += 1
                continue

            # 1. Fetch labels to check for FWD-PEND-CAREER
            labels_blob = _fetch_xgm_labels_blob(mail, uid)
            has_fwd_pending = GMAIL_LABEL_FWD_PENDING.encode("utf-8") in labels_blob

            # 2. Fetch email data
            _, msg_data = mail.uid("FETCH", uid, "(BODY.PEEK[])")
            if not msg_data or not isinstance(msg_data[0], tuple):
                i += 1
                continue
            raw_email = msg_data[0][1]
            if not isinstance(raw_email, bytes):
                i += 1
                continue

            email_message = email.message_from_bytes(raw_email)
            orig_subject = (
                email_message.get("Subject", "No Subject") or "No Subject"
            )
            orig_subject = orig_subject.replace("\r", "").replace("\n", "")
            orig_sender = email_message.get("From", "Unknown")

            if already_forwarded(conn, gmail_address, orig_subject, orig_sender):
                logger.info(f"  {ap}⏭️  Already forwarded: {orig_subject}")
                _tag_career_message(mail, uid, GMAIL_LABEL_NON_CAREER)
                if has_fwd_pending:
                    _imap_uid_remove_label(mail, uid, GMAIL_LABEL_FWD_PENDING)
                i += 1
                continue

            # A. If it was already labeled as FWD-PEND-CAREER in a previous execution:
            if has_fwd_pending:
                if forward_count >= MAX_FORWARDS_PER_ACCOUNT:
                    logger.info(
                        f"  {ap}🛑 Forward limit reached ({MAX_FORWARDS_PER_ACCOUNT}) — "
                        f"keeping {orig_subject} as {GMAIL_LABEL_FWD_PENDING} for next run."
                    )
                    i += 1
                    continue
                
                if smtp_conn is None:
                    logger.warning(f"  ⚠️  No SMTP — cannot forward pending: {orig_subject}")
                    stats["errors"] += 1
                    i += 1
                    continue

                logger.info(f"  {ap}➔  Forwarding PENDING reply ({GMAIL_LABEL_REPLY} → {TARGET_FORWARD_EMAIL}): {orig_subject}")
                fwd_msg = create_forward_message(email_message, gmail_address, TARGET_FORWARD_EMAIL)
                smtp_conn.send_message(fwd_msg)
                log_processed(conn, execution_id, gmail_address, orig_sender, orig_subject, "forwarded")
                stats["forwards_sent"] += 1
                forward_count += 1
                
                # Apply PROCESSED-CAREER, FORWARDED-CAREER, REPLY-CAREER, and REMOVE FWD-PEND-CAREER
                _tag_career_message(mail, uid, GMAIL_LABEL_FORWARDED, GMAIL_LABEL_REPLY)
                _imap_uid_remove_label(mail, uid, GMAIL_LABEL_FWD_PENDING)

                if forward_count < MAX_FORWARDS_PER_ACCOUNT:
                    logger.info(f"  {ap}⏳ Waiting {FORWARD_DELAY_SECS}s before next forward...")
                    time.sleep(FORWARD_DELAY_SECS)
                
                i += 1
                continue

            # B. If not previously pending, run the checks & LLM classification:
            if is_system_or_bounce_msg(email_message):
                logger.info(f"  {ap}⏭️  Skipped system/bounce: {orig_subject}")
                log_processed(
                    conn, execution_id, gmail_address,
                    orig_sender, orig_subject, "skipped_bounce",
                )
                if _career_autoreply_hint(email_message):
                    _tag_career_message(mail, uid, GMAIL_LABEL_AUTOREPLY)
                else:
                    _tag_career_message(mail, uid, GMAIL_LABEL_NON_CAREER)
                i += 1
                continue

            plain_text = extract_body(email_message, html=False)
            html_text  = extract_body(email_message, html=True)
            combined   = plain_text + " " + html_text

            has_reply_headers = bool(email_message.get("In-Reply-To") or email_message.get("References"))
            
            # Classification
            if not NVIDIA_KEYS:
                # Heuristic fallback
                if is_career_related(orig_subject, combined):
                    classification = {
                        "should_forward": True,
                        "is_reply": True,
                        "is_auto_response": False,
                        "is_decline": False,
                        "reason": "Heuristic fallback (no LLM keys)"
                    }
                else:
                    classification = {
                        "should_forward": False,
                        "is_reply": False,
                        "is_auto_response": False,
                        "is_decline": False,
                        "reason": "Heuristic fallback (no LLM keys)"
                    }
            else:
                logger.info(f"  {ap}🤖 Classifying reply via Nvidia LLM (Llama 3.3): {orig_subject}...")
                classification = classify_email_via_llm(orig_subject, orig_sender, combined, has_reply_headers)
                logger.info(f"    - LLM Result: should_forward={classification['should_forward']}, reply={classification['is_reply']}, auto={classification['is_auto_response']}, decline={classification['is_decline']}, reason={classification['reason']}")

            if classification["should_forward"]:
                if forward_count >= MAX_FORWARDS_PER_ACCOUNT:
                    logger.info(
                        f"  {ap}🛑 Limit reached ({MAX_FORWARDS_PER_ACCOUNT}) — "
                        f"tagging {orig_subject} with {GMAIL_LABEL_FWD_PENDING} for next run."
                    )
                    _imap_uid_copy_to_label(mail, uid, GMAIL_LABEL_FWD_PENDING)
                    log_processed(
                        conn, execution_id, gmail_address,
                        orig_sender, orig_subject, "pending_fwd_limit",
                    )
                    i += 1
                    continue

                if smtp_conn is None:
                    logger.warning(f"  ⚠️  No SMTP — cannot forward: {orig_subject}")
                    stats["errors"] += 1
                    i += 1
                    continue

                logger.info(
                    f"  {ap}➔  Forwarding approved reply (REPLY-CAREER → {TARGET_FORWARD_EMAIL}): "
                    f"{orig_subject}  (from {orig_sender})"
                )
                fwd_msg = create_forward_message(
                    email_message, gmail_address, TARGET_FORWARD_EMAIL
                )
                smtp_conn.send_message(fwd_msg)
                log_processed(
                    conn, execution_id, gmail_address,
                    orig_sender, orig_subject, "forwarded",
                )
                stats["forwards_sent"] += 1
                forward_count += 1
                
                # Apply PROCESSED-CAREER, FORWARDED-CAREER, REPLY-CAREER
                _tag_career_message(mail, uid, GMAIL_LABEL_FORWARDED, GMAIL_LABEL_REPLY)

                if forward_count < MAX_FORWARDS_PER_ACCOUNT:
                    logger.info(
                        f"  {ap}⏳ Waiting {FORWARD_DELAY_SECS}s before next forward..."
                    )
                    time.sleep(FORWARD_DELAY_SECS)
            else:
                log_processed(
                    conn, execution_id, gmail_address,
                    orig_sender, orig_subject, f"skipped_llm_{classification['reason'][:30]}",
                )
                if classification["is_auto_response"] or _career_autoreply_hint(email_message):
                    _tag_career_message(mail, uid, GMAIL_LABEL_AUTOREPLY)
                else:
                    _tag_career_message(mail, uid, GMAIL_LABEL_NON_CAREER)

            i += 1

        except (ssl.SSLError, OSError, imaplib.IMAP4.abort) as e:
            if reconnects >= 3:
                logger.warning(f"  Reconnect limit (pass2b); skipping {uid!r}: {e}")
                i += 1
                continue
            reconnects += 1
            logger.warning(f"  Reconnecting ({reconnects}/3)...")
            time.sleep(2 * reconnects)
            try:
                mail = _imap_connect(gmail_address, app_password)
                ensure_gmail_labels(mail)
            except Exception as re_err:
                logger.error(f"  Reconnect failed: {re_err}")
                break
        except Exception as e:
            logger.error(f"  Error processing career UID {uid!r}: {e}")
            stats["errors"] += 1
            i += 1

    if smtp_conn:
        try:
            smtp_conn.quit()
        except Exception:
            pass

    if skip_career_forward and tagged_non_career_headers:
        logger.info(
            f"  {ap}Tagged {tagged_non_career_headers} career-branch UID(s) as NON-CAREER "
            f"(target inbox)"
        )

    try:
        mail.logout()
    except Exception:
        pass

    return stats


def print_db_summary_to_logger(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute("SELECT * FROM v_validation_pipeline_summary").fetchone()
        if not row:
            return
        names = [d[0] for d in conn.execute("SELECT * FROM v_validation_pipeline_summary").description]
        logger.info("")
        logger.info("=" * 60)
        logger.info("  DATABASE PIPELINE SUMMARY (v_validation_pipeline_summary)")
        logger.info("=" * 60)
        for name, val in zip(names, row):
            logger.info(f"  {name:<30}: {val:,}" if isinstance(val, int) else f"  {name:<30}: {val}")
        logger.info("=" * 60)
    except Exception as e:
        logger.warning(f"Could not print database validation summary: {e}")


# =============================================
# MAIN
# =============================================

def main():
    os.system("cls" if os.name == "nt" else "clear")
    logger.info("=" * 60)
    logger.info("  BOUNCE CHECKER + CAREER-REPLY FORWARDER")
    logger.info(f"  Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # Load config
    if not os.path.exists(SMTP_CONFIG_FILE):
        logger.error(f"Config not found: {SMTP_CONFIG_FILE}")
        return

    with open(SMTP_CONFIG_FILE) as f:
        config = json.load(f)
    smtp_passwords = config.get("profiles", {})
    profile_list = list(smtp_passwords.items())
    total_profiles = len(profile_list)
    logger.info(
        f"  {total_profiles} Gmail account(s) to process (order = email_config.json)"
    )

    # Open DB
    conn = open_db()
    known_addresses = get_all_sent_addresses(conn)
    logger.info(f"  {len(known_addresses)} email addresses in campaign DB")

    if not known_addresses:
        logger.info("  No sent emails in DB — run a send campaign first.")

    # Log this run
    cur = conn.execute(
        "INSERT INTO send_runs (script, started_at) VALUES ('bounce_checker', ?)",
        (datetime.now().isoformat(),)
    )
    conn.commit()
    bounce_run_id = cur.lastrowid

    # Also create an execution record for the forwarding tracker
    cur2 = conn.execute(
        "INSERT INTO processed_emails (execution_id, account, original_sender, "
        "subject, status, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (None, "__run_start__", "", "", "run_start", datetime.now().isoformat())
    )
    # We'll use bounce_run_id as execution_id throughout
    conn.commit()

    # ── Process each account ───────────────────────────────────
    total_bounces_detected = 0
    total_bounces_new      = 0
    total_forwards         = 0
    total_errors           = 0

    for sr_no, (gmail_address, app_password) in enumerate(profile_list, start=1):
        prog = _profile_progress_label(sr_no, total_profiles)
        logger.info(f"\n{'─'*55}")
        logger.info(f"  {prog} Account: {gmail_address}")
        logger.info(f"{'─'*55}")

        result = process_account(
            gmail_address, app_password,
            known_addresses, conn, bounce_run_id,
            account_progress=prog,
        )
        total_bounces_detected += result["bounces_detected"]
        total_bounces_new      += result["bounces_new"]
        total_forwards         += result["forwards_sent"]
        total_errors           += result["errors"]

    # ── Summary ────────────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("  SUMMARY")
    logger.info(f"  Bounces found     : {total_bounces_detected}")
    logger.info(f"  New in DB         : {total_bounces_new}")
    logger.info(f"  Already marked    : {total_bounces_detected - total_bounces_new}")
    logger.info(f"  Emails forwarded  : {total_forwards}  →  {TARGET_FORWARD_EMAIL}")
    logger.info(f"  Errors            : {total_errors}")
    logger.info("=" * 60)

    conn.execute("""
        UPDATE send_runs
        SET finished_at = ?, bounces_found = ?, notes = ?
        WHERE id = ?
    """, (
        datetime.now().isoformat(),
        total_bounces_new,
        f"{total_bounces_detected} bounce emails found, {total_bounces_new} new, "
        f"{total_forwards} career replies forwarded",
        bounce_run_id
    ))
    conn.commit()

    if total_bounces_new > 0:
        logger.info("\n  Next send run will skip bounced formats and try the next one.")

        cur = conn.execute("""
            SELECT ea.employee_name, ea.company_name, ea.company_domain,
                   ea.email_format AS bounced_format
            FROM   email_attempts ea
            WHERE  ea.status = 'bounced'
            ORDER  BY ea.company_name
        """)
        rows = cur.fetchall()
        if rows:
            logger.info(f"\n  Contacts to retry ({len(rows)}):")
            for row in rows:
                logger.info(
                    f"    {row['employee_name']} @ {row['company_domain']}"
                    f"  (bounced: {row['bounced_format']})"
                )

    if total_forwards > 0:
        logger.info(f"\n  {total_forwards} career reply email(s) forwarded to "
                    f"{TARGET_FORWARD_EMAIL} — check your inbox.")

    print_db_summary_to_logger(conn)
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Cancelled.")
    except Exception as e:
        import traceback
        logger.error(f"Fatal error: {e}")
        traceback.print_exc()
    finally:
        if sys.stdin and sys.stdin.isatty():
            input("\n  Press Enter to exit...")
