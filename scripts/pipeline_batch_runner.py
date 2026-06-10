#!/usr/bin/env python3
"""
Preflight checks and DB logging for run_full_pipeline.bat.

Does not modify core pipeline scripts — only records batch/step runs and
scans the previous master log for errors before a new unattended run.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "db" / "linkedin_data.db"
SQL_FILE = ROOT / "sql" / "pipeline_batch_runs.sql"
LOG_DIR = ROOT / "logs" / "pipeline_batch"

_ERROR_PATTERNS = (
    re.compile(r"traceback \(most recent call last\)", re.I),
    re.compile(r"fatal error", re.I),
    re.compile(r"❌"),
    re.compile(r"\berror processing\b", re.I),
    re.compile(r"finished with exit code [1-9]", re.I),
    re.compile(r"exit code [1-9]", re.I),
    re.compile(r"could not save", re.I),
    re.compile(r"database is locked", re.I),
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(conn: sqlite3.Connection) -> None:
    if SQL_FILE.is_file():
        conn.executescript(SQL_FILE.read_text(encoding="utf-8"))
        conn.commit()


def _scan_log_for_errors(log_path: Path, tail_lines: int = 400) -> list[str]:
    if not log_path.is_file():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return [f"Could not read log {log_path}: {e}"]
    hits: list[str] = []
    for line in lines[-tail_lines:]:
        for pat in _ERROR_PATTERNS:
            if pat.search(line):
                hits.append(line.strip())
                break
    # de-dupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out[-12:]


def cmd_preflight() -> int:
    """Exit 1 if last batch or its log shows errors (prompt user at bat start)."""
    if not DB_PATH.is_file():
        print("No database yet — first pipeline run.")
        return 0

    conn = _connect()
    try:
        ensure_tables(conn)
        row = conn.execute(
            """
            SELECT id, started_at, finished_at, status, log_file,
                   failed_step, failed_exit_code, notes
            FROM pipeline_batch_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    if not row:
        print("No previous pipeline batch recorded.")
        return 0

    issues: list[str] = []
    status = (row["status"] or "").lower()
    if status == "running":
        issues.append(
            f"Last batch #{row['id']} never finished (status=running, "
            f"started {row['started_at']}) — likely interrupted"
        )
        conn2 = _connect()
        try:
            ensure_tables(conn2)
            conn2.execute(
                """
                UPDATE pipeline_batch_runs
                SET status = 'aborted', finished_at = ?, notes = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    _now(),
                    "Marked aborted on next preflight (previous run did not finish)",
                    row["id"],
                ),
            )
            conn2.commit()
        finally:
            conn2.close()
    elif status in {"failed", "completed_with_errors", "aborted"}:
        issues.append(
            f"Last batch #{row['id']} status={row['status']} "
            f"({row['started_at']} → {row['finished_at'] or '?'})"
        )
        if row["failed_step"]:
            issues.append(
                f"  Failed step: {row['failed_step']} "
                f"(exit {row['failed_exit_code']})"
            )
        if row["notes"]:
            issues.append(f"  Notes: {row['notes']}")

    log_file = (row["log_file"] or "").strip()
    if log_file:
        log_hits = _scan_log_for_errors(Path(log_file))
        for hit in log_hits:
            issues.append(f"  Log: {hit}")

    if not issues:
        print(
            f"Last batch #{row['id']} OK ({row['status']}) — "
            f"{row['finished_at'] or row['started_at']}"
        )
        return 0

    print("=" * 72)
    print("  WARNING — PREVIOUS PIPELINE RUN HAD ISSUES")
    print("=" * 72)
    if log_file:
        print(f"  Log file: {log_file}")
    for line in issues:
        print(line)
    print("=" * 72)
    print("  Review the log before continuing. Ask Cursor to fix if needed.")
    print("=" * 72)
    return 1


def cmd_start(log_file: str, scraper_runs: int) -> int:
    conn = _connect()
    try:
        ensure_tables(conn)
        started = _now()
        conn.execute(
            """
            INSERT INTO pipeline_batch_runs (
                started_at, status, scraper_runs_planned, log_file
            ) VALUES (?, 'running', ?, ?)
            """,
            (started, scraper_runs, log_file),
        )
        conn.commit()
        batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        print(batch_id)
        return 0
    finally:
        conn.close()


def cmd_step_start(batch_id: int, step_order: int, step_name: str) -> int:
    conn = _connect()
    try:
        ensure_tables(conn)
        conn.execute(
            """
            INSERT INTO pipeline_batch_steps (
                batch_id, step_order, step_name, started_at, status
            ) VALUES (?, ?, ?, ?, 'running')
            """,
            (batch_id, step_order, step_name, _now()),
        )
        conn.commit()
        step_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        print(step_id)
        return 0
    finally:
        conn.close()


def cmd_step_end(
    batch_id: int, step_id: int, exit_code: int, step_name: str = ""
) -> int:
    conn = _connect()
    try:
        ensure_tables(conn)
        status = "ok" if exit_code == 0 else "failed"
        conn.execute(
            """
            UPDATE pipeline_batch_steps
            SET finished_at = ?, exit_code = ?, status = ?
            WHERE id = ? AND batch_id = ?
            """,
            (_now(), exit_code, status, step_id, batch_id),
        )
        if exit_code != 0:
            conn.execute(
                """
                UPDATE pipeline_batch_runs
                SET failed_step = ?, failed_exit_code = ?
                WHERE id = ? AND (failed_step IS NULL OR failed_step = '')
                """,
                (step_name, exit_code, batch_id),
            )
        conn.commit()
        return 0
    finally:
        conn.close()


def cmd_finish(batch_id: int, status: str, notes: str = "") -> int:
    conn = _connect()
    try:
        ensure_tables(conn)
        conn.execute(
            """
            UPDATE pipeline_batch_runs
            SET finished_at = ?, status = ?, notes = ?
            WHERE id = ?
            """,
            (_now(), status, notes, batch_id),
        )
        conn.commit()
        return 0
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("preflight", help="Check last batch run; exit 1 if errors")

    ps = sub.add_parser("start", help="Create batch row; print batch id")
    ps.add_argument("--log", required=True)
    ps.add_argument("--scraper-runs", type=int, default=2)

    pss = sub.add_parser("step-start", help="Start step; print step id")
    pss.add_argument("batch_id", type=int)
    pss.add_argument("step_order", type=int)
    pss.add_argument("step_name")

    pse = sub.add_parser("step-end", help="Finish step")
    pse.add_argument("batch_id", type=int)
    pse.add_argument("step_id", type=int)
    pse.add_argument("exit_code", type=int)
    pse.add_argument("step_name", nargs="?", default="")

    pf = sub.add_parser("finish", help="Finish batch")
    pf.add_argument("batch_id", type=int)
    pf.add_argument("status")
    pf.add_argument("--notes", default="")

    args = p.parse_args()

    if args.cmd == "preflight":
        return cmd_preflight()
    if args.cmd == "start":
        return cmd_start(args.log, args.scraper_runs)
    if args.cmd == "step-start":
        return cmd_step_start(args.batch_id, args.step_order, args.step_name)
    if args.cmd == "step-end":
        return cmd_step_end(args.batch_id, args.step_id, args.exit_code, args.step_name)
    if args.cmd == "finish":
        return cmd_finish(args.batch_id, args.status, args.notes)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
