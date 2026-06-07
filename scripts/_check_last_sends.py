#!/usr/bin/env python3
"""Temp: last campaign send per Gmail profile + validated-not-sent counts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "db" / "linkedin_data.db"
PROGRESS = ROOT / "data" / "json" / "email_progress_linkedin.json"
CONFIG_CANDIDATES = [
    Path(r"C:\Users\sandeep\Downloads\Claudes\EmailJson\email_config.json"),
    ROOT / "email_config.json",
    ROOT / "backup_2026_05_02" / "email_config.json",
]


def load_profiles() -> list[str]:
    for p in CONFIG_CANDIDATES:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            profiles = data.get("profiles") or {}
            return sorted(profiles.keys())
    return []


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(ts)


def main() -> None:
    if not DB.is_file():
        print(f"DB not found: {DB}")
        return

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 72)
    print("  VALIDATED BUT NOT IN email_attempts (campaign never recorded)")
    print("=" * 72)

    rows = conn.execute(
        """
        SELECT z.email_address, z.zb_status, z.mv_status
        FROM zerobounce_validation z
        WHERE (
            lower(trim(coalesce(z.zb_status, ''))) = 'valid'
            OR lower(trim(coalesce(z.mv_status, ''))) IN ('ok', 'valid', 'deliverable')
        )
        AND NOT EXISTS (
            SELECT 1 FROM email_attempts ea
            WHERE ea.email_address = z.email_address
        )
        ORDER BY z.email_address
        """
    ).fetchall()
    print(f"  Total allowlisted, no email_attempts row: {len(rows)}")

    sent_prog: set[str] = set()
    if PROGRESS.is_file():
        prog = json.loads(PROGRESS.read_text(encoding="utf-8"))
        sent_prog = {e.lower() for e in prog.get("sent_emails", [])}

    free, in_prog_only = [], []
    for r in rows:
        el = (r["email_address"] or "").lower()
        if el in sent_prog:
            in_prog_only.append(r)
        else:
            free.append(r)

    print(f"  Free (not in progress JSON either):     {len(free)}")
    print(f"  In progress JSON only (send drift):      {len(in_prog_only)}")
    print()
    if free:
        print("  Ready to send via pool sender:")
        for r in free:
            print(f"    {r['email_address']}  zb={r['zb_status']}  mv={r['mv_status']}")
    if in_prog_only:
        print()
        print("  Validated + no attempt row, but blocked by progress JSON:")
        for r in in_prog_only[:15]:
            print(f"    {r['email_address']}")
        if len(in_prog_only) > 15:
            print(f"    ... and {len(in_prog_only) - 15} more")

    print()
    print("=" * 72)
    print("  LAST CAMPAIGN SEND PER GMAIL ACCOUNT (email_attempts status=sent)")
    print("=" * 72)

    profiles = load_profiles()
    if not profiles:
        print("  (email_config.json not found — listing all from_profile in DB)")
        profiles = [
            r[0]
            for r in conn.execute(
                """
                SELECT DISTINCT from_profile FROM email_attempts
                WHERE from_profile IS NOT NULL AND trim(from_profile) != ''
                ORDER BY 1
                """
            ).fetchall()
        ]

    print(f"  Config profiles: {len(profiles)}")
    print()
    print(f"  {'Gmail account':<42} {'Last sent':<22} {'Sends':>6}  Last recipient")
    print("  " + "-" * 68)

    highlight = "jain1001sandeep@gmail.com"
    for email in profiles:
        row = conn.execute(
            """
            SELECT sent_timestamp, email_address
            FROM email_attempts
            WHERE lower(trim(from_profile)) = lower(?)
              AND lower(coalesce(status, '')) = 'sent'
            ORDER BY sent_timestamp DESC
            LIMIT 1
            """,
            (email,),
        ).fetchone()
        count = conn.execute(
            """
            SELECT count(*) FROM email_attempts
            WHERE lower(trim(from_profile)) = lower(?)
              AND lower(coalesce(status, '')) = 'sent'
            """,
            (email,),
        ).fetchone()[0]
        if row:
            last_ts = fmt_ts(row["sent_timestamp"])
            last_to = row["email_address"] or ""
        else:
            last_ts = "never"
            last_to = ""
        mark = " <<" if email.lower() == highlight.lower() else ""
        print(f"  {email:<42} {last_ts:<22} {count:>6}  {last_to}{mark}")

    print()
    print("=" * 72)
    print("  OVERALL DB SEND STATS")
    print("=" * 72)
    overall = conn.execute(
        """
        SELECT sent_timestamp, from_profile, email_address
        FROM email_attempts
        WHERE lower(coalesce(status, '')) = 'sent'
        ORDER BY sent_timestamp DESC
        LIMIT 1
        """
    ).fetchone()
    if overall:
        print(f"  Last send in DB (any account): {fmt_ts(overall['sent_timestamp'])}")
        print(f"    from: {overall['from_profile']}")
        print(f"    to:   {overall['email_address']}")
    else:
        print("  No sent rows in email_attempts.")

    sent_total = conn.execute(
        "SELECT count(*) FROM email_attempts WHERE lower(coalesce(status,''))='sent'"
    ).fetchone()[0]
    bounced = conn.execute(
        "SELECT count(*) FROM email_attempts WHERE lower(coalesce(status,''))='bounced'"
    ).fetchone()[0]
    print(f"  Total sent rows:    {sent_total:,}")
    print(f"  Total bounced rows: {bounced:,}")

    if PROGRESS.is_file():
        prog = json.loads(PROGRESS.read_text(encoding="utf-8"))
        print(f"  Progress JSON sent_emails count: {len(prog.get('sent_emails', [])):,}")
        print(f"  Progress total_sent counter:     {prog.get('total_sent', 'n/a')}")

    run_row = conn.execute(
        """
        SELECT script, started_at, finished_at, notes
        FROM send_runs
        WHERE script LIKE '%send%' OR script LIKE '%campaign%'
        ORDER BY id DESC LIMIT 5
        """
    ).fetchall()
    if not run_row:
        run_row = conn.execute(
            "SELECT script, started_at, finished_at, notes FROM send_runs ORDER BY id DESC LIMIT 5"
        ).fetchall()
    if run_row:
        print()
        print("  Recent send_runs:")
        for r in run_row:
            print(f"    {r['started_at']}  {r['script']}  {r['notes'] or ''}")

    conn.close()
    print("=" * 72)


if __name__ == "__main__":
    main()
