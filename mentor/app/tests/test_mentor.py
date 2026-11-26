import base64
import io
import json
import os
import tempfile

os.environ.setdefault("MENTOR_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("MENTOR_OPTIONS_PATH", os.path.join(os.environ["MENTOR_DATA_DIR"], "options.json"))

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from app import config, main, storage

OPTS = {
    "gemini_api_key": "test-key",
    "model": "m",
    "leak_check_model": "lite",
    "unlock_passphrase": "secret123",
    "unlock_idle_timeout_minutes": 30,
    "max_unlock_attempts": 3,
    "lockout_minutes": 15,
    "leak_check_enabled": True,
    "student_age_or_grade": "Grade 8",
    "log_conversations": True,
}


class FakeGemini:
    def __init__(self):
        self.drafts = []
        self.leak = False
        self.calls = []
        self.last_contents = None

    def configure(self, key):
        return bool(key)

    system_instruction = staticmethod(main.GeminiMentorClient.system_instruction)
    build_contents = staticmethod(main.GeminiMentorClient.build_contents)

    async def mentor_reply(self, **kw):
        self.calls.append(("mentor", kw["model"], kw["check_model"], kw["user_text"]))
        self.last_contents = kw["contents"]
        text = self.drafts.pop(0) if self.drafts else "hint"
        return text, self.leak

    async def stream(self, model, system, contents, temperature):
        self.calls.append(("stream", model))
        for p in ["full ", "answer"]:
            yield p


def write_opts(**over):
    data = dict(OPTS, **over)
    with open(config.OPTIONS_PATH, "w") as f:
        json.dump(data, f)
    config._cache["mtime"] = "unset"


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "CONV_DIR", tmp_path / "conversations")
    monkeypatch.setattr(storage, "IMG_DIR", tmp_path / "images")
    storage.SESSIONS.clear()
    storage._META_CACHE.clear()
    main.UNLOCK_GUARD.update(failed=0, until=0.0)
    main.PARENT_TOKENS.clear()
    write_opts()
    fake = FakeGemini()
    monkeypatch.setattr(main, "gemini", fake)
    return fake


def client():
    return AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test")


async def chat(ac, msg, sid=None, image=None):
    headers = {"X-Session-ID": sid} if sid else {}
    res = await ac.post("/api/chat", json={"message": msg, "image_b64": image}, headers=headers)
    assert res.status_code == 200, res.text
    events = [json.loads(line) for line in res.text.splitlines() if line.strip()]
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    return events, text


def jpeg_b64(w=3000, h=2000):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 10, 10)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.mark.asyncio
async def test_mentor_flow_rung_and_persist(setup):
    setup.drafts = ["first hint", "second hint", "<<NEW>> new problem hint"]
    async with client() as ac:
        ev, text = await chat(ac, "Solve 2x + 5 = 17")
        sid = ev[0]["session_id"]
        assert text == "first hint" and ev[-1]["current_rung"] == 2
        _, _ = await chat(ac, "x=5?", sid)
        ev, text = await chat(ac, "another question", sid)
        assert text == "new problem hint"
        assert ev[-1]["current_rung"] == 2
        assert setup.calls[0][2] == "lite"
        res = await ac.get("/api/conversations")
        conv = res.json()["conversations"][0]
        assert conv["id"] == sid and conv["message_count"] == 6
        assert (storage.CONV_DIR / (sid + ".json")).exists()


@pytest.mark.asyncio
async def test_leak_flag_counted(setup):
    setup.leak = True
    async with client() as ac:
        ev, _ = await chat(ac, "give me the answer")
        sid = ev[0]["session_id"]
    assert storage.SESSIONS[sid]["leak_flags"] == 1


