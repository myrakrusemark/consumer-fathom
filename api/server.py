"""Fathom Consumer API — OpenAI-compat chat completions with delta lake tools."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, crystal, db, delta_client, drift, mood, pressure
from .prompt import (
    CRYSTAL_DIRECTIVE,
    FEED_DIRECTIVE,
    ORIENT_PROMPT,
    build_system_prompt,
    load_feed_directive,
)
from .providers import llm
from .search import search as nl_search
from .settings import settings
from .tools import IMAGE_RESULT_PREFIX, TOOLS, execute

# ── Request / response models ───────────────────


class Message(BaseModel):
    role: str
    content: str | list | None = None
    tool_calls: list | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[Message]
    session_id: str | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    image_uploaded: bool = False  # Skip user message persist — image upload already wrote it


class SessionCreate(BaseModel):
    title: str = "New session"


class SessionUpdate(BaseModel):
    title: str


class SourceCreate(BaseModel):
    source_type: str
    config: dict
    name: str = ""
    interval_minutes: int = 30
    expiry_days: float | None = 30


class SourceUpdate(BaseModel):
    config: dict | None = None
    interval_minutes: int | None = None
    expiry_days: float | None = None


# ── App ─────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await delta_client.close()


app = FastAPI(title="Fathom Consumer API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(auth.TokenAuthMiddleware)


# ── Helpers ─────────────────────────────────────

MAX_TOOL_ROUNDS = 10


def _msg_dicts(messages: list[Message]) -> list[dict]:
    """Convert pydantic models to plain dicts for the openai SDK."""
    out = []
    for m in messages:
        d: dict = {"role": m.role}
        if m.content is not None:
            d["content"] = m.content
        if m.tool_calls is not None:
            d["tool_calls"] = m.tool_calls
        if m.tool_call_id is not None:
            d["tool_call_id"] = m.tool_call_id
        if m.name is not None:
            d["name"] = m.name
        out.append(d)
    return out


async def _resolve_tools(
    messages: list[dict],
    model: str,
    tools: list[dict] | None = None,
    on_tool_event: callable | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
    **kwargs,
) -> list[dict]:
    """Run the tool-calling loop until the LLM stops calling tools.

    Each round: call LLM → if tool_calls, execute them, append results,
    repeat. When the LLM returns text (no tool_calls), stop and return
    the updated messages list with the final text as the last entry.
    """
    tools = tools or TOOLS
    for _ in range(max_rounds):
        resp = await llm.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            **kwargs,
        )
        choice = resp.choices[0]
        msg = choice.message

        if not msg.tool_calls:
            # LLM produced text, not tool calls — we're done resolving.
            # Append the text as an assistant message so the final streaming
            # call has full context if we need to re-call. But for the
            # non-streaming path, this IS the final answer.
            messages.append({"role": "assistant", "content": msg.content or ""})
            return messages

        # Append the assistant's tool_calls message
        messages.append(msg.model_dump(exclude_none=True))

        # Execute each tool call
        for tc in msg.tool_calls:
            fn = tc.function
            try:
                args = json.loads(fn.arguments) if fn.arguments else {}
            except json.JSONDecodeError:
                args = {}

            if on_tool_event:
                on_tool_event("call", fn.name, args)

            result_str = await execute(fn.name, args)

            # Image results become multimodal content blocks
            is_image = result_str.startswith(IMAGE_RESULT_PREFIX)

            if on_tool_event:
                if is_image:
                    on_tool_event("result", fn.name, {"media_hash": args.get("media_hash")})
                else:
                    try:
                        result_data = json.loads(result_str)
                        on_tool_event("result", fn.name, result_data)
                    except Exception:
                        on_tool_event("result", fn.name, {})

            if is_image:
                data_uri = result_str[len(IMAGE_RESULT_PREFIX):]
                media_hash = args.get("media_hash", "?")
                # Gemini doesn't support image_url in tool results.
                # Return text as tool result, then inject the image as a
                # user message so it lands in a supported position.
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": f"Image loaded (media_hash: {media_hash}). See the image in the next message.",
                })
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"[System: here is the image from delta lake, media_hash={media_hash}]"},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                })
            else:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

    # Exceeded max rounds — force a text-only final call so we always get a response
    resp = await llm.chat.completions.create(model=model, messages=messages, **kwargs)
    choice = resp.choices[0]
    messages.append({"role": "assistant", "content": choice.message.content or ""})
    return messages


async def _stream_response(
    messages: list[dict],
    model: str,
    tool_events: list[dict],
    session_id: str | None = None,
    **kwargs,
) -> AsyncGenerator[str, None]:
    """Stream tool events + final LLM text response as SSE.

    Format:
      event: tool_result
      data: {"name": "delta_search", "count": 14}

      data: {"choices": [{"delta": {"content": "token..."}}]}
      ...
      data: [DONE]
    """
    # Phase 1: emit tool events collected during resolution
    for evt in tool_events:
        yield f"event: tool_result\ndata: {json.dumps(evt)}\n\n"

    # Phase 2: stream final text from the last assistant message.
    # The last message in `messages` is the assistant's text response
    # from _resolve_tools. We stream it token-by-token by re-calling
    # the LLM — or if the text is already there, stream it directly.
    last = messages[-1] if messages else {}
    if last.get("role") == "assistant" and last.get("content"):
        # Already have the full text from the non-streaming tool loop.
        # Emit it as a single chunk in OpenAI streaming format.
        chunk = {
            "choices": [{
                "index": 0,
                "delta": {"content": last["content"]},
                "finish_reason": None,
            }],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        done_chunk = {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(done_chunk)}\n\n"
    else:
        # Fallback: re-call the LLM with streaming (no tools)
        stream = await llm.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            **kwargs,
        )
        async for chunk in stream:
            yield f"data: {chunk.model_dump_json()}\n\n"

    meta = {"session_id": session_id} if session_id else {}
    yield f"event: meta\ndata: {json.dumps(meta)}\n\n"
    yield "data: [DONE]\n\n"


# ── Core loop ──────────────────────────────────


async def fathom_think(
    user_message: str,
    directive: str = "",
    history: list[dict] | None = None,
    tools: list[dict] | None = None,
    extra_tools: list[dict] | None = None,
    recall: bool = True,
    session_slug: str | None = None,
    model: str | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
    on_tool_event: callable | None = None,
    **llm_kwargs,
) -> list[dict]:
    """Unified Fathom reasoning loop.

    Every path through the system — chat, feed, crystal — goes through here.
    This guarantees the same voice (SYSTEM_PREAMBLE), the same relationship
    to memories, and the same tool access regardless of task.

    Args:
        tools: Replace the default tool surface entirely. None = TOOLS.
        extra_tools: Append additional tools to whatever base set is active.

    Returns the full messages list with the final assistant response as the
    last entry.
    """
    model = model or settings.resolved_model
    crystal_text = await crystal.latest_text()

    # Mood layer — wake-gated synthesis. May trigger a fresh mood, or just
    # return the most recent one. Failures degrade gracefully (mood = None).
    current_mood = await mood.maybe_synthesize_on_wake(session_slug=session_slug)

    # Resolve tool surface: replace, extend, or default
    resolved_tools = tools if tools is not None else TOOLS
    if extra_tools:
        resolved_tools = resolved_tools + extra_tools

    # 1. Build system prompt — always the full Fathom voice
    system = build_system_prompt(
        crystal_text=crystal_text,
        session_slug=session_slug,
        mood_carrier_wave=(current_mood or {}).get("carrier_wave"),
        mood_threads=(current_mood or {}).get("threads"),
    )

    # Append task-specific directive
    if directive:
        system += f"\n\n--- Task Directive ---\n{directive}\n--- End Directive ---"

    # 2. Assemble message list
    messages: list[dict] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # 3. Recall — proactively surface memories before the main call
    if recall:
        conv_context = ""
        if history:
            recent = [m for m in history if m.get("role") in ("user", "assistant")][-6:]
            conv_context = "\n".join(
                f'{m["role"]}: {(m.get("content") or "")[:200]}' for m in recent
            )

        recalled = await nl_search(
            text=user_message,
            depth="deep",
            session_slug=session_slug,
            conv_context=conv_context,
        )

        if recalled["as_prompt"]:
            inject_msg = {
                "role": "system",
                "content": (
                    f"Here is what you remember ({recalled['total_count']} memories surfaced):\n\n"
                    f"{recalled['as_prompt']}\n\n"
                    "Speak from these naturally — they are your own memories. "
                    "If they don't cover what you need, search deeper."
                ),
            }
            messages.insert(-1, inject_msg)

        if on_tool_event:
            on_tool_event("result", "recall", {"count": recalled["total_count"]})

    # 4. Run the tool loop
    messages = await _resolve_tools(
        messages, model, tools=resolved_tools, on_tool_event=on_tool_event,
        max_rounds=max_rounds, **llm_kwargs,
    )

    return messages


# ── Endpoints ───────────────────────────────────


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    model = req.model or settings.resolved_model

    # Session-aware: load history if session_id provided, or create one
    SHORTTERM_TURNS = 6  # last 3 exchanges (user+assistant each)
    session_id = req.session_id
    if session_id:
        session = await db.get_session(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}, 404
        raw_history = await db.get_messages(session_id)
        history = []
        for m in raw_history:
            if m["role"] not in ("user", "assistant"):
                continue
            content = m.get("content") or ""
            if m.get("media_hash"):
                content += f"\n[Image in this message: media_hash={m['media_hash']}]"
            history.append({"role": m["role"], "content": content})
        history = history[-SHORTTERM_TURNS:]
    else:
        session_data = await db.create_session()
        session_id = session_data["id"]
        history = None

    # Persist the user message(s) — skip if image upload already created the delta
    latest_user_msg = ""
    for m in req.messages:
        if m.role == "user" and m.content:
            content = m.content if isinstance(m.content, str) else json.dumps(m.content)
            if not req.image_uploaded:
                await db.add_message(session_id, "user", content)
            latest_user_msg = content

    extra: dict = {}
    if req.max_tokens:
        extra["max_tokens"] = req.max_tokens
    if req.temperature is not None:
        extra["temperature"] = req.temperature

    tool_events: list[dict] = []

    def on_tool(kind: str, name: str, data: dict):
        if kind == "result":
            evt = {"name": name, "count": data.get("count")}
            if data.get("media_hash"):
                evt["media_hash"] = data["media_hash"]
            tool_events.append(evt)

    messages = await fathom_think(
        user_message=latest_user_msg or _msg_dicts(req.messages)[-1].get("content", ""),
        history=history,
        recall=bool(latest_user_msg),
        session_slug=session_id,
        model=model,
        on_tool_event=on_tool,
        **extra,
    )

    # Extract assistant response text
    last = messages[-1] if messages else {}
    assistant_text = last.get("content", "")

    # Persist assistant response
    if assistant_text:
        await db.add_message(session_id, "assistant", assistant_text)
        await db.touch_session(session_id)

    if req.stream:
        return StreamingResponse(
            _stream_response(messages, model, tool_events, session_id=session_id, **extra),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        return {
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": assistant_text},
                "finish_reason": "stop",
            }],
            "model": model,
            "session_id": session_id,
        }


@app.get("/v1/crystal")
async def get_crystal():
    """Return the current identity crystal (lake-backed)."""
    c = await crystal.latest(force=True)
    if not c:
        raise HTTPException(404, "No crystal generated yet")
    return {
        "text": c["text"],
        "created_at": c["created_at"],
        "id": c["id"],
        "source": c["source"],
    }


@app.post("/v1/crystal/refresh")
async def refresh_crystal():
    """Regenerate the identity crystal via LLM + delta lake tools.

    The lake is the source of truth — the regen delta itself becomes the
    new canonical crystal on the next load. No on-disk file involved.
    """
    messages = await fathom_think(
        user_message=ORIENT_PROMPT,
        directive=CRYSTAL_DIRECTIVE,
        recall=False,  # crystal does its own deep searching via tools
        max_rounds=20,
    )
    last = messages[-1] if messages else {}
    crystal_text = last.get("content", "")

    if crystal_text:
        await crystal.write(crystal_text, source="consumer-api")

        # Push facets to delta store for activation hooks (best-effort)
        facets = _split_facets(crystal_text)
        if facets:
            try:
                c = await delta_client._get()
                await c.post(
                    "/hooks/activation/facets",
                    json={"facets": facets},
                )
            except Exception:
                pass

    return {"status": "ok", "length": len(crystal_text)}


@app.post("/v1/feed/refresh")
async def refresh_feed():
    """Generate new feed stories via LLM + delta lake tools."""
    # Build directive: core feed instructions + optional disk override
    directive_parts = [FEED_DIRECTIVE]
    disk_directive = load_feed_directive()
    if disk_directive:
        directive_parts.append(disk_directive)
    directive = "\n\n".join(directive_parts)

    messages = await fathom_think(
        user_message="Generate 3-6 feed stories from what's in the lake right now.",
        directive=directive,
        recall=True,
    )
    last = messages[-1] if messages else {}
    return {"status": "ok", "response": last.get("content", "")}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{
            "id": settings.resolved_model,
            "object": "model",
            "owned_by": settings.provider,
        }],
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "provider": settings.provider,
        "model": settings.resolved_model,
    }


# ── Session endpoints ───────────────────────────


@app.post("/v1/sessions")
async def create_session(req: SessionCreate):
    return await db.create_session(req.title)


@app.get("/v1/sessions")
async def list_sessions(limit: int = 50):
    sessions = await db.list_sessions(limit)
    # Group by recency for the sidebar
    now = datetime.now(timezone.utc)
    groups: dict[str, list] = {"today": [], "yesterday": [], "last_7_days": [], "older": []}
    for s in sessions:
        created = s["updated_at"]
        if hasattr(created, "date"):
            delta_days = (now.date() - created.date()).days
        else:
            delta_days = 999
        if delta_days == 0:
            groups["today"].append(s)
        elif delta_days == 1:
            groups["yesterday"].append(s)
        elif delta_days <= 7:
            groups["last_7_days"].append(s)
        else:
            groups["older"].append(s)
    # Serialize datetimes
    for group in groups.values():
        for s in group:
            for k in ("created_at", "updated_at"):
                if hasattr(s.get(k), "isoformat"):
                    s[k] = s[k].isoformat()
    return {"groups": groups}


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str):
    session = await db.get_session(session_id)
    if not session:
        return {"error": "not found"}, 404
    messages = await db.get_messages(session_id)
    for k in ("created_at", "updated_at"):
        if hasattr(session.get(k), "isoformat"):
            session[k] = session[k].isoformat()
    return {"session": session, "messages": messages}


@app.patch("/v1/sessions/{session_id}")
async def update_session(session_id: str, req: SessionUpdate):
    result = await db.update_session(session_id, req.title)
    if not result:
        return {"error": "not found"}, 404
    for k in ("created_at", "updated_at"):
        if hasattr(result.get(k), "isoformat"):
            result[k] = result[k].isoformat()
    return result


@app.delete("/v1/sessions/{session_id}")
async def delete_session(session_id: str):
    deleted = await db.delete_session(session_id)
    return {"deleted": deleted}


# ── Feed endpoints ──────────────────────────────


@app.get("/v1/feed/stories")
async def get_feed_stories(limit: int = 20, offset: int = 0):
    """Proxy to delta-store's feed stories endpoint."""
    return await delta_client.feed_stories(limit=limit, offset=offset)


