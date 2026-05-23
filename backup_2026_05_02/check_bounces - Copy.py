#!/usr/bin/env python3
"""
check_bounces.py
================
Connects to each Gmail account via IMAP, finds bounce/NDR emails,
and marks the corresponding email_attempts records as 'bounced'
in linkedin_data.db.

Run this once a day, the morning after a send run.

Config files needed (same folder):
  email_config.json   — Gmail App Passwords
  linkedin_data.db    — the scraper database

After running, the next send_linkedin_campaigns.py run will
automatically skip bounced formats and try the next one.
"""

import imaplib
import email
import json
import os
import re
import sqlite3
import sys
import logging
from datetime import datetime

# =============================================
# CONFIG
# =============================================
_SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
SMTP_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "email_config.json")
DB_PATH          = os.path.join(_SCRIPT_DIR, "linkedin_data.db")

# Each run gets its own timestamped log file inside a logs/ subfolder
_LOG_DIR  = os.path.join(_SCRIPT_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
LOG_FILE  = os.path.join(
    _LOG_DIR,
    f"check_bounces_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
)

# IMAP settings for Gmail
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Bounce sender patterns — emails from these indicate a bounce
BOUNCE_SENDERS = [
    "mailer-daemon@googlemail.com",
    "mailer-daemon@google.com",
    "postmaster@",
    "mailer-daemon@",
    "noreply@",
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
]

# =============================================
# LOGGING
# =============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
    ]
)
logger = logging.getLogger("bounce_checker")
logger.info(f"📄 Log file: {LOG_FILE}")

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
    """Return all email addresses we sent to."""
    cur = conn.execute("SELECT email_address FROM email_attempts WHERE status = 'sent'")
    return {row["email_address"].lower() for row in cur.fetchall()}

# =============================================
# IMAP / BOUNCE DETECTION
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
    Looks in headers and body for addresses we actually sent to.
    """
    found = []

    # Check headers first
    for header in ["X-Failed-Recipients", "Final-Recipient", "Original-Recipient"]:
        val = msg.get(header, "") or ""
        for addr in re.findall(r'[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}', val):
            if addr.lower() in known_addresses:
                found.append(addr.lower())

    # Walk the body parts
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


def check_inbox(gmail_address: str, app_password: str,
                known_addresses: set) -> list:
    """
    Connect to Gmail IMAP, find bounce emails, return list of bounced addresses.
    """
    bounced = []
    try:
        logger.info(f"  Connecting to {gmail_address}...")
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(gmail_address, app_password)
        mail.select("INBOX")

        # Search for unread messages from mailer-daemon or with bounce subjects
        search_terms = [
            'FROM "mailer-daemon"',
            'FROM "postmaster"',
            'SUBJECT "delivery status"',
            'SUBJECT "undeliverable"',
            'SUBJECT "mail delivery failed"',
            'SUBJECT "failure notice"',
        ]

        message_ids = set()
        for term in search_terms:
            try:
                _, ids = mail.search(None, term)
                if ids and ids[0]:
                    message_ids.update(ids[0].split())
            except Exception:
                pass

        logger.info(f"  Found {len(message_ids)} potential bounce email(s)")

        for msg_id in message_ids:
            try:
                _, data = mail.fetch(msg_id, "(RFC822)")
                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                if not is_bounce(msg):
                    continue

                addresses = extract_bounced_addresses(msg, known_addresses)
                for addr in addresses:
                    logger.info(f"  📧 Bounce detected: {addr}")
                    bounced.append(addr)

            except Exception as e:
                logger.warning(f"  Could not read message {msg_id}: {e}")

        mail.logout()

    except imaplib.IMAP4.error as e:
        logger.error(f"  IMAP error for {gmail_address}: {e}")
    except Exception as e:
        logger.error(f"  Error checking {gmail_address}: {e}")

    return bounced

# =============================================
# MAIN
# =============================================

def main():
    os.system("cls" if os.name == "nt" else "clear")
    logger.info("=" * 60)
    logger.info("  BOUNCE CHECKER")
    logger.info(f"  Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # Load SMTP config (has App Passwords for IMAP too)
    if not os.path.exists(SMTP_CONFIG_FILE):
        logger.error(f"Config not found: {SMTP_CONFIG_FILE}")
        return

    with open(SMTP_CONFIG_FILE) as f:
        config = json.load(f)
    smtp_passwords = config.get("profiles", {})
    logger.info(f"  {len(smtp_passwords)} Gmail account(s) to check")

    # Open DB
    conn = open_db()
    known_addresses = get_all_sent_addresses(conn)
    logger.info(f"  {len(known_addresses)} sent email addresses in DB")

    if not known_addresses:
        logger.info("  No sent emails to check against — run a send campaign first.")
        conn.close()
        return

    # Log this run
    cur = conn.execute(
        "INSERT INTO send_runs (script, started_at) VALUES ('bounce_checker', ?)"
        , (datetime.now().isoformat(),))
    conn.commit()
    bounce_run_id = cur.lastrowid

    # Check each Gmail inbox
    total_bounced = 0
    total_new     = 0

    for gmail_address, app_password in smtp_passwords.items():
        logger.info(f"\n  Checking: {gmail_address}")
        bounced_addrs = check_inbox(gmail_address, app_password, known_addresses)

        for addr in bounced_addrs:
            total_bounced += 1
            if mark_bounced(conn, addr):
                total_new += 1
                logger.info(f"  ✓ Marked bounced: {addr}")
            else:
                logger.info(f"  ⏭️  Already marked: {addr}")

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info(f"  Bounces found   : {total_bounced}")
    logger.info(f"  New in DB       : {total_new}")
    logger.info(f"  (already marked): {total_bounced - total_new}")
    logger.info("=" * 60)

    conn.execute("""
        UPDATE send_runs
        SET finished_at = ?, bounces_found = ?, notes = ?
        WHERE id = ?
    """, (datetime.now().isoformat(), total_new,
          f"{total_bounced} bounce emails found, {total_new} new", bounce_run_id))
    conn.commit()

    if total_new > 0:
        logger.info("\n  Next send run will automatically skip bounced formats")
        logger.info("  and try the next email format for those contacts.")

        # Show what will be retried
        cur = conn.execute("""
            SELECT ea.employee_name, ea.company_name, ea.company_domain,
                   ea.email_format as bounced_format
            FROM   email_attempts ea
            WHERE  ea.status = 'bounced'
            ORDER  BY ea.company_name
        """)
        rows = cur.fetchall()
        if rows:
            logger.info(f"\n  Contacts to retry ({len(rows)}):")
            for row in rows:
                logger.info(f"    {row['employee_name']} @ {row['company_domain']}"
                            f"  (bounced: {row['bounced_format']})")

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
        input("\n  Press Enter to exit...")
