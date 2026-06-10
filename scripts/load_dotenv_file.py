"""Minimal .env loader (no python-dotenv dependency)."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv_file(path: str | Path) -> None:
    """Set KEY=VALUE pairs from a .env file into os.environ (existing env wins)."""
    p = Path(path)
    if not p.is_file():
        return
    for raw_line in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def resolve_env_path(path: str, base_dir: str | Path) -> str:
    """Expand relative paths against project root."""
    path = (path or "").strip()
    if not path:
        return ""
    p = Path(path)
    if p.is_absolute():
        return str(p.resolve())
    return str((Path(base_dir) / p).resolve())
