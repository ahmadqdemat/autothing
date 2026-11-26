"""Add-on options, read once and re-read only when options.json changes."""
import json
import os
from pathlib import Path

OPTIONS_PATH = Path(os.getenv("MENTOR_OPTIONS_PATH", "/data/options.json"))

DEFAULTS = {
    "gemini_api_key": "",
    "model": "gemini-2.5-flash",
    "leak_check_model": "",
    "thinking_level": "low",
    "unlock_passphrase": "",
    "unlock_idle_timeout_minutes": 30,
    "max_unlock_attempts": 5,
    "lockout_minutes": 15,
    "leak_check_enabled": True,
    "student_age_or_grade": "Middle School / High School",
    "log_conversations": False,
}

_cache = {"mtime": "unset", "data": dict(DEFAULTS)}


def _from_env() -> dict:
    data = dict(DEFAULTS)
    data["gemini_api_key"] = os.getenv("GEMINI_API_KEY", "")
    data["model"] = os.getenv("GEMINI_MODEL", DEFAULTS["model"])
    data["unlock_passphrase"] = os.getenv("UNLOCK_PASSPHRASE", "")
    return data


def get_options() -> dict:
    try:
        mtime = OPTIONS_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if mtime != _cache["mtime"]:
        if mtime is None:
            data = _from_env()
        else:
            data = dict(DEFAULTS)
            try:
                data.update(json.loads(OPTIONS_PATH.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        _cache["mtime"] = mtime
        _cache["data"] = data
    return _cache["data"]