@pytest.mark.asyncio
async def test_unlock_command_never_stored_or_sent(setup):
    async with client() as ac:
        ev, text = await chat(ac, "/unlock secret123")
        sid = ev[0]["session_id"]
        assert ev[-1]["is_full_mode"] is True
        assert setup.calls == []
        assert storage.SESSIONS[sid]["messages"] == []
        ev, text = await chat(ac, "now answer", sid)
        assert text == "full answer" and setup.calls[0][0] == "stream"
        ev, text = await chat(ac, "/lock", sid)
        assert ev[-1]["is_full_mode"] is False
    saved = json.loads((storage.CONV_DIR / (sid + ".json")).read_text())
    assert "secret123" not in json.dumps(saved)


@pytest.mark.asyncio
async def test_global_lockout_uses_config(setup):
    async with client() as ac:
        r1 = await ac.post("/api/unlock", json={"passphrase": "bad"})
        r2 = await ac.post("/api/unlock", json={"passphrase": "bad"}, headers={"X-Session-ID": storage.get(None)[0]})
        r3 = await ac.post("/api/unlock", json={"passphrase": "bad"})
        assert (r1.status_code, r2.status_code, r3.status_code) == (401, 401, 429)
        r4 = await ac.post("/api/unlock", json={"passphrase": "secret123"})
        assert r4.status_code == 429
        assert "????" not in r4.json()["detail"]


@pytest.mark.asyncio
async def test_idle_relock(setup):
    async with client() as ac:
        r = await ac.post("/api/unlock", json={"passphrase": "secret123"})
        sid = r.json()["session_id"]
        storage.SESSIONS[sid]["last_activity"] -= 31 * 60
        st = await ac.get("/api/status", headers={"X-Session-ID": sid})
        assert st.json()["is_full_mode"] is False


@pytest.mark.asyncio
async def test_image_shrunk_and_served(setup):
    async with client() as ac:
        ev, _ = await chat(ac, "", image=jpeg_b64())
        sid, name = ev[0]["session_id"], ev[0]["image"]
        r = await ac.get("/api/images/%s/%s" % (sid, name))
        assert r.status_code == 200
        img = Image.open(io.BytesIO(r.content))
        assert max(img.size) == storage.MAX_IMAGE_SIDE and img.format == "JPEG"
        saved = (storage.CONV_DIR / (sid + ".json")).read_text()
        assert "base64" not in saved and len(saved) < 2000


@pytest.mark.asyncio
async def test_only_recent_images_sent(setup):
    async with client() as ac:
        ev, _ = await chat(ac, "a", image=jpeg_b64(50, 50))
        sid = ev[0]["session_id"]
        for _ in range(3):
            await chat(ac, "b", sid, image=jpeg_b64(50, 50))
    parts = [p for c in setup.last_contents for p in c.parts]
    images = [p for p in parts if getattr(p, "inline_data", None)]
    assert len(images) == 2


@pytest.mark.asyncio
async def test_no_logging_keeps_disk_clean(setup):
    write_opts(log_conversations=False)
    async with client() as ac:
        ev, _ = await chat(ac, "hello", image=jpeg_b64(40, 40))
        sid = ev[0]["session_id"]
        r = await ac.get("/api/images/%s/%s" % (sid, ev[0]["image"]))
        assert r.status_code == 200
    assert not storage.CONV_DIR.exists() and not storage.IMG_DIR.exists()


@pytest.mark.asyncio
async def test_migrates_old_inline_images(setup):
    sid = "11111111-1111-4111-8111-111111111111"
    storage.CONV_DIR.mkdir(parents=True)
    old = {"id": sid, "title": "old", "messages": [
        {"role": "user", "content": "q", "image_b64": jpeg_b64(30, 30)},
        {"role": "assistant", "content": "hint"}]}
    (storage.CONV_DIR / (sid + ".json")).write_text(json.dumps(old))
    async with client() as ac:
        r = await ac.get("/api/conversations/" + sid)
        msg = r.json()["messages"][0]
        assert msg["image"] and "image_b64" not in msg
        assert (await ac.get("/api/images/%s/%s" % (sid, msg["image"]))).status_code == 200
    assert "image_b64" not in (storage.CONV_DIR / (sid + ".json")).read_text()


