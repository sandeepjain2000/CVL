#!/usr/bin/env python3
"""Run one pipeline step: stream output to console and append to master log."""

from __future__ import annotations

import os
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: run_step_tee.py <log_file>  (command in CVL_STEP_CMD)", file=sys.stderr)
        return 2
    log_path = sys.argv[1]
    cmd = os.environ.get("CVL_STEP_CMD", "").strip()
    if not cmd:
        print("CVL_STEP_CMD is not set", file=sys.stderr)
        return 2
    with open(log_path, "a", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
        )
        if proc.stdout is None:
            return 1
        for line in proc.stdout:
            try:
                print(line, end="", flush=True)
            except UnicodeEncodeError:
                safe = line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
                    sys.stdout.encoding or "utf-8", errors="replace"
                )
                print(safe, end="", flush=True)
            log.write(line)
        return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