@app.get("/v1/moods/latest")
async def get_latest_mood():
    """Return the most recent mood (carrier wave) plus current pressure state.

    The UI surfaces this as a feed-style card so Myra can see what Fathom
    is carrying right now.
    """
    latest = await mood.latest_mood()
    pressure_state = await pressure.read_pressure()
    pressure_view = {
        "volume": pressure_state["volume"],
        "threshold": pressure_state["threshold"],
        "ratio": (
            pressure_state["volume"] / pressure_state["threshold"]
            if pressure_state["threshold"] > 0 else 0.0
        ),
        "last_synthesis_at": (
            pressure_state["last_synthesis_at"].isoformat()
            if pressure_state["last_synthesis_at"] else None
        ),
        "time_since_synthesis_seconds": pressure_state["time_since_synthesis_seconds"],
    }
    return {"mood": latest, "pressure": pressure_view}


@app.post("/v1/moods/synthesize")
async def force_mood_synthesis():
    """Manually trigger a mood synthesis (for testing / UI refresh button)."""
    fresh = await mood.synthesize_mood()
    if not fresh:
        raise HTTPException(503, "Mood synthesis failed — see logs")
    return fresh


@app.get("/v1/moods/history")
async def get_mood_history(limit: int = 200):
    """Mood timeline for the ECG colored band + state-change events."""
    timeline = await mood.mood_history(limit=limit)
    return {"history": timeline}


