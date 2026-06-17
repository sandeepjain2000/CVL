"""Stage and completion voice prompts for the CVL full pipeline (Windows SAPI; no extra pip packages)."""

from __future__ import annotations

import json
import re
import sys
import time
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
DEFAULT_SCRAPE_NAV_MESSAGES = {
    "search": "Opening search results page.",
    "company": "Opening company page.",
    "about": "Opening about page.",
    "people": "Opening employees page.",
    "feed": "Opening LinkedIn feed.",
}

_STEP_LABELS = {
    "check_bounces": "bounce check",
    "zeroclone_run_cycle": "zeroclone validation cycle",
    "pipeline_summary": "pipeline summary",
    "pool_sender": "validated pool email send",
}

_last_scrape_voice_at = 0.0


def _default_config() -> dict:
    return {
        "pipeline_complete_voice": True,
        "pipeline_stage_voice": True,
        "pipeline_complete_voice_message": DEFAULT_COMPLETE_VOICE_MESSAGE,
        "pipeline_complete_error_message": DEFAULT_COMPLETE_ERROR_MESSAGE,
        "voice_volume": 40,
        "beep_volume_percent": 90,
        "stage_beep_volume_percent": 90,
        "completion_beep_volume_percent": 100,
        "scrape_navigation_voice": True,
        "scrape_navigation_beep": True,
        "scrape_navigation_beep_volume_percent": 85,
        "scrape_navigation_voice_interval_seconds": 12,
        "scrape_navigation_messages": DEFAULT_SCRAPE_NAV_MESSAGES,
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


def _clamp_percent(value: object, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def voice_volume(config: dict | None) -> int:
    cfg = config or _default_config()
    return _clamp_percent(cfg.get("voice_volume"), 40)


def beep_volume(config: dict | None, *, kind: str = "stage") -> int:
    cfg = config or _default_config()
    default = _clamp_percent(cfg.get("beep_volume_percent"), 90)
    key_by_kind = {
        "stage": "stage_beep_volume_percent",
        "navigation": "scrape_navigation_beep_volume_percent",
        "completion": "completion_beep_volume_percent",
    }
    key = key_by_kind.get(kind, "beep_volume_percent")
    return _clamp_percent(cfg.get(key, default), default)


def beep_with_volume(freq_hz: int, duration_ms: int, volume_percent: int = 100) -> None:
    """Play a beep; on Windows briefly lowers wave-out volume when below 100."""
    if sys.platform == "win32":
        import ctypes
        import winsound

        volume_percent = _clamp_percent(volume_percent, 100)
        try:
            winmm = ctypes.windll.winmm
            old = winmm.waveOutGetVolume(0)
            if volume_percent < 100:
                vol = int(0xFFFF * volume_percent / 100)
                combined = vol | (vol << 16)
                winmm.waveOutSetVolume(0, combined)
            try:
                winsound.Beep(freq_hz, duration_ms)
            finally:
                if volume_percent < 100:
                    winmm.waveOutSetVolume(0, old)
        except Exception:
            try:
                winsound.Beep(freq_hz, duration_ms)
            except Exception:
                pass
    else:
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass


def stage_beep(config: dict | None = None) -> None:
    beep_with_volume(880, 150, beep_volume(config, kind="stage"))


def navigation_beep(config: dict | None = None) -> None:
    beep_with_volume(920, 100, beep_volume(config, kind="navigation"))


def pipeline_complete_beep(config: dict | None = None) -> None:
    vol = beep_volume(config, kind="completion")
    beep_with_volume(523, 800, vol)
    beep_with_volume(659, 1000, vol)


def speak_message(message: str, *, volume: int | None = None, config: dict | None = None) -> None:
    text = (message or "").strip()
    if not text:
        return
    vol = voice_volume(config) if volume is None else _clamp_percent(volume, 40)
    try:
        if sys.platform == "win32":
            import subprocess

            safe = text.replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.Volume = {vol}; "
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

            amp = max(0, min(200, vol))
            if shutil.which("espeak"):
                sp.run(["espeak", "-a", str(amp), text], check=False, timeout=60)
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


def scrape_nav_voice_enabled(config: dict) -> bool:
    if "scrape_navigation_voice" in config:
        return bool(config.get("scrape_navigation_voice"))
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


def scrape_nav_message(config: dict, kind: str, *, label: str = "") -> str:
    overrides = config.get("scrape_navigation_messages") or {}
    template = str(
        overrides.get(kind) or DEFAULT_SCRAPE_NAV_MESSAGES.get(kind) or ""
    ).strip()
    if not template:
        return ""
    try:
        message = template.format(label=label, kind=kind)
    except (KeyError, ValueError):
        message = template
    if label and "{label}" not in template and kind in ("company", "people"):
        message = f"{message} {label}"
    return message


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
        stage_beep(config)
    if message:
        speak_message(message, config=config)


def notify_step_start(config: dict, step_name: str) -> None:
    notify_stage(config, "step_start", label=friendly_step_label(step_name))


def notify_step_end(config: dict, step_name: str, exit_code: int) -> None:
    label = friendly_step_label(step_name)
    if exit_code == 0:
        notify_stage(config, "step_complete", label=label)
    else:
        notify_stage(config, "step_failed", label=label)


def notify_scrape_page_open(
    config: dict,
    kind: str,
    *,
    label: str = "",
    allow_beep: bool = True,
) -> None:
    """
    Intermittent scrape feedback: short beep on every page open;
    spoken message throttled by scrape_navigation_voice_interval_seconds.
    """
    global _last_scrape_voice_at

    if allow_beep and config.get("scrape_navigation_beep", True):
        navigation_beep(config)

    if not scrape_nav_voice_enabled(config):
        return

    interval = float(config.get("scrape_navigation_voice_interval_seconds", 12) or 12)
    now = time.monotonic()
    if interval > 0 and (now - _last_scrape_voice_at) < interval:
        return

    message = scrape_nav_message(config, kind, label=label.strip())
    if not message:
        return

    _last_scrape_voice_at = now
    speak_message(message, config=config)


def notify_run_complete(config: dict, *, had_errors: bool = False) -> None:
    pipeline_complete_beep(config)
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
        speak_message(message, config=config)
