import base64
import hmac
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import storage
from .config import get_options
from .gemini_client import NEW_PROBLEM_MARK, GeminiMentorClient, fallback_hint, is_arabic

VERSION = "14.2.0"
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STREAM_CHUNK = 24
PARENT_TOKEN_TTL = 30 * 60
MAX_IMAGE_BYTES = 15 * 1024 * 1024

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mentor")

gemini = GeminiMentorClient()
app = FastAPI(title="Mentor Tutor Assistant")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Unlock attempts are counted for the whole add-on, so a new session can't reset them.
UNLOCK_GUARD = {"failed": 0, "until": 0.0}
PARENT_TOKENS: Dict[str, float] = {}

MSG = {
    "no_passphrase": "لم يتم تعيين كلمة مرور للفتح في إعدادات الإضافة.",
    "locked_out": "تم إيقاف محاولات فتح القفل مؤقتاً. حاول مرة أخرى بعد {m} دقيقة.",
    "too_many": "محاولات كثيرة خاطئة. تم إيقاف المحاولات لمدة {m} دقيقة.",
    "wrong": "كلمة المرور غير صحيحة. المتبقي: {n} محاولات.",
    "unlocked": "تم فتح الوضع الكامل.",
    "locked": "تم الرجوع إلى وضع المعلم.",
    "no_key": "لم يتم إعداد مفتاح Gemini API. أدخل مفتاحاً صالحاً في إعدادات الإضافة.",
    "ai_error": "تعذر الحصول على رد من Gemini الآن. حاول مرة أخرى بعد قليل.",
    "bad_image": "تعذر قراءة الصورة. جرّب صورة أخرى.",
    "parent_auth": "يلزم تسجيل الدخول كولي أمر.",
    "not_found": "المحادثة غير موجودة.",
}


class ChatRequest(BaseModel):
    message: str = ""
    image_b64: Optional[str] = None


class PassRequest(BaseModel):
    passphrase: str


# ---------- helpers ----------

def apply_idle_lock(session: dict, opts: dict) -> None:
    timeout = int(opts.get("unlock_idle_timeout_minutes") or 0) * 60
    now = time.time()
    if session.get("is_full_mode") and timeout and now - session.get("last_activity", now) > timeout:
        session["is_full_mode"] = False
        session["current_rung"] = 1
    session["last_activity"] = now


def check_passphrase(given: str, opts: dict) -> None:
    """Raises HTTPException unless the passphrase is right. Shared by unlock and parent login."""
    correct = opts.get("unlock_passphrase") or ""
    max_attempts = int(opts.get("max_unlock_attempts") or 5)
    minutes = int(opts.get("lockout_minutes") or 15)
    if not correct:
        raise HTTPException(400, MSG["no_passphrase"])
    if time.time() < UNLOCK_GUARD["until"]:
        raise HTTPException(429, MSG["locked_out"].format(m=minutes))
    if hmac.compare_digest((given or "").encode("utf-8"), correct.encode("utf-8")):
        UNLOCK_GUARD["failed"] = 0
        return
    UNLOCK_GUARD["failed"] += 1
    left = max_attempts - UNLOCK_GUARD["failed"]
    if left <= 0:
        UNLOCK_GUARD["failed"] = 0
        UNLOCK_GUARD["until"] = time.time() + minutes * 60
        raise HTTPException(429, MSG["too_many"].format(m=minutes))
    raise HTTPException(401, MSG["wrong"].format(n=left))


def public_conversation(conv_id: str, s: dict) -> dict:
    return {
        "id": conv_id,
        "title": s.get("title", ""),
        "created_at": s.get("created_at"),
        "updated_at": s.get("updated_at"),
        "is_full_mode": s.get("is_full_mode", False),
        "current_rung": s.get("current_rung", 1),
        "leak_flags": s.get("leak_flags", 0),
        "messages": [
            {"role": m.get("role"), "content": m.get("content", ""), "image": m.get("image")}
            for m in s.get("messages", [])
        ],
    }


def ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def require_parent(token: Optional[str]) -> None:
    now = time.time()
    for t, exp in list(PARENT_TOKENS.items()):
        if exp < now:
            PARENT_TOKENS.pop(t, None)
    if not token or token not in PARENT_TOKENS:
        raise HTTPException(401, MSG["parent_auth"])
    PARENT_TOKENS[token] = now + PARENT_TOKEN_TTL


# ---------- pages ----------

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
async def get_status(x_session_id: Optional[str] = Header(None)):
    opts = get_options()
    conv_id, s = storage.get(x_session_id)
    apply_idle_lock(s, opts)
    return {"session_id": conv_id, "is_full_mode": s["is_full_mode"],
            "current_rung": s["current_rung"], "version": VERSION}


# ---------- conversations (student) ----------

@app.get("/api/conversations")
async def list_conversations():
    items = [{k: c[k] for k in ("id", "title", "created_at", "updated_at", "message_count")}
             for c in storage.list_all()]
    return {"conversations": items}


@app.post("/api/conversations")
async def create_conversation():
    conv_id, s = storage.get(None)
    return public_conversation(conv_id, s)


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    if not storage.exists(conv_id):
        raise HTTPException(404, MSG["not_found"])
    conv_id, s = storage.get(conv_id)
    apply_idle_lock(s, get_options())
    return public_conversation(conv_id, s)


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    if not storage.delete(conv_id):
        raise HTTPException(404, MSG["not_found"])
    return {"status": "deleted", "id": conv_id}


@app.get("/api/images/{conv_id}/{name}")
async def get_image(conv_id: str, name: str):
    data = storage.load_image(conv_id, name, storage.SESSIONS.get(conv_id))
    if data is None:
        raise HTTPException(404, MSG["not_found"])
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000"})


# ---------- chat ----------

def _command(text: str):
    t = (text or "").strip()
    low = t.lower()
    if low == "/lock":
        return "lock", ""
    if low.startswith("/unlock"):
        return "unlock", t[len("/unlock"):].strip()
    return None, None