@app.get("/v1/pressure/history")
async def get_pressure_history(since_seconds: int | None = None):
    """Rolling pressure samples for the ECG pressure track."""
    items = await pressure.history(since_seconds=since_seconds)
    return {"history": items}


@app.get("/v1/drift")
async def get_drift():
    """Sample current crystal drift and return latest snapshot."""
    return await drift.sample()


@app.get("/v1/drift/history")
async def get_drift_history(since_seconds: int | None = None):
    """Drift samples accumulated from prior /v1/drift calls."""
    items = await drift.history(since_seconds=since_seconds)
    return {"history": items}


@app.get("/v1/crystal/events")
async def get_crystal_events(limit: int = 50):
    """Real crystal regeneration events — strict filter (see api/crystal.py)."""
    return {"events": await crystal.list_events(limit=limit)}


@app.get("/v1/usage")
async def usage():
    """Usage stats for the home screen widget: daily delta counts + totals."""
    from collections import Counter
    stats = await delta_client.stats()
    timestamps = await delta_client.recent_deltas_timestamps(limit=5000)
    day_counts = Counter(timestamps)
    # Build sorted daily series (last 14 days)
    today = datetime.now(timezone.utc).date()
    days = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        ds = d.isoformat()
        days.append({"date": ds, "count": day_counts.get(ds, 0)})
    return {
        "total": stats.get("total", 0),
        "embedded": stats.get("embedded", 0),
        "days": days,
    }


