"""Stage and completion voice prompts for the CVL full pipeline (Windows SAPI; no extra pip packages)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "pipeline_config.json"

DEFAULT_COMPLETE_VOICE_MESSAGE = "CVL full pipeline complete."
DEFAULT_COMPLETE_ERROR_MESSAGE = "CVL full pipeline finished with errors."
DEFAULT_STAGE_MESSAGES = {
    "run_start": "Starting CVL full pipeline.",
    "step_start": "Starting {label}.",
    "step_complete": "{label} complete.",
    "step_failed": "{label} finished with errors.",
}

_STEP_LABELS = {
    "check_bounces": "bounce check",
    "zeroclone_run_cycle": "zeroclone validation cycle",
    "pipeline_summary": "pipeline summary",
    "pool_sender": "validated pool email send",
}


def _default_config() -> dict:
    return {
        "pipeline_complete_voice": True,
        "pipeline_stage_voice": True,
        "pipeline_complete_voice_message": DEFAULT_COMPLETE_VOICE_MESSAGE,
        "pipeline_complete_error_message": DEFAULT_COMPLETE_ERROR_MESSAGE,
    }


def load_pipeline_config() -> dict:
    cfg = _default_config()
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def stage_beep() -> None:
    try:
        if sys.platform == "win32":
            import winsound

            winsound.Beep(880, 150)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:
        pass


def pipeline_complete_beep() -> None:
    try:
        if sys.platform == "win32":
            import winsound

            winsound.Beep(523, 800)
            winsound.Beep(659, 1000)
        else:
            sys.stdout.write("\a\a")
            sys.stdout.flush()
    except Exception:
        pass


def speak_message(message: str) -> None:
    text = (message or "").strip()
    if not text:
        return
    try:
        if sys.platform == "win32":
            import subprocess

            safe = text.replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.Speak('{safe}')"
            )
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                check=False,
                timeout=60,
                creationflags=flags,
            )
        else:
            import shutil
            import subprocess as sp

            if shutil.which("espeak"):
                sp.run(["espeak", text], check=False, timeout=60)
            elif shutil.which("say"):
                sp.run(["say", text], check=False, timeout=60)
    except Exception:
        pass


def voice_enabled(config: dict) -> bool:
    return bool(config.get("pipeline_complete_voice", True))


def stage_voice_enabled(config: dict) -> bool:
    if "pipeline_stage_voice" in config:
        return bool(config.get("pipeline_stage_voice"))
    return voice_enabled(config)


def friendly_step_label(step_name: str) -> str:
    if step_name in _STEP_LABELS:
        return _STEP_LABELS[step_name]
    match = re.match(r"scraper_production_(\d+)$", step_name)
    if match:
        return f"LinkedIn scraper production run {match.group(1)}"
    return step_name.replace("_", " ")


def stage_message(config: dict, key: str, **kwargs: object) -> str:
    overrides = config.get("pipeline_stage_messages") or {}
    template = str(overrides.get(key) or DEFAULT_STAGE_MESSAGES.get(key) or "").strip()
    if not template:
        return ""
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError):
        return template


def notify_stage(
    config: dict,
    key: str,
    *,
    beep: bool = True,
    **kwargs: object,
) -> None:
    if not stage_voice_enabled(config):
        return
    message = stage_message(config, key, **kwargs)
    if beep:
        stage_beep()
    if message:
        speak_message(message)


def notify_step_start(config: dict, step_name: str) -> None:
    notify_stage(config, "step_start", label=friendly_step_label(step_name))


def notify_step_end(config: dict, step_name: str, exit_code: int) -> None:
    label = friendly_step_label(step_name)
    if exit_code == 0:
        notify_stage(config, "step_complete", label=label)
    else:
        notify_stage(config, "step_failed", label=label)


def notify_run_complete(config: dict, *, had_errors: bool = False) -> None:
    pipeline_complete_beep()
    if not voice_enabled(config):
        return
    if had_errors:
        message = (
            config.get("pipeline_complete_error_message") or DEFAULT_COMPLETE_ERROR_MESSAGE
        ).strip()
    else:
        message = (
            config.get("pipeline_complete_voice_message") or DEFAULT_COMPLETE_VOICE_MESSAGE
        ).strip()
    if message:
        speak_message(message)
