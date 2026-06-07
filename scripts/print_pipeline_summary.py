#!/usr/bin/env python3
"""
Pipeline summary (no Playwright).

Default: read pre-saved pipeline_summary_results table (instant).
--refresh: recompute and save snapshot (~1 minute on large DB).
--quick: live base-table counts only (~2 seconds).

Usage:
  python scripts/print_pipeline_summary.py
  python scripts/print_pipeline_summary.py --refresh
  python scripts/print_pipeline_summary.py --quick
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pipeline_summary_cache import (
    VIEW_METRIC_KEYS,
    _count_validation_unprocessed,
    load_pipeline_summary_snapshot,
    refresh_pipeline_summary_snapshot,
)

DB_PATH = ROOT / "data" / "db" / "linkedin_data.db"

_LABELS = {
    "total_employee_rows": "All employee rows in database",
    "scrapeable_employees": (
        "Outreach-ready (domain + name) — includes already-contacted employees"
    ),
    "rows_in_employee_email_state": "Employees tracked in email-validation state table",
    "never_in_validation_cycle": (
        "Outreach-ready employees never started in validation cycle"
    ),
    "resolved_valid_count": (
        "Employees with at least one confirmed valid email address"
    ),
    "still_eligible_for_validation": (
        "Still eligible for another validation / format attempt"
    ),
    "eligible_firstname_lastname": "Next format to try: firstname.lastname@domain",
    "eligible_firstname": "Next format to try: firstname@domain",
    "eligible_firstinitial_lastname": (
        "Next format to try: f.lastname@domain (first initial)"
    ),
    "eligible_firstname_lastinitial": (
        "Next format to try: firstname.l@domain (last initial)"
    ),
    "cascade_exhausted_no_valid": "All format patterns tried — no valid address found",
    "allowlisted_addresses": "Allowlisted addresses (trusted / skip re-validation)",
    "pool_sendable_addresses": "Addresses in send pool (ready for campaign)",
    "email_attempts_total": "Total email_attempts rows (every send + bounce record)",
    "email_attempts_sent": "Attempts with status sent (SMTP accepted)",
    "email_attempts_bounced": "Attempts with status bounced / delivery failed",
}


def _quick_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Fast counts from base tables only (no heavy views)."""
    out: dict[str, int] = {}
    out["total_employee_rows"] = conn.execute(
        "SELECT count(*) FROM employees"
    ).fetchone()[0]
    out["rows_in_employee_email_state"] = conn.execute(
        "SELECT count(*) FROM employee_email_state"
    ).fetchone()[0]
    out["resolved_valid_count"] = conn.execute(
        """
        SELECT count(*) FROM employee_email_state
        WHERE trim(coalesce(resolved_valid_email, '')) != ''
        """
    ).fetchone()[0]
    ea = conn.execute(
        """
        SELECT count(*),
               sum(CASE WHEN lower(coalesce(status, '')) = 'sent' THEN 1 ELSE 0 END),
               sum(CASE WHEN lower(coalesce(status, '')) = 'bounced' THEN 1 ELSE 0 END)
        FROM email_attempts
        """
    ).fetchone()
    out["email_attempts_total"] = int(ea[0] or 0)
    out["email_attempts_sent"] = int(ea[1] or 0)
    out["email_attempts_bounced"] = int(ea[2] or 0)
    out["allowlisted_addresses"] = conn.execute(
        """
        SELECT count(*) FROM zerobounce_validation
        WHERE lower(trim(coalesce(zb_status, ''))) = 'valid'
           OR lower(trim(coalesce(mv_status, ''))) IN ('ok', 'valid', 'deliverable')
        """
    ).fetchone()[0]
    out["pool_sendable_addresses"] = conn.execute(
        """
        SELECT count(*) FROM zerobounce_validation z
        WHERE (
            lower(trim(coalesce(z.zb_status, ''))) = 'valid'
            OR lower(trim(coalesce(z.mv_status, ''))) IN ('ok', 'valid', 'deliverable')
        )
        AND NOT EXISTS (
            SELECT 1 FROM email_attempts ea
            WHERE ea.email_address = z.email_address
        )
        """
    ).fetchone()[0]
    return out