# ── Source endpoints (proxy to source-runner) ──


def _source_runner() -> httpx.AsyncClient:
    """Lazy client for source-runner API."""
    return httpx.AsyncClient(
        base_url=settings.source_runner_url.rstrip("/"),
        timeout=15,
    )


@app.get("/v1/sources")
async def list_sources():
    async with _source_runner() as c:
        r = await c.get("/api/sources")
        r.raise_for_status()
        return r.json()


@app.get("/v1/sources/types")
async def list_source_types():
    async with _source_runner() as c:
        r = await c.get("/api/sources/types")
        r.raise_for_status()
        return r.json()


@app.post("/v1/sources")
async def create_source(req: SourceCreate):
    async with _source_runner() as c:
        r = await c.post("/api/sources", json=req.model_dump())
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.json().get("detail", r.text))
        return r.json()


@app.put("/v1/sources/{source_id}")
async def update_source(source_id: str, req: SourceUpdate):
    # Include explicitly-set fields (even if None, for "forever" expiry)
    body = {k: v for k, v in req.model_dump(exclude_unset=True).items()}
    async with _source_runner() as c:
        r = await c.put(f"/api/sources/{source_id}", json=body)
        if r.status_code == 404:
            raise HTTPException(404, f"Source not found: {source_id}")
        r.raise_for_status()
        return r.json()