@app.post("/api/chat")
async def chat(req: ChatRequest, x_session_id: Optional[str] = Header(None)):
    opts = get_options()
    conv_id, s = storage.get(x_session_id)
    apply_idle_lock(s, opts)
    persist = bool(opts.get("log_conversations"))

    # Chat commands are handled here and never reach Gemini or the saved log.
    cmd, arg = _command(req.message)
    if cmd:
        if cmd == "lock":
            s["is_full_mode"] = False
            s["current_rung"] = 1
            reply = MSG["locked"]
        else:
            try:
                check_passphrase(arg, opts)
                s["is_full_mode"] = True
                reply = MSG["unlocked"]
            except HTTPException as exc:
                reply = exc.detail

        async def cmd_stream():
            yield ndjson({"type": "meta", "session_id": conv_id})
            yield ndjson({"type": "delta", "text": reply})
            yield ndjson({"type": "done", "is_full_mode": s["is_full_mode"],
                          "current_rung": s["current_rung"], "title": s.get("title", ""), "system": True})
        return StreamingResponse(cmd_stream(), media_type="application/x-ndjson")

    image_name = None
    image_bytes = None
    if req.image_b64:
        try:
            raw = base64.b64decode(req.image_b64.split(",")[-1])
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError("too large")
            image_name = storage.store_image(conv_id, s, raw, persist)
            image_bytes = storage.load_image(conv_id, image_name, s)
        except Exception:
            raise HTTPException(400, MSG["bad_image"])

    s["messages"].append({"role": "user", "content": req.message, "image": image_name, "ts": time.time()})

    async def run():
        yield ndjson({"type": "meta", "session_id": conv_id, "image": image_name})
        if not gemini.configure(opts.get("gemini_api_key", "")):
            reply = MSG["no_key"]
            yield ndjson({"type": "delta", "text": reply})
        else:
            model = opts.get("model") or "gemini-2.5-flash"
            check_model = opts.get("leak_check_model") or model
            system = gemini.system_instruction(s["is_full_mode"], s["current_rung"], opts.get("student_age_or_grade"))
            contents = gemini.build_contents(s["messages"], lambda n: storage.load_image(conv_id, n, s))
            reply = ""
            try:
                if s["is_full_mode"]:
                    async for piece in gemini.stream(model, system, contents, 0.7):
                        reply += piece
                        yield ndjson({"type": "delta", "text": piece})
                else:
                    text, flagged = await gemini.mentor_reply(
                        model=model, check_model=check_model, system=system, contents=contents,
                        user_text=req.message, latest_image=image_bytes,
                        leak_check=bool(opts.get("leak_check_enabled", True)),
                        thinking=(opts.get("thinking_level") or "").strip().lower() or None)
                    new_problem = NEW_PROBLEM_MARK in text
                    reply = text.replace(NEW_PROBLEM_MARK, "").strip() or fallback_hint(req.message)
                    if flagged:
                        s["leak_flags"] = s.get("leak_flags", 0) + 1
                    s["current_rung"] = 2 if new_problem else min(5, s["current_rung"] + 1)
                    for i in range(0, len(reply), STREAM_CHUNK):
                        yield ndjson({"type": "delta", "text": reply[i:i + STREAM_CHUNK]})
            except Exception as exc:
                log.warning("Gemini error: %s", exc)
                if not reply:
                    reply = MSG["ai_error"] if is_arabic(req.message) or not req.message else \
                        "Couldn't get a reply from Gemini right now. Please try again shortly."
                    yield ndjson({"type": "delta", "text": reply})
        s["messages"].append({"role": "assistant", "content": reply, "ts": time.time()})
        storage.save(conv_id, s, persist)
        yield ndjson({"type": "done", "is_full_mode": s["is_full_mode"],
                      "current_rung": s["current_rung"], "title": s.get("title", "")})

    return StreamingResponse(run(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/unlock")
async def unlock(req: PassRequest, x_session_id: Optional[str] = Header(None)):
    opts = get_options()
    conv_id, s = storage.get(x_session_id)
    check_passphrase(req.passphrase, opts)
    s["is_full_mode"] = True
    s["last_activity"] = time.time()
    return {"status": "unlocked", "session_id": conv_id, "is_full_mode": True, "current_rung": s["current_rung"]}


@app.post("/api/lock")
async def lock(x_session_id: Optional[str] = Header(None)):
    conv_id, s = storage.get(x_session_id)
    s["is_full_mode"] = False
    s["current_rung"] = 1
    return {"status": "locked", "session_id": conv_id, "is_full_mode": False, "current_rung": 1}


# ---------- parent view ----------

@app.post("/api/parent/login")
async def parent_login(req: PassRequest):
    check_passphrase(req.passphrase, get_options())
    token = secrets.token_urlsafe(32)
    PARENT_TOKENS[token] = time.time() + PARENT_TOKEN_TTL
    return {"token": token, "expires_in": PARENT_TOKEN_TTL}


@app.post("/api/parent/logout")
async def parent_logout(x_parent_token: Optional[str] = Header(None)):
    PARENT_TOKENS.pop(x_parent_token or "", None)
    return {"status": "ok"}


@app.get("/api/parent/conversations")
async def parent_list(x_parent_token: Optional[str] = Header(None)):
    require_parent(x_parent_token)
    opts = get_options()
    items = storage.list_all()
    return {
        "conversations": items,
        "logging": bool(opts.get("log_conversations")),
        "totals": {
            "conversations": len(items),
            "messages": sum(c["message_count"] for c in items),
            "leak_flags": sum(c.get("leak_flags", 0) for c in items),
        },
    }


@app.get("/api/parent/conversations/{conv_id}")
async def parent_get(conv_id: str, x_parent_token: Optional[str] = Header(None)):
    require_parent(x_parent_token)
    if not storage.exists(conv_id):
        raise HTTPException(404, MSG["not_found"])
    conv_id, s = storage.get(conv_id)
    return public_conversation(conv_id, s)


@app.delete("/api/parent/conversations/{conv_id}")
async def parent_delete(conv_id: str, x_parent_token: Optional[str] = Header(None)):
    require_parent(x_parent_token)
    if not storage.delete(conv_id):
        raise HTTPException(404, MSG["not_found"])
    return {"status": "deleted", "id": conv_id}
