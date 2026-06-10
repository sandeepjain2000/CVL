#!/usr/bin/env python3
"""
Unattended CVL full pipeline (replaces fragile batch orchestration).

Sequence:
  1. LinkedIn scraper PRODUCTION x N
  2. check_bounces.py
  3. zeroclone run_cycle.py
  4. print_pipeline_summary.py
  5. send_validated_pool.py -n 5

One Enter prompt at start only. CVL_UNATTENDED=1 for child scripts.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
ZEROCLONE = ROOT.parent / "zeroclone"
LOG_DIR = ROOT / "logs" / "pipeline_batch"
RUNNER = SCRIPTS / "pipeline_batch_runner.py"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from load_dotenv_file import load_dotenv_file  # noqa: E402

load_dotenv_file(ROOT / ".env")

DEFAULT_SCRAPER_RUNS = 2

STEP_NAMES = (
    "scraper",
    "bounces",
    "zeroclone",
    "summary",
    "pool",
)


def _py() -> str:
    return sys.executable


def _safe_console_line(line: str) -> str:
    """ASCII-safe console output on Windows CMD (log file keeps UTF-8)."""
    return (
        line.replace("\u2500", "-")
        .replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2026", "...")
        .replace("\u2192", "->")
        .replace("\u2713", "OK")
        .replace("\u2717", "X")
    )


def _runner(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_py(), str(RUNNER), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def _tee_run(cmd: list[str], *, cwd: Path | None, log_path: Path, env: dict) -> int:
    header = f"\n{'=' * 70}\nCOMMAND: {' '.join(cmd)}\n{'=' * 70}\n"
    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write(header)
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd or ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
            env=env,
        )
        if proc.stdout is None:
            return 1
        for line in proc.stdout:
            out = _safe_console_line(line)
            try:
                print(out, end="", flush=True)
            except UnicodeEncodeError:
                enc = sys.stdout.encoding or "utf-8"
                print(
                    out.encode(enc, errors="replace").decode(enc, errors="replace"),
                    end="",
                    flush=True,
                )
            log.write(line)
        return proc.wait()


def _run_step(
    batch_id: int,
    order: int,
    name: str,
    cmd: list[str],
    *,
    cwd: Path | None,
    log_path: Path,
    env: dict,
) -> tuple[int, bool]:
    print(f"\n  Running: {name}")
    log_path.open("a", encoding="utf-8").write(
        f"\n[{datetime.now().isoformat(timespec='seconds')}] STEP START: {name}\n"
    )
    start = _runner("step-start", str(batch_id), str(order), name)
    if start.returncode != 0:
        print(start.stderr or start.stdout, file=sys.stderr)
        return 1, True
    step_id = (start.stdout or "").strip().splitlines()[-1]

    code = _tee_run(cmd, cwd=cwd, log_path=log_path, env=env)

    end = _runner("step-end", str(batch_id), step_id, str(code), name)
    if end.returncode != 0:
        print(end.stderr or end.stdout, file=sys.stderr)
    log_path.open("a", encoding="utf-8").write(
        f"[{datetime.now().isoformat(timespec='seconds')}] STEP END: {name} exit={code}\n"
    )
    return code, code != 0


def _preflight() -> int:
    r = subprocess.run([_py(), str(RUNNER), "preflight"], cwd=ROOT)
    return r.returncode


def _start_batch(log_path: Path, scraper_runs: int) -> int:
    r = _runner("start", "--log", str(log_path), "--scraper-runs", str(scraper_runs))
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        raise SystemExit(1)
    return int((r.stdout or "").strip().splitlines()[-1])


def _finish_batch(batch_id: int, failed: bool) -> None:
    if failed:
        _runner("finish", str(batch_id), "completed_with_errors", "--notes", "step_errors")
    else:
        _runner("finish", str(batch_id), "completed")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--scraper-runs",
        type=int,
        default=DEFAULT_SCRAPER_RUNS,
        help=f"Production scraper runs (default {DEFAULT_SCRAPER_RUNS})",
    )
    p.add_argument(
        "--from-step",
        choices=STEP_NAMES,
        default="scraper",
        help="Skip earlier steps (e.g. --from-step zeroclone after scraper+bounces done)",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the single start prompt (for automation)",
    )
    return p.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    master_log = LOG_DIR / f"pipeline_{stamp}.log"

    env = os.environ.copy()
    env["CVL_UNATTENDED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    print()
    print("=" * 72)
    print("  CVL FULL PIPELINE")
    print(f"  Folder: {ROOT}")
    print("=" * 72)
    print()
    print("  Sequence after start:")
    steps = []
    if args.from_step == "scraper":
        for i in range(1, args.scraper_runs + 1):
            steps.append(f"    - LinkedIn scraper PRODUCTION #{i}")
    if args.from_step in ("scraper", "bounces"):
        steps.append("    - Check bounces")
    if args.from_step in ("scraper", "bounces", "zeroclone"):
        steps.append("    - Zeroclone run_cycle")
    if args.from_step in ("scraper", "bounces", "zeroclone", "summary"):
        steps.append("    - Pipeline summary")
    steps.append("    - Pool sender (-n 5)")
    print("\n".join(steps))
    print()
    print(f"  Master log: {master_log}")
    print()
    print("-" * 72)
    _preflight()
    print("-" * 72)
    print()
    print("  ONE Enter starts everything. No further prompts.")
    if not args.yes:
        try:
            input("  Press Enter to start... ")
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return 1

    master_log.write_text(
        f"[{datetime.now().isoformat(timespec='seconds')}] PIPELINE BATCH START\n"
        f"scraper_runs={args.scraper_runs} from_step={args.from_step}\n\n",
        encoding="utf-8",
    )
    batch_id = _start_batch(master_log, args.scraper_runs)
    order = 0
    any_fail = False
    py = _py()

    def step(name: str, cmd: list[str], cwd: Path | None = None) -> None:
        nonlocal order, any_fail
        order += 1
        code, fail = _run_step(batch_id, order, name, cmd, cwd=cwd, log_path=master_log, env=env)
        if fail:
            any_fail = True
            print(f"  WARNING: {name} exited with code {code} — continuing pipeline")

    start_idx = STEP_NAMES.index(args.from_step)

    if start_idx <= 0:
        for n in range(1, args.scraper_runs + 1):
            step(
                f"scraper_production_{n}",
                [py, str(ROOT / "linkedin_scraper.py"), "--run", "--browser", "chromium"],
            )

    if start_idx <= 1:
        step("check_bounces", [py, str(ROOT / "check_bounces.py")])

    if start_idx <= 2:
        zc_script = ZEROCLONE / "run_cycle.py"
        if not zc_script.is_file():
            print(f"  ERROR: zeroclone not found: {zc_script}", file=sys.stderr)
            any_fail = True
        else:
            step("zeroclone_run_cycle", [py, str(zc_script), "--limit", "500"], cwd=ZEROCLONE)

    if start_idx <= 3:
        step("pipeline_summary", [py, str(SCRIPTS / "print_pipeline_summary.py")])

    if start_idx <= 4:
        step("pool_sender", [py, str(ROOT / "send_validated_pool.py"), "-n", "5"])

    _finish_batch(batch_id, any_fail)
    status = "COMPLETED WITH ERRORS" if any_fail else "COMPLETED OK"
    print()
    print(f"  Pipeline {status}")
    print(f"  Master log: {master_log}")
    print(f"  Batch id: {batch_id}")
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