@app.post("/v1/sources/{source_id}/pause")
async def pause_source(source_id: str):
    async with _source_runner() as c:
        r = await c.post(f"/api/sources/{source_id}/pause")
        r.raise_for_status()
        return r.json()


@app.post("/v1/sources/{source_id}/resume")
async def resume_source(source_id: str):
    async with _source_runner() as c:
        r = await c.post(f"/api/sources/{source_id}/resume")
        r.raise_for_status()
        return r.json()


@app.post("/v1/sources/{source_id}/poll")
async def poll_source(source_id: str):
    async with _source_runner() as c:
        r = await c.post(f"/api/sources/{source_id}/poll")
        if r.status_code == 404:
            raise HTTPException(404, f"Source not found: {source_id}")
        r.raise_for_status()
        return r.json()


@app.delete("/v1/sources/{source_id}")
async def delete_source(source_id: str):
    async with _source_runner() as c:
        r = await c.delete(f"/api/sources/{source_id}")
        r.raise_for_status()
        return r.json()


@app.get("/v1/sources/{source_id}/detail")
async def source_detail(source_id: str):
    """Fetch source metadata + recent deltas + time-windowed counts."""
    # Get source info from source-runner
    async with _source_runner() as c:
        r = await c.get(f"/api/sources/{source_id}")
        if r.status_code == 404:
            raise HTTPException(404, f"Source not found: {source_id}")
        r.raise_for_status()
        source = r.json()

    # The source field in deltas is "{type}/{id}" for scoped sources
    source_type = source.get("source_type", "")
    delta_source = source.get("source", source_type)
    if delta_source == source_type:
        delta_source = f"{source_type}/{source_id}"

    now = datetime.now(timezone.utc)
    t_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    t_7d = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Fetch recent deltas + counts in parallel
    recent, last_24h, last_7d = await asyncio.gather(
        delta_client.query(limit=20, source=delta_source),
        delta_client.query(limit=1000, source=delta_source, time_start=t_24h),
        delta_client.query(limit=5000, source=delta_source, time_start=t_7d),
    )

    # Slim down recent deltas for the response
    deltas = []
    for d in recent[:20]:
        deltas.append({
            "id": d.get("id"),
            "content": d.get("content") or "",
            "timestamp": d.get("timestamp"),
            "tags": d.get("tags", []),
            "media_hash": d.get("media_hash"),
        })

    return {
        "source": source,
        "counts": {
            "last_24h": len(last_24h),
            "last_7d": len(last_7d),
            "all_time": source.get("deltaCount") or 0,
        },
        "deltas": deltas,
    }


