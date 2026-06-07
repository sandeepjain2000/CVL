#!/usr/bin/env python3
"""
Create validation pipeline views in linkedin_data.db and sync employee_email_state
from zeroclone CSV (required for cascade views).

Usage:
  python scripts/apply_validation_views.py
  python scripts/apply_validation_views.py --db path/to/linkedin_data.db
  python scripts/apply_validation_views.py --no-sync-state
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from pipeline_summary_cache import ensure_results_table, refresh_pipeline_summary_snapshot

DEFAULT_DB = ROOT / "data" / "db" / "linkedin_data.db"
SQL_FILE = ROOT / "sql" / "validation_pipeline_views.sql"
RESULTS_SQL_FILE = ROOT / "sql" / "pipeline_summary_results.sql"
DEFAULT_STATE_CSV = (
    ROOT.parent / "zeroclone" / "cycles" / "state" / "employee_email_state.csv"
)

EMPLOYEE_STATE_DDL = """
CREATE TABLE IF NOT EXISTS employee_email_state (
    employee_key                    TEXT PRIMARY KEY,
    employee_id                     TEXT,
    company_name                    TEXT,
    full_name                       TEXT,
    first_name                      TEXT,
    last_name                       TEXT,
    company_domain                  TEXT,
    email_format                    TEXT,
    email                           TEXT,
    validation_status               TEXT,
    validation_reason               TEXT,
    resolved_valid_email            TEXT,
    last_updated                    TEXT,
    format_firstname_lastname_status TEXT,
    format_firstname_lastname_email TEXT,
    format_firstname_status         TEXT,
    format_firstname_email          TEXT,
    format_firstinitial_lastname_status TEXT,
    format_firstinitial_lastname_email TEXT,
    format_firstname_lastinitial_status TEXT,
    format_firstname_lastinitial_email TEXT
);
"""


def sync_employee_state(conn: sqlite3.Connection, csv_path: Path) -> int:
    if not csv_path.is_file():
        print(f"  (skip sync — CSV not found: {csv_path})")
        return 0
    conn.executescript(EMPLOYEE_STATE_DDL)
    conn.execute("DELETE FROM employee_email_state")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return 0
        cols = [c for c in reader.fieldnames if c]
        placeholders = ",".join("?" * len(cols))
        col_sql = ",".join(cols)
        rows = [tuple(row.get(c, "") for c in cols) for row in reader]
    conn.executemany(
        f"INSERT INTO employee_email_state ({col_sql}) VALUES ({placeholders})",
        rows,
    )
    conn.commit()
    return len(rows)


VIEW_DROP_ORDER = [
    "Employees_with0_Valid_emails",
    "v_validation_pipeline_summary",
    "v_employee_validation_status",
    "v_allowlist_send_status",
    "v_validated_pool_sendable",
    "v_zerobounce_allowlisted",
    "v_scrapeable_employees",
    "v_companies_with_domain",
]


def apply_views(conn: sqlite3.Connection, sql_path: Path) -> None:
    for name in VIEW_DROP_ORDER:
        conn.execute(f"DROP VIEW IF EXISTS {name}")
    sql = sql_path.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()


def print_summary(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT * FROM v_validation_pipeline_summary").fetchone()
    if not row:
        return
    names = [d[0] for d in conn.execute("SELECT * FROM v_validation_pipeline_summary").description]
    print("\n=== v_validation_pipeline_summary ===")
    for name, val in zip(names, row):
        print(f"  {name}: {val:,}" if isinstance(val, int) else f"  {name}: {val}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--state-csv", type=Path, default=DEFAULT_STATE_CSV)
    p.add_argument("--no-sync-state", action="store_true")
    args = p.parse_args()

    if not args.db.is_file():
        print(f"Database not found: {args.db}", file=sys.stderr)
        return 1
    if not SQL_FILE.is_file():
        print(f"SQL file not found: {SQL_FILE}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    try:
        if not args.no_sync_state:
            n = sync_employee_state(conn, args.state_csv)
            print(f"Synced {n} rows into employee_email_state from CSV.")
        apply_views(conn, SQL_FILE)
        print(f"Applied views from {SQL_FILE}")
        if RESULTS_SQL_FILE.is_file():
            ensure_results_table(conn)
            print(f"Ensured results table from {RESULTS_SQL_FILE}")
        views = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name LIKE 'v_%' ORDER BY 1"
        ).fetchall()
        print("Views:", ", ".join(v[0] for v in views))
        refreshed_at = refresh_pipeline_summary_snapshot(conn)
        print(f"Saved pipeline summary snapshot at {refreshed_at}")
        print_summary(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
