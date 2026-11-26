"""Conversation and image storage.

Conversations live in memory and, when logging is on, as JSON files on disk.
Images are stored as separate JPEG files (never inside the JSON), so loading
a conversation or the history list stays fast.
"""
import base64
import io
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageOps

_BASE_DIR = Path(__file__).resolve().parent
_ENV_DATA_DIR = os.getenv("MENTOR_DATA_DIR")
DATA_DIR = Path(_ENV_DATA_DIR) if _ENV_DATA_DIR else (
    Path("/data") if Path("/data").is_dir() else _BASE_DIR.parent / "data"
)
CONV_DIR = DATA_DIR / "conversations"
IMG_DIR = DATA_DIR / "images"

TITLE_MAX_LEN = 60
MAX_IMAGE_SIDE = 1600

SESSIONS: Dict[str, Dict[str, Any]] = {}
_META_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def is_valid_id(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except (ValueError, AttributeError, TypeError):
        return False


def is_valid_image_name(name: str) -> bool:
    return bool(name) and name.endswith(".jpg") and is_valid_id(name[:-4])


def _conv_path(conv_id: str) -> Path:
    return CONV_DIR / (conv_id + ".json")


def new_state() -> Dict[str, Any]:
    now = time.time()
    return {
        "created_at": now,
        "updated_at": now,
        "title": "",
        "is_full_mode": False,
        "last_activity": now,
        "current_rung": 1,
        "leak_flags": 0,
        "messages": [],
        "images": {},
    }


def derive_title(messages: List[Dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") == "user" and (msg.get("content") or "").strip():
            first = msg["content"].strip().splitlines()[0].strip()
            if len(first) > TITLE_MAX_LEN:
                return first[:TITLE_MAX_LEN].rstrip() + "…"
            return first
    return ""


# ---------- images ----------

def normalize_image(raw: bytes) -> bytes:
    """Fix rotation, shrink to MAX_IMAGE_SIDE and re-encode as JPEG."""
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue()


def store_image(conv_id: str, session: Dict[str, Any], raw: bytes, persist: bool) -> str:
    data = normalize_image(raw)
    name = str(uuid.uuid4()) + ".jpg"
    if persist:
        folder = IMG_DIR / conv_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
    else:
        session.setdefault("images", {})[name] = data
    return name


def load_image(conv_id: str, name: str, session: Optional[Dict[str, Any]] = None) -> Optional[bytes]:
    if not (is_valid_id(conv_id) and is_valid_image_name(name)):
        return None
    if session and name in session.get("images", {}):
        return session["images"][name]
    path = IMG_DIR / conv_id / name
    try:
        return path.read_bytes()
    except OSError:
        return None


# ---------- conversations ----------

def save(conv_id: str, session: Dict[str, Any], persist: bool) -> None:
    session["updated_at"] = time.time()
    if not session.get("title"):
        session["title"] = derive_title(session["messages"])
    if not persist or not is_valid_id(conv_id) or not session.get("messages"):
        return
    data = {
        "id": conv_id,
        "title": session.get("title", ""),
        "created_at": session.get("created_at"),
        "updated_at": session["updated_at"],
        "current_rung": session.get("current_rung", 1),
        "leak_flags": session.get("leak_flags", 0),
        "messages": session["messages"],
    }
    CONV_DIR.mkdir(parents=True, exist_ok=True)
    path = _conv_path(conv_id)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _migrate_inline_images(conv_id: str, data: Dict[str, Any]) -> bool:
    """Older versions stored photos as base64 inside the JSON. Move them to files."""
    changed = False
    for msg in data.get("messages", []):
        b64 = msg.pop("image_b64", None)
        if b64:
            changed = True
            try:
                raw = base64.b64decode(b64)
                folder = IMG_DIR / conv_id
                folder.mkdir(parents=True, exist_ok=True)
                name = str(uuid.uuid4()) + ".jpg"
                (folder / name).write_bytes(normalize_image(raw))
                msg["image"] = name
            except Exception:
                msg["image"] = None
    return changed


def load(conv_id: str) -> Optional[Dict[str, Any]]:
    if not is_valid_id(conv_id):
        return None
    path = _conv_path(conv_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    session = new_state()
    for key in ("created_at", "updated_at", "title", "current_rung", "leak_flags", "messages"):
        if key in data:
            session[key] = data[key]
    session["last_activity"] = time.time()
    SESSIONS[conv_id] = session
    if _migrate_inline_images(conv_id, data):
        session["messages"] = data["messages"]
        save(conv_id, session, persist=True)
    return session


def get(conv_id: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """Returns (id, session). Unknown or malformed IDs get a fresh session."""
    if conv_id and is_valid_id(conv_id):
        if conv_id in SESSIONS:
            return conv_id, SESSIONS[conv_id]
        restored = load(conv_id)
        if restored is not None:
            return conv_id, restored
        SESSIONS[conv_id] = new_state()
        return conv_id, SESSIONS[conv_id]
    new_id = str(uuid.uuid4())
    SESSIONS[new_id] = new_state()
    return new_id, SESSIONS[new_id]


def exists(conv_id: str) -> bool:
    return is_valid_id(conv_id) and (conv_id in SESSIONS or _conv_path(conv_id).exists())


def _meta_from_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _META_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    conv_id = data.get("id") or path.stem
    if not is_valid_id(conv_id):
        return None
    meta = {
        "id": conv_id,
        "title": data.get("title", ""),
        "created_at": data.get("created_at", 0),
        "updated_at": data.get("updated_at", 0),
        "message_count": len(data.get("messages", [])),
        "leak_flags": data.get("leak_flags", 0),
    }
    _META_CACHE[str(path)] = (mtime, meta)
    return meta


def list_all() -> List[Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = {}
    if CONV_DIR.is_dir():
        for path in CONV_DIR.glob("*.json"):
            meta = _meta_from_file(path)
            if meta:
                items[meta["id"]] = meta
    for conv_id, s in SESSIONS.items():
        if s.get("messages"):
            items[conv_id] = {
                "id": conv_id,
                "title": s.get("title", ""),
                "created_at": s.get("created_at", 0),
                "updated_at": s.get("updated_at", 0),
                "message_count": len(s["messages"]),
                "leak_flags": s.get("leak_flags", 0),
            }
    return sorted(items.values(), key=lambda c: c.get("updated_at", 0), reverse=True)


def delete(conv_id: str) -> bool:
    if not is_valid_id(conv_id):
        return False
    removed = SESSIONS.pop(conv_id, None) is not None
    path = _conv_path(conv_id)
    if path.exists():
        path.unlink()
        removed = True
    _META_CACHE.pop(str(path), None)
    folder = IMG_DIR / conv_id
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    return removed