# ── Media proxy ─────────────────────────────────


@app.get("/v1/media/{media_hash}")
async def proxy_media(media_hash: str):
    """Proxy image from delta store so the consumer UI has one API surface."""
    c = await delta_client._get()
    r = await c.get(f"/media/{media_hash}", timeout=15)
    if r.status_code != 200:
        return {"error": "not found"}, 404
    from fastapi.responses import Response
    return Response(content=r.content, media_type="image/webp")


@app.post("/v1/media/upload")
async def upload_media(
    file: UploadFile = File(...),
    session_id: str = Form(""),
    content: str = Form(""),
):
    """Upload an image as a session-tagged delta. Returns {id, media_hash}."""
    file_bytes = await file.read()
    tags = [db.LAKE_CHAT_TAG, "user", "image"]
    if session_id:
        tags.append(f"chat:{session_id}")
    result = await delta_client.upload_media(
        file_bytes=file_bytes,
        filename=file.filename or "upload.jpg",
        content=content,
        tags=tags,
        source=db.LAKE_CHAT_SOURCE,
    )
    return result


class CaptureContext(BaseModel):
    media_hash: str
    content: str = ""
    tags: list[str] = []
    source: str = "browser-capture"


@app.post("/v1/media/capture-context")
async def capture_context(req: CaptureContext):
    """Write a context delta for a browser-captured image.

    The image is already in delta-store (uploaded via /v1/media/upload).
    This writes a companion text delta linking the media_hash to the
    story content so the lake knows what the image means.
    """
    c = await delta_client._get()
    body = {
        "content": req.content or f"[captured image:{req.media_hash}]",
        "tags": req.tags or ["browser-capture"],
        "source": req.source,
        "media_hash": req.media_hash,
        "modality": "image",
    }
    r = await c.post("/deltas", json=body)
    r.raise_for_status()
    return r.json()


