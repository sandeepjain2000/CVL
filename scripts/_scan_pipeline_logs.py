#!/usr/bin/env python3
"""Scan last N pipeline batch logs for errors."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs" / "pipeline_batch"
DB = ROOT / "data" / "db" / "linkedin_data.db"

ERROR_PATTERNS = [
    re.compile(r"traceback \(most recent call last\)", re.I),
    re.compile(r"fatal error", re.I),
    re.compile(r"error:", re.I),
    re.compile(r"\bERROR\b"),
    re.compile(r"not recognized as an internal or external command", re.I),
    re.compile(r"exit=\d+", re.I),
    re.compile(r"STEP END:.*exit=[1-9]", re.I),
    re.compile(r"completed_with_errors", re.I),
    re.compile(r"invalid int value", re.I),
    re.compile(r"cannot find the path", re.I),
    re.compile(r"database is locked", re.I),
    re.compile(r"AUTH ERROR", re.I),
    re.compile(r"Press Enter to exit", re.I),
]


def scan_log(path: Path, tail: int = 0) -> list[tuple[int, str]]:
    if not path.is_file():
        return [(0, f"MISSING: {path}")]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if tail:
        lines = lines[-tail:]
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=len(lines) - len(lines) + 1 if tail else 1):
        for pat in ERROR_PATTERNS:
            if pat.search(line):
                hits.append((i, line.strip()[:200]))
                break
    return hits


def main() -> None:
    logs = sorted(LOG_DIR.glob("pipeline_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    print("=== LAST 2 PIPELINE LOG FILES ===")
    for p in logs[:2]:
        print(f"  {p.name}  ({p.stat().st_size:,} bytes, {p.stat().st_mtime})")

    if DB.is_file():
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, started_at, finished_at, status, log_file,
                   failed_step, failed_exit_code, notes
            FROM pipeline_batch_runs ORDER BY id DESC LIMIT 2
            """
        ).fetchall()
        print("\n=== LAST 2 BATCH DB RECORDS ===")
        for r in rows:
            print(dict(r))
            steps = conn.execute(
                """
                SELECT step_order, step_name, status, exit_code
                FROM pipeline_batch_steps WHERE batch_id = ?
                ORDER BY step_order
                """,
                (r["id"],),
            ).fetchall()
            for s in steps:
                flag = " ***" if s["exit_code"] not in (0, None) else ""
                print(f"    step {s['step_order']} {s['step_name']}: exit={s['exit_code']} {s['status']}{flag}")
        conn.close()

    for p in logs[:2]:
        print(f"\n=== ERRORS IN {p.name} ===")
        hits = scan_log(p)
        # de-dupe adjacent similar
        if not hits:
            print("  No error-pattern lines found (full scan).")
            continue
        seen = set()
        count = 0
        for ln, text in hits:
            if text in seen:
                continue
            seen.add(text)
            print(f"  L{ln}: {text}")
            count += 1
            if count >= 40:
                print("  ... truncated ...")
                break

        # step summary from log
        print(f"\n  STEP END lines:")
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "STEP END:" in line or "PIPELINE BATCH COMPLETED" in line:
                print(f"    L{i}: {line.strip()}")


if __name__ == "__main__":
    main()
