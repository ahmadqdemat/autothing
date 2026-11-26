from __future__ import annotations

import html
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

load_dotenv()
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.gemini import has_api_key, probe_search, search_status
from app.orchestrator import store, handle_chat
from app.schemas import ChatRequest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
INGRESS_PROXY = "172.30.32.2"


def index_html(ingress_path: str) -> str:
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    prefix = ingress_path.strip()
    if not prefix:
        return page
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    href = html.escape(prefix.rstrip("/") + "/", quote=True)
    return page.replace("<head>", f'<head>\n    <base href="{href}" />', 1)


def ingress_allowed(host: str) -> bool:
    if os.getenv("DEBATER_INGRESS_ONLY") != "1":
        return True
    return host == INGRESS_PROXY


class IngressOnlyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host = request.client.host if request.client else ""
        if not ingress_allowed(host):
            return JSONResponse({"detail": "Only the Home Assistant ingress proxy may connect."}, status_code=403)
        return await call_next(request)


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=probe_search, daemon=True).start()
    yield


app = FastAPI(title="Debater", lifespan=lifespan)
app.add_middleware(IngressOnlyMiddleware)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/api/health")
def health() -> dict[str, object]:
    return {"ok": True, "has_key": has_api_key(), "search": search_status()}


@app.get("/api/search")
def search() -> dict[str, str]:
    return search_status()


@app.get("/api/sessions")
def list_sessions() -> dict[str, object]:
    return {"sessions": store.list()}


@app.post("/api/session")
def create_session() -> dict[str, object]:
    session = store.create()
    return store.snapshot(session)


@app.get("/api/session/{session_id}")
def get_session(session_id: str) -> dict[str, object]:
    try:
        session = store.get(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    if not store.is_running(session.id):
        store.clear_stale_run(session)
    return store.snapshot(session)


@app.post("/api/chat")
def chat(request: ChatRequest) -> StreamingResponse:
    if not has_api_key():
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY is missing. Copy .env.example to .env and add your Google AI Studio key.",
        )

    def events():
        for event in handle_chat(
            request.session_id,
            request.message,
            request.images,
            request.confirm_run,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/")
def index(request: Request) -> HTMLResponse:
    return HTMLResponse(index_html(request.headers.get("x-ingress-path", "")))