# ── Crystal facet parsing ───────────────────────

def _split_facets(text: str) -> list[dict]:
    """Split crystal text on ## headers into facets."""
    facets = []
    current_label = None
    current_lines: list[str] = []

    for line in text.splitlines():
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            if current_label and current_lines:
                facets.append({
                    "label": current_label,
                    "text": "\n".join(current_lines).strip(),
                })
            current_label = m.group(1).strip()
            current_lines = []
        elif current_label is not None:
            current_lines.append(line)

    if current_label and current_lines:
        facets.append({
            "label": current_label,
            "text": "\n".join(current_lines).strip(),
        })

    return facets


# ── Token management ─────────────────────────────


class TokenCreate(BaseModel):
    name: str = ""
    scopes: list[str] | None = None


@app.post("/v1/tokens")
async def create_token(req: TokenCreate):
    return auth.create_token(req.name, req.scopes)


@app.get("/v1/scopes")
async def list_scopes():
    return auth.get_scopes()


@app.get("/v1/tokens")
async def list_tokens():
    return auth.list_tokens()


@app.delete("/v1/tokens/{token_id}")
async def delete_token(token_id: str):
    deleted = auth.delete_token(token_id)
    if not deleted:
        raise HTTPException(404, "Token not found")
    return {"deleted": True}


# ── Tool definitions (served to all clients) ─────

LAKE_TOOLS = [
    {
        "name": "search_lake",
        "description": (
            "Search the memory lake with a natural language query. Returns a "
            "hierarchical trail of memories — conversations, notes, research, "
            "photos, sensor data — as an associative chain (first came to mind, "
            "which reminded me of...). Be descriptive: 'Nova mozzarella stretch "
            "kitchen photo' works better than 'nova'. depth='deep' (default) "
            "runs a multi-step plan; 'shallow' is a single quick similarity search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "depth": {
                    "type": "string",
                    "enum": ["deep", "shallow"],
                    "description": "deep = planner + DAG; shallow = single search.",
                    "default": "deep",
                },
                "limit": {"type": "integer", "description": "Max results per step.", "default": 20},
            },
            "required": ["query"],
        },
        "endpoint": {"method": "POST", "path": "/v1/search"},
        "request_map": {"query": "text", "depth": "depth", "limit": "limit"},
        "scope": "lake:read",
    },
    {
        "name": "write_delta",
        "description": (
            "Write a memory to the lake. Use for observations, decisions, "
            "facts, notes — anything worth remembering. One idea per delta."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The memory content."},
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Tags for filtering (e.g. ['meeting', 'decision']).",
                },
                "source": {"type": "string", "description": "Source label.", "default": "api"},
            },
            "required": ["content"],
        },
        "endpoint": {"method": "POST", "path": "/v1/deltas"},
        "scope": "lake:write",
    },
    {
        "name": "query_deltas",
        "description": (
            "Query the lake with structured filters. Unlike search_lake "
            "(semantic), this is exact filtering by tags, source, or time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Deltas must have ALL these tags.",
                },
                "source": {"type": "string", "description": "Filter by source."},
                "time_start": {"type": "string", "description": "ISO timestamp — only after this."},
                "limit": {"type": "integer", "description": "Max results.", "default": 30},
            },
        },
        "endpoint": {"method": "GET", "path": "/v1/deltas"},
        "request_map": {"tags": "tags_include", "limit": "limit", "source": "source", "time_start": "time_start"},
        "scope": "lake:read",
    },
    {
        "name": "lake_stats",
        "description": (
            "Get lake statistics — total deltas, embedding coverage, top tags. "
            "Quick orientation. Call first if unsure what's in the lake."
        ),
        "parameters": {"type": "object", "properties": {}},
        "endpoint": {"method": "GET", "path": "/v1/stats"},
        "scope": "lake:read",
    },
    {
        "name": "chat",
        "description": (
            "Chat with Fathom — an AI with full memory of the lake. Fathom "
            "searches automatically before responding. Use for questions, "
            "analysis, or conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Your message."},
                "session_id": {"type": "string", "description": "Session ID for conversation continuity."},
            },
            "required": ["message"],
        },
        "endpoint": {"method": "POST", "path": "/v1/chat/completions"},
        "scope": "chat",
    },
]