@pytest.mark.asyncio
async def test_parent_view(setup):
    setup.leak = True
    async with client() as ac:
        ev, _ = await chat(ac, "hw")
        sid = ev[0]["session_id"]
        assert (await ac.get("/api/parent/conversations")).status_code == 401
        bad = await ac.post("/api/parent/login", json={"passphrase": "no"})
        assert bad.status_code == 401
        tok = (await ac.post("/api/parent/login", json={"passphrase": "secret123"})).json()["token"]
        h = {"X-Parent-Token": tok}
        data = (await ac.get("/api/parent/conversations", headers=h)).json()
        assert data["totals"] == {"conversations": 1, "messages": 2, "leak_flags": 1}
        conv = (await ac.get("/api/parent/conversations/" + sid, headers=h)).json()
        assert conv["messages"][0]["content"] == "hw"
        assert (await ac.delete("/api/parent/conversations/" + sid, headers=h)).status_code == 200
        assert (await ac.get("/api/conversations")).json()["conversations"] == []


@pytest.mark.asyncio
async def test_missing_key_message(setup):
    write_opts(gemini_api_key="")
    async with client() as ac:
        _, text = await chat(ac, "hi")
    assert "Gemini" in text and "????" not in text


class _Resp:
    def __init__(self, text):
        self.text = text


class _Models:
    def __init__(self, outputs):
        self.outputs = outputs
        self.seen = []

    async def generate_content(self, model, contents, config):
        self.seen.append(model)
        return _Resp(self.outputs.pop(0))


class _Aio:
    def __init__(self, outputs):
        self.models = _Models(outputs)


class _Client:
    def __init__(self, outputs):
        self.aio = _Aio(outputs)


@pytest.mark.asyncio
async def test_real_client_leak_regenerates_once():
    from app.gemini_client import GeminiMentorClient
    g = GeminiMentorClient()
    g.client = _Client(["x = 6", '{"gives_away_answer": true}', "Try isolating x first."])
    text, flagged = await g.mentor_reply(model="big", check_model="lite", system="s", contents=[],
                                         user_text="2x+5=17", latest_image=None, leak_check=True)
    assert flagged and text == "Try isolating x first."
    assert g.client.aio.models.seen == ["big", "lite", "big"]


@pytest.mark.asyncio
async def test_real_client_clean_draft_single_check():
    from app.gemini_client import GeminiMentorClient
    g = GeminiMentorClient()
    g.client = _Client(["What is given?", '{"gives_away_answer": false}'])
    text, flagged = await g.mentor_reply(model="big", check_model="lite", system="s", contents=[],
                                         user_text="ما هو", latest_image=None, leak_check=True)
    assert not flagged and text == "What is given?" and len(g.client.aio.models.seen) == 2


class _PickyModels(_Models):
    async def generate_content(self, model, contents, config):
        if model == "lite" and config.thinking_config is not None:
            self.seen.append(model + "!rejected")
            raise ValueError("400 INVALID_ARGUMENT: thinking level is not supported")
        return await super().generate_content(model, contents, config)


@pytest.mark.asyncio
async def test_thinking_level_applied_and_fallback():
    from app.gemini_client import GeminiMentorClient
    g = GeminiMentorClient()
    g.client = _Client(["hint", '{"gives_away_answer": false}', "hint2", '{"gives_away_answer": false}'])
    g.client.aio.models = _PickyModels(g.client.aio.models.outputs)
    kw = dict(model="big", check_model="lite", system="s", contents=[], user_text="q",
              latest_image=None, leak_check=True, thinking="low")
    await g.mentor_reply(**kw)
    await g.mentor_reply(**kw)
    assert g.client.aio.models.seen == ["big", "lite!rejected", "lite", "big", "lite"]
