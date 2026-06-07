#!/usr/bin/env python3
"""Temp: today's zeroclone validation runs and pool outcome."""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "db" / "linkedin_data.db"
ZERO = ROOT.parent / "zeroclone"
MANIFEST = ZERO / "cycles" / "manifests" / "validation_manifest.csv"
VALIDATION_DIR = ZERO / "cycles" / "validation"
TODAY = date.today().isoformat()


def parse_day(ts: str) -> str:
    if not ts:
        return ""
    return ts[:10]


def main() -> None:
    print("=" * 72)
    print(f"  ZERoclone VALIDATION OUTCOME CHECK  (today = {TODAY})")
    print("=" * 72)

    if MANIFEST.is_file():
        with MANIFEST.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        today_rows = [r for r in rows if parse_day(r.get("run_at", "")) == TODAY]
        print(f"\n  validation_manifest.csv: {len(rows)} total runs")
        print(f"  Runs today ({TODAY}): {len(today_rows)}")
        print()
        for r in today_rows:
            print(f"  --- run_at: {r.get('run_at')}")
            for k, v in r.items():
                if k != "run_at" and v:
                    print(f"      {k}: {v}")
            print()
        if not today_rows:
            print("  (no manifest rows dated today — showing last 5 runs)")
            for r in rows[-5:]:
                print(f"    {r.get('run_at')}  cycle={r.get('cycle_number')}  "
                      f"file={r.get('validation_file')}  status={r.get('status')}")
    else:
        print(f"  Manifest not found: {MANIFEST}")

    if VALIDATION_DIR.is_dir():
        csvs = sorted(VALIDATION_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        today_csvs = [p for p in csvs if date.fromtimestamp(p.stat().st_mtime).isoformat() == TODAY]
        print(f"\n  Validation CSV files in cycles/validation: {len(csvs)}")
        print(f"  CSV files modified today: {len(today_csvs)}")
        for p in today_csvs[:10]:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(f"    {mtime}  {p.name}")

    if not DB.is_file():
        print(f"\n  DB not found: {DB}")
        return

    conn = sqlite3.connect(DB)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(zerobounce_validation)").fetchall()]
    print(f"\n  zerobounce_validation columns: {', '.join(cols)}")

    print("\n  --- Pool in DB (zerobounce_validation) ---")
    total = conn.execute("SELECT count(*) FROM zerobounce_validation").fetchone()[0]
    print(f"  Total rows: {total}")

    mv_ok = conn.execute(
        """
        SELECT count(*) FROM zerobounce_validation
        WHERE lower(trim(coalesce(mv_status, ''))) IN ('ok', 'valid', 'deliverable')
        """
    ).fetchone()[0]
    zb_valid = conn.execute(
        """
        SELECT count(*) FROM zerobounce_validation
        WHERE lower(trim(coalesce(zb_status, ''))) = 'valid'
        """
    ).fetchone()[0]
    allowlisted = conn.execute(
        """
        SELECT count(*) FROM zerobounce_validation
        WHERE lower(trim(coalesce(zb_status, ''))) = 'valid'
           OR lower(trim(coalesce(mv_status, ''))) IN ('ok', 'valid', 'deliverable')
        """
    ).fetchone()[0]
    print(f"  MV ok/valid/deliverable: {mv_ok}")
    print(f"  ZB valid:                {zb_valid}")
    print(f"  Allowlisted (either):    {allowlisted}")

    pool_new = conn.execute(
        """
        SELECT count(*) FROM zerobounce_validation z
        WHERE (
            lower(trim(coalesce(z.zb_status, ''))) = 'valid'
            OR lower(trim(coalesce(z.mv_status, ''))) IN ('ok', 'valid', 'deliverable')
        )
        AND NOT EXISTS (
            SELECT 1 FROM email_attempts ea WHERE ea.email_address = z.email_address
        )
        """
    ).fetchone()[0]
    print(f"  Allowlisted, never in email_attempts: {pool_new}")

    if "source_batch" in cols:
        print("\n  --- By source_batch (top 15) ---")
        for batch, n in conn.execute(
            """
            SELECT coalesce(source_batch, '(none)'), count(*)
            FROM zerobounce_validation GROUP BY 1 ORDER BY 2 DESC LIMIT 15
            """
        ):
            print(f"    {batch}: {n}")

    ts_col = next(
        (c for c in ("mv_validated_at", "zb_validated_at", "validated_at", "last_updated") if c in cols),
        None,
    )
    if ts_col:
        print(f"\n  --- Rows with {ts_col} dated today ---")
        n_today = conn.execute(
            f"SELECT count(*) FROM zerobounce_validation WHERE {ts_col} LIKE ?",
            (f"{TODAY}%",),
        ).fetchone()[0]
        print(f"  Count: {n_today}")
        if n_today:
            for status, n in conn.execute(
                f"""
                SELECT coalesce(mv_status, zb_status, '(blank)'), count(*)
                FROM zerobounce_validation
                WHERE {ts_col} LIKE ?
                GROUP BY 1 ORDER BY 2 DESC
                """,
                (f"{TODAY}%",),
            ):
                print(f"    {status}: {n}")

    print("\n  --- MV status breakdown ---")
    for s, n in conn.execute(
        """
        SELECT coalesce(mv_status, '(blank)'), count(*)
        FROM zerobounce_validation GROUP BY 1 ORDER BY 2 DESC
        """
    ):
        print(f"    {s}: {n}")

    print("\n  --- employee_email_state (validation cycle state) ---")
    try:
        n_state = conn.execute("SELECT count(*) FROM employee_email_state").fetchone()[0]
        n_resolved = conn.execute(
            "SELECT count(*) FROM employee_email_state "
            "WHERE trim(coalesce(resolved_valid_email,'')) != ''"
        ).fetchone()[0]
        print(f"  Rows in state table: {n_state}")
        print(f"  With resolved_valid_email: {n_resolved}")
        if "last_updated" in [
            r[1] for r in conn.execute("PRAGMA table_info(employee_email_state)").fetchall()
        ]:
            n_upd = conn.execute(
                "SELECT count(*) FROM employee_email_state WHERE last_updated LIKE ?",
                (f"{TODAY}%",),
            ).fetchone()[0]
            print(f"  State rows updated today: {n_upd}")
    except sqlite3.OperationalError as e:
        print(f"  (skip: {e})")

    conn.close()
    print("=" * 72)


if __name__ == "__main__":
    main()