@app.get("/v1/tools")
async def list_tools(req: Request):
    """Tool definitions filtered by the calling token's scopes.

    Any client — MCP, mobile, enterprise — reads this to discover
    what it can do. Tools the token can't access are omitted.
    Public endpoint, but reads the Bearer token if present for filtering.
    """
    # /v1/tools is public, so middleware doesn't validate. Check manually.
    token = getattr(req.state, "token", None)
    if not token:
        auth_header = req.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth.validate(auth_header[7:])

    if token:
        granted = set(token.get("scopes") or auth.DEFAULT_SCOPES)
        visible = [t for t in LAKE_TOOLS if t.get("scope") in granted]
    else:
        visible = LAKE_TOOLS

    return {"tools": visible, "scopes": auth.get_scopes()}


# ── Delta proxy (unified gateway) ────────────────


@app.post("/v1/search")
async def search_endpoint(request: dict):
    """Canonical NL search. One shape returned to CLI, MCP, hook, and anyone else.

    Request:
        text: the natural-language query.
        depth: "deep" (planner + multi-step plan, default) or "shallow" (single search).
        session_slug: if set, unions session-scoped memories into the plan (deep only).
        limit: cap on raw results per step.
        threshold: shallow-mode distance cutoff (defaults to None = keep all).
    """
    text = request.get("text", "")
    depth = request.get("depth", "deep")
    session_slug = request.get("session_slug")
    limit = int(request.get("limit", 50))
    threshold = request.get("threshold")
    if threshold is not None:
        threshold = float(threshold)
    return await nl_search(
        text=text,
        depth=depth,
        session_slug=session_slug,
        limit=limit,
        threshold=threshold,
    )


@app.post("/v1/deltas")
async def proxy_write_delta(request: dict):
    c = await delta_client._get()
    r = await c.post("/deltas", json=request)
    r.raise_for_status()
    return r.json()


@app.get("/v1/deltas")
async def proxy_query_deltas(
    limit: int = 50,
    tags_include: str | None = None,
    source: str | None = None,
    time_start: str | None = None,
):
    c = await delta_client._get()
    params: dict = {"limit": limit}
    if tags_include:
        params["tags_include"] = tags_include
    if source:
        params["source"] = source
    if time_start:
        params["time_start"] = time_start
    r = await c.get("/deltas", params=params)
    r.raise_for_status()
    return r.json()


@app.get("/v1/deltas/{delta_id}")
async def proxy_get_delta(delta_id: str):
    c = await delta_client._get()
    r = await c.get(f"/deltas/{delta_id}")
    if r.status_code == 404:
        raise HTTPException(404, "Delta not found")
    r.raise_for_status()
    return r.json()


@app.post("/v1/plan")
async def proxy_plan(request: dict):
    c = await delta_client._get()
    r = await c.post("/plan", json=request)
    r.raise_for_status()
    return r.json()


@app.get("/v1/tags")
async def proxy_tags():
    c = await delta_client._get()
    r = await c.get("/tags")
    r.raise_for_status()
    return r.json()


@app.get("/v1/stats")
async def proxy_stats():
    return await delta_client.stats()


# ── Static UI (must be last — catches everything unmatched above) ───

_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
if _UI_DIR.is_dir():

    @app.get("/")
    async def ui_root():
        return FileResponse(_UI_DIR / "index.html")

    app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