def _print_row(label: str, val: int | str) -> None:
    if isinstance(val, int):
        print(f"  {label:<55}: {val:,}")
    else:
        print(f"  {label:<55}: {val}")


def _print_failed_records_summary(snapshot: dict[str, int | str]) -> None:
    unproc = int(snapshot.get("validation_unprocessed") or 0)
    cascade = int(snapshot.get("cascade_exhausted_no_valid") or 0)
    bounced = int(snapshot.get("email_attempts_bounced") or 0)
    all_failed = int(snapshot.get("all_attempts_failed") or 0)
    print("  --- Failed / backlog records ---")
    _print_row(
        "Validation unprocessed (zeroclone partial runs)",
        unproc,
    )
    _print_row(
        "All format patterns tried - no valid address (employees)",
        cascade,
    )
    _print_row("Campaign delivery failed (email_attempts bounced)", bounced)
    _print_row(
        "Outreach failed - emailed but zero successful sends (employees)",
        all_failed,
    )
    print()


def _print_from_snapshot(snapshot: dict[str, int | str]) -> None:
    print("=" * 72)
    print("  EMAIL / VALIDATION PIPELINE SUMMARY")
    print("=" * 72)
    print(f"  Results saved at: {snapshot['refreshed_at']}")
    print("  Table: pipeline_summary_results  |  --refresh to recompute now")
    print()
    _print_row(
        ">>> STILL REACHABLE: no successful send, formats not all exhausted",
        int(snapshot["still_reachable"]),
    )
    print("       (never emailed + bounce-only; excludes all formats exhausted)")
    print()
    for key in VIEW_METRIC_KEYS:
        _print_row(_LABELS.get(key, key), int(snapshot[key]))
    print()
    _print_failed_records_summary(snapshot)
    print("  --- Per-employee outreach breakdown ---")
    _print_row(
        "Never emailed — not even one attempt",
        int(snapshot["never_emailed_once"]),
    )
    _print_row(
        "Emailed before but every attempt failed (0 sent, has bounce/attempt rows)",
        int(snapshot["all_attempts_failed"]),
    )
    print("=" * 72)


def _print_quick(conn: sqlite3.Connection) -> None:
    counts = _quick_counts(conn)
    counts["validation_unprocessed"] = _count_validation_unprocessed()
    counts["cascade_exhausted_no_valid"] = 0
    counts["all_attempts_failed"] = 0
    try:
        row = conn.execute(
            "SELECT cascade_exhausted_no_valid FROM pipeline_summary_results WHERE id = 1"
        ).fetchone()
        if row:
            counts["cascade_exhausted_no_valid"] = int(row[0] or 0)
    except sqlite3.OperationalError:
        pass
    print("=" * 72)
    print("  PIPELINE SUMMARY (QUICK — live counts, not cached)")
    print("=" * 72)
    print()
    for key in (
        "total_employee_rows",
        "rows_in_employee_email_state",
        "resolved_valid_count",
        "allowlisted_addresses",
        "pool_sendable_addresses",
        "email_attempts_total",
        "email_attempts_sent",
        "email_attempts_bounced",
    ):
        if key in counts:
            _print_row(_LABELS.get(key, key), counts[key])
    print()
    _print_failed_records_summary(counts)
    print("=" * 72)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--quick",
        action="store_true",
        help="Live base-table counts only, skip cached snapshot",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Recompute and save snapshot to DB before printing",
    )
    args = p.parse_args()

    if not DB_PATH.is_file():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    try:
        if args.quick:
            _print_quick(conn)
            return 0

        snapshot = load_pipeline_summary_snapshot(conn)
        if args.refresh or snapshot is None:
            if snapshot is None:
                print("No saved snapshot — computing and saving to database...", flush=True)
            else:
                print("Refreshing pipeline summary snapshot...", flush=True)
            t0 = time.perf_counter()
            refreshed_at = refresh_pipeline_summary_snapshot(conn)
            print(f"  Saved at {refreshed_at} ({time.perf_counter() - t0:.1f}s)", flush=True)
            print()
            snapshot = load_pipeline_summary_snapshot(conn)

        if snapshot is None:
            print("Could not load pipeline summary snapshot.", file=sys.stderr)
            return 1

        _print_from_snapshot(snapshot)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
