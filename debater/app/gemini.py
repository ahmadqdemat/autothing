from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
QUESTION_CAP = 8
RETIRED_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
)
FALLBACK_MODELS = [
    MODEL,
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]


def _model_queue() -> list[str]:
    extra = [
        item.strip()
        for item in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",")
        if item.strip()
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in extra + FALLBACK_MODELS:
        if name in seen or name in RETIRED_MODELS:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _is_quota(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in ("429", "resource exhausted", "quota")
    )


def _is_busy(exc: Exception) -> bool:
    if _is_quota(exc):
        return False
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "503",
            "unavailable",
            "high demand",
            "overloaded",
        )
    )


def _is_timeout(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "10060",
            "504",
            "deadline_exceeded",
            "deadline expired",
            "timed out",
            "timeout",
            "did not properly respond",
            "failed to respond",
            "connection attempt failed",
            "connecterror",
            "readtimeout",
            "connecttimeout",
        )
    )


def _is_bad_model(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "not found",
            "not supported",
            "unknown model",
            "404",
            "no longer available",
        )
    )

_client: genai.Client | None = None


def has_api_key() -> bool:
    return bool(os.getenv("GEMINI_API_KEY", "").strip())


def get_client() -> genai.Client:
    global _client
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Copy .env.example to .env and add your key from Google AI Studio."
        )
    if _client is None:
        _client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=60000),
        )
    return _client


def _parse_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def build_input(text: str, images: list[dict[str, str]] | None = None) -> Any:
    if not images:
        return text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for image in images:
        blocks.append(
            {
                "type": "image",
                "data": image["data"],
                "mime_type": image["mime_type"],
            }
        )
    return blocks


def _reraise_last(last_error: Exception) -> None:
    if _is_timeout(last_error):
        raise RuntimeError(
            "Gemini did not respond in time. Check your internet, then press Try the debate again once. Do not keep clicking."
        ) from last_error
    if _is_quota(last_error):
        raise RuntimeError(
            "Gemini search quota is exhausted. The analysis will use only the decision brief until the quota resets."
        ) from last_error
    if _is_busy(last_error):
        raise RuntimeError(
            "Gemini is busy right now. Wait a few seconds and send again, or set GEMINI_MODEL in .env to another Gemini id."
        ) from last_error
    raise last_error


def _call_models(contents: Any, config: dict[str, Any]) -> Any:
    client = get_client()
    last_error: Exception | None = None
    for model in _model_queue():
        for attempt in range(2):
            try:
                return client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                last_error = exc
                if _is_quota(exc):
                    _reraise_last(exc)
                if _is_bad_model(exc) or _is_timeout(exc):
                    break
                if _is_busy(exc):
                    time.sleep(1.2 * (attempt + 1))
                    continue
                raise
    assert last_error is not None
    _reraise_last(last_error)


def _content_tools(tools: list[dict[str, str]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    mapped: list[dict[str, Any]] = []
    for tool in tools:
        kind = tool.get("type")
        if kind == "google_search":
            mapped.append({"google_search": {}})
        elif kind == "url_context":
            mapped.append({"url_context": {}})
    return mapped or None


def generate_grounded_text(*, system: str, prompt: str) -> str:
    config: dict[str, Any] = {
        "system_instruction": system,
        "tools": [{"google_search": {}}],
        "http_options": types.HttpOptions(timeout=60000),
    }
    response = _call_models(prompt, config)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Search returned an empty brief.")
    return text


_search_state: dict[str, str] = {"state": "checking", "detail": ""}


def search_status() -> dict[str, str]:
    return dict(_search_state)


def probe_search() -> dict[str, str]:
    global _search_state
    if not has_api_key():
        _search_state = {
            "state": "unavailable",
            "detail": "GEMINI_API_KEY is missing, so the debate cannot search.",
        }
        return dict(_search_state)
    config: dict[str, Any] = {
        "system_instruction": "Reply with one short sentence. Use search.",
        "tools": [{"google_search": {}}],
        "http_options": types.HttpOptions(timeout=20000),
    }
    try:
        response = _call_models("What is today's date?", config)
    except Exception as exc:
        if _is_quota(exc):
            _search_state = {
                "state": "unavailable",
                "detail": (
                    "Google Search grounding is not available on this API key's tier. "
                    "Analysis will use only the decision brief, and unsupported claims will remain unverified."
                ),
            }
        else:
            _search_state = {"state": "unknown", "detail": str(exc)[:300]}
        return dict(_search_state)
    if (response.text or "").strip():
        _search_state = {"state": "ok", "detail": ""}
    else:
        _search_state = {"state": "unknown", "detail": "Search returned an empty answer."}
    return dict(_search_state)


def generate_json(
    *,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    images: list[dict[str, str]] | None = None,
    previous_interaction_id: str | None = None,
    tools: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], str | None]:
    del previous_interaction_id
    last_error: Exception | None = None
    parts: list[types.Part] = [types.Part.from_text(text=prompt)]
    for image in images or []:
        raw = image["data"]
        padding = (-len(raw)) % 4
        blob = base64.b64decode(raw + ("=" * padding))
        parts.append(types.Part.from_bytes(data=blob, mime_type=image["mime_type"]))

    for toolset in ((tools,) if tools else ()) + (None,):
        config: dict[str, Any] = {
            "system_instruction": system,
            "response_mime_type": "application/json",
            "response_json_schema": schema,
            "automatic_function_calling": {"disable": True},
            "http_options": types.HttpOptions(timeout=60000),
        }
        mapped = _content_tools(toolset)
        if mapped:
            config["tools"] = mapped
        try:
            response = _call_models(parts, config)
            return _parse_json(response.text or ""), None
        except Exception as exc:
            last_error = exc
            if tools and toolset is not None:
                continue
            break

    assert last_error is not None
    _reraise_last(last_error)
