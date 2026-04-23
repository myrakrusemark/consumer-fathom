"""Fathom Consumer API — OpenAI-compat chat completions with delta lake tools."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncGenerator

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, auto_regen, contacts as contacts_mod, crystal, crystal_anchor, db, delta_client, drift, feed_crystal, feed_loop, mood, pairing, pressure, recall, reserved_tags, routines as routines_mod, usage as usage_module

log = logging.getLogger(__name__)
from .prompt import (
    CRYSTAL_DIRECTIVE,
    CRYSTAL_REGEN_SYSTEM,
    FEED_DIRECTIVE,
    ORIENT_PROMPT,
    build_system_prompt,
    load_feed_directive,
)
from .providers import llm
from .search import search as nl_search
from .settings import settings
from .tools import IMAGE_RESULT_PREFIX, TOOLS, execute, heartbeat_age_seconds, heartbeat_is_fresh

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


class FeedEngagementRequest(BaseModel):
    kind: str  # "more" | "less" | "chat"
    card_id: str
    topic: str | None = None
    card_excerpt: str | None = None
    chat_session: str | None = None


class EngagementRequest(BaseModel):
    """Generic engagement on any delta — sediment, memory, whatever.

    `kind` is the relationship type: `refutes`, `affirms`, or `reply-to`.
    The tag written is `<kind>:<target_id>`; content is the caller's prose.
    Use /v1/feed/engagement for the feed-specific +/− shape; use this one
    for repair of bad sediment and for lake-wide engagement signals.
    """

    target_id: str
    kind: str  # "refutes" | "affirms" | "reply-to"
    reason: str = ""


# ── App ─────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from . import chat_listener
    # Resolve the first-admin slug up front so the legacy-token migration
    # and any contact-tag backfill have a target. On a fresh install with
    # no admin yet, this returns None and both operations become no-ops
    # until bootstrap runs. Retries because delta-store may still be
    # booting when api starts.
    import asyncio as _asyncio
    resolved_admin: str | None = None
    for attempt in range(6):
        try:
            resolved_admin = await contacts_mod.first_admin_slug()
            break
        except Exception:
            if attempt == 5:
                log.exception("lifespan: first_admin_slug failed after retries")
            else:
                await _asyncio.sleep(2 ** attempt)
    if resolved_admin:
        migrated = auth.migrate_legacy_tokens(default_slug=resolved_admin)
        if migrated:
            log.info("Bound %d legacy tokens to contact '%s'", migrated, resolved_admin)

    # One-shot backfill of contact:<admin> onto per-user deltas that
    # predate the contact registry. Idempotent — skips deltas that
    # already carry any contact: tag, so re-runs are no-ops. Only fires
    # once an admin exists; on pre-bootstrap installs the lake is empty
    # and there's nothing to backfill anyway.
    async def _backfill_once(admin_slug: str):
        for attempt in range(6):  # ~30s total with backoff
            try:
                result = await delta_client.backfill_contact_tag(
                    contact_slug=admin_slug,
                    filter_tags=[
                        "feed-engagement",
                        "feed-story",
                        "feed-card",
                        "crystal:feed-orient",
                    ],
                )
                if result.get("updated"):
                    log.info(
                        "Backfilled contact:%s on %d legacy feed deltas",
                        admin_slug,
                        result.get("updated"),
                    )
                return
            except Exception:
                if attempt == 5:
                    log.exception("contact backfill failed after retries (non-fatal)")
                    return
                await _asyncio.sleep(2 ** attempt)

    if resolved_admin:
        _asyncio.create_task(_backfill_once(resolved_admin))

    auto_regen.start()
    chat_listener.listener.start()
    try:
        yield
    finally:
        await chat_listener.listener.stop()
        await auto_regen.stop()
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

def _current_contact_slug(request: Request) -> str:
    """Resolve the authenticated caller's contact slug.

    Returns an empty string on pre-bootstrap installs where no admin
    exists yet — in practice those paths shouldn't fire because the UI
    gates on /v1/auth/bootstrap-status and redirects to onboarding
    before any contact-scoped endpoint is reached. After bootstrap the
    auth middleware always populates request.state.contact (either from
    the caller's token or, on tokenless first-run, from the first-admin
    fallback)."""
    contact = getattr(request.state, "contact", None)
    return (contact or {}).get("slug") or ""


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
    session_id: str | None = None,
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

            result_str = await execute(fn.name, args, session_id=session_id)

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
      data: {"name": "remember", "count": 14}

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
    system_override: str | None = None,
    **llm_kwargs,
) -> list[dict]:
    """Unified Fathom reasoning loop.

    Every path through the system — chat, feed, crystal — goes through here.
    This guarantees the same voice (SYSTEM_PREAMBLE), the same relationship
    to memories, and the same tool access regardless of task.

    Args:
        tools: Replace the default tool surface entirely. None = TOOLS.
        extra_tools: Append additional tools to whatever base set is active.
        system_override: Replace the built system prompt entirely. Used by
            crystal regen so the synthesis isn't polluted by SYSTEM_PREAMBLE
            rules, prior-crystal injection, or mood layer — the regen should
            look at itself from outside, not BE itself reading itself.

    Returns the full messages list with the final assistant response as the
    last entry.
    """
    model = model or settings.resolved_model

    # Resolve tool surface: replace, extend, or default
    resolved_tools = tools if tools is not None else TOOLS
    if extra_tools:
        resolved_tools = resolved_tools + extra_tools

    # 1. Build system prompt — default path is the full Fathom voice;
    # callers that need a clean frame (crystal regen) pass system_override.
    if system_override is not None:
        system = system_override
    else:
        crystal_text = await crystal.latest_text()
        current_mood = await mood.maybe_synthesize_on_wake(session_slug=session_slug)
        session_title: str | None = None
        if session_slug:
            sess = await db.get_session(session_slug)
            if sess:
                session_title = sess.get("title")
        from .tools import _agent_alive
        agent_connected, agents_info = await _agent_alive()
        # Known contacts hydrate the "who is Fathom talking to + about"
        # context. Merged with session-addressee so the model can propose
        # new contacts instead of hallucinating slugs. list_all returns
        # a small set (typically <20); the query is 60s-cached elsewhere.
        try:
            known_contacts = await contacts_mod.list_all()
        except Exception:
            known_contacts = []
        current_contact_slug: str | None = None
        if session_slug:
            # The addressee of this chat session — whoever's contact: tag
            # appears on the user deltas in this thread. Read off the
            # most recent user delta via the session history.
            try:
                latest = await delta_client.query(
                    tags_include=[f"chat:{session_slug}", "participant:user"],
                    limit=1,
                )
                if latest:
                    for t in latest[0].get("tags") or []:
                        if isinstance(t, str) and t.startswith("contact:"):
                            current_contact_slug = t.split(":", 1)[1]
                            break
            except Exception:
                pass
        # Resolve the addressee's timezone so "Current time" in the prompt
        # matches the clock rendered in the UI opener stamp. known_contacts
        # is already fetched above, so no extra round-trip.
        user_timezone: str | None = None
        if current_contact_slug and known_contacts:
            for c in known_contacts:
                if c.get("slug") == current_contact_slug:
                    tz_raw = c.get("timezone")
                    if isinstance(tz_raw, str) and tz_raw.strip():
                        user_timezone = tz_raw.strip()
                    break
        system = build_system_prompt(
            crystal_text=crystal_text,
            session_slug=session_slug,
            session_title=session_title,
            mood_carrier_wave=(current_mood or {}).get("carrier_wave"),
            mood_threads=(current_mood or {}).get("threads"),
            agent_connected=agent_connected,
            agent_hosts=[a.get("host", "") for a in agents_info if a.get("host")],
            known_contacts=known_contacts,
            current_contact_slug=current_contact_slug,
            user_timezone=user_timezone,
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
                    f"You remember these things ({recalled['total_count']} surfaced):\n\n"
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
        max_rounds=max_rounds, session_id=session_slug, **llm_kwargs,
    )

    return messages


# ── Endpoints ───────────────────────────────────


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    """Write a user message into a chat session and return immediately.

    Fathom's response comes via the chat listener (api/chat_listener.py),
    which polls the lake for new deltas and fires inference per session.
    This endpoint is no longer the inference trigger — it only persists
    the user's delta. The UI picks up Fathom's eventual reply through
    the same poll-the-session cycle that surfaces agent/body messages.

    Why: every participant in a chat (user, Fathom, local bodies, other
    humans in the future) should fire the same way — drop a delta,
    everyone who's listening takes their turn. Previously Fathom only
    responded to HTTP requests; now Fathom responds to deltas. Symmetric.
    """
    # Session-aware: create one if not provided
    session_id = req.session_id
    if session_id:
        session = await db.get_session(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}, 404
    else:
        session_data = await db.create_session()
        session_id = session_data["id"]

    # Persist the user message(s). Image uploads already wrote their own
    # delta via /v1/media so we skip writing a duplicate text delta when
    # image_uploaded is set. The write itself is what triggers the chat
    # listener to take a turn — no inference runs here synchronously.
    contact = getattr(request.state, "contact", None)
    contact_slug = (contact or {}).get("slug")
    for m in req.messages:
        if m.role == "user" and m.content:
            content = m.content if isinstance(m.content, str) else json.dumps(m.content)
            if not req.image_uploaded:
                await db.add_message(
                    session_id, "user", content, contact_slug=contact_slug
                )

    # Return session_id so the UI can lock onto it for its poll cycle.
    # No streaming response — there's nothing to stream. The chat listener
    # has already (or will shortly) pick up the user delta and write
    # Fathom's reply, which the UI's 3-second poll will surface.
    return {"session_id": session_id}


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


CRYSTAL_MIN_CHARS = 800
CRYSTAL_ACCEPT_MIN = 0.05
CRYSTAL_ACCEPT_MAX = 0.5


async def _generate_crystal_candidate(retry_hint: str | None = None) -> str:
    """Run one fathom_think pass for crystal regen. Returns the text."""
    directive = CRYSTAL_DIRECTIVE
    if retry_hint:
        directive += (
            "\n\nYour previous attempt was rejected: "
            f"{retry_hint}. Read more from the lake before writing, and "
            "produce a grounded, multi-section synthesis."
        )
    messages = await fathom_think(
        user_message=ORIENT_PROMPT,
        directive=directive,
        system_override=CRYSTAL_REGEN_SYSTEM,
        recall=False,  # crystal does its own deep searching via tools
        max_rounds=20,
    )
    last = messages[-1] if messages else {}
    return last.get("content", "") or ""


async def _validate_crystal_candidate(text: str) -> str | None:
    """Return a rejection reason or None if the candidate passes gates.

    Gate 1 — length: failure-mode outputs tend to be short paragraphs
    (200-500 chars). Real crystals are multi-section (1500-3000+).

    Gate 2 — semantic band: cosine distance from the lake centroid must
    sit in a reasonable window. Too low (< 0.05) means the text parrots
    the lake without synthesis; too high (> 0.5) means the text doesn't
    describe what's in the lake at all (e.g. the "I can't remember my
    memories" failure mode). Values come from observed good-crystal
    distances clustering around 0.2-0.3.
    """
    if len(text) < CRYSTAL_MIN_CHARS:
        return f"too short ({len(text)} chars, need {CRYSTAL_MIN_CHARS})"
    try:
        d = await delta_client.drift(text)
    except Exception as e:
        return f"drift check failed: {type(e).__name__}: {e}"
    drift_value = float(d.get("drift", 0.0))
    if drift_value < CRYSTAL_ACCEPT_MIN:
        return (
            f"too aligned with lake (drift={drift_value:.3f} < "
            f"{CRYSTAL_ACCEPT_MIN}, looks like a parroted summary)"
        )
    if drift_value > CRYSTAL_ACCEPT_MAX:
        return (
            f"too far from lake (drift={drift_value:.3f} > "
            f"{CRYSTAL_ACCEPT_MAX}, doesn't describe current state)"
        )
    return None


async def _record_rejected_candidate(text: str, reason: str) -> None:
    """Preserve a rejected candidate in the lake for forensics.

    Tagged crystal-reject — invisible to the crystal-regen detection
    rule so it doesn't show up on the identity ECG, but searchable
    later to diagnose what the LLM produced.
    """
    try:
        await delta_client.write(
            content=(text or "(empty)")[:4000] + f"\n\n[rejected: {reason}]",
            tags=["crystal-reject"],
            source="consumer-api",
        )
    except Exception:
        log.exception("failed to record rejected crystal candidate")


@app.post("/v1/crystal/refresh")
async def refresh_crystal():
    """Regenerate the identity crystal via LLM + delta lake tools.

    Gates a candidate through length + drift-band validation before
    persisting. On accept: writes the crystal to the lake, snapshots
    the current lake centroid as the drift anchor (so drift ≡ 0 by
    construction right after regen), and samples drift to seed the
    ECG history. On reject: runs one retry with a corrective hint;
    if that also fails, preserves both candidates as crystal-reject
    deltas for forensics and returns without writing a crystal.
    """
    text = await _generate_crystal_candidate()
    reason = await _validate_crystal_candidate(text)

    if reason:
        log.warning("crystal regen attempt 1 rejected: %s", reason)
        await _record_rejected_candidate(text, reason)
        text = await _generate_crystal_candidate(retry_hint=reason)
        reason = await _validate_crystal_candidate(text)
        if reason:
            log.warning("crystal regen attempt 2 rejected: %s", reason)
            await _record_rejected_candidate(text, reason)
            return {
                "status": "rejected",
                "reason": reason,
                "length": len(text),
            }

    # Accepted — persist crystal first, then snapshot anchor against the
    # post-write lake (one new delta barely perturbs the centroid, so the
    # ECG's first drift tick reads ~0 as intended).
    written = await crystal.write(text, source="consumer-api")
    try:
        c = await delta_client.centroid()
        vec = c.get("centroid")
        if vec:
            await crystal_anchor.save(vec, (written or {}).get("id"))
    except Exception:
        log.exception("failed to snapshot crystal anchor")

    # Seed the drift history with the fresh zero-ish reading.
    try:
        await drift.sample()
    except Exception:
        log.exception("failed to seed post-regen drift sample")

    # Push facets to delta store for activation hooks (best-effort)
    facets = _split_facets(text)
    if facets:
        try:
            c = await delta_client._get()
            await c.post(
                "/hooks/activation/facets",
                json={"facets": facets},
            )
        except Exception:
            pass

    return {"status": "ok", "length": len(text)}


@app.post("/v1/feed/refresh")
async def refresh_feed(request: Request):
    """Manual kick of the feed loop, bypassing the visit-debounce.

    Still respects the per-contact single-flight lock — repeated calls
    during a fire return `fired=False, reason=already-running`. Useful
    for debugging and for any external trigger that wants to force a
    regen.
    """
    slug = _current_contact_slug(request)
    return await feed_loop.force_fire(slug, reason="manual-refresh")


@app.post("/v1/feed/visit")
async def feed_visit(request: Request):
    """Page-view ping. Schedules a debounced fire (cooldown in settings)."""
    slug = _current_contact_slug(request)
    return await feed_loop.mark_visit(slug)


@app.get("/v1/feed/status")
async def feed_status(request: Request):
    """Current loop state for the UI's "generating…" indicator."""
    slug = _current_contact_slug(request)
    return feed_loop.current_status(slug)


@app.get("/v1/feed/crystal")
async def get_feed_crystal(request: Request):
    """Latest crystal:feed-orient delta for the current contact."""
    slug = _current_contact_slug(request)
    c = await feed_crystal.latest(slug, force=True)
    if not c:
        return {"crystal": None}
    return {"crystal": {
        "id": c.get("id"),
        "created_at": c.get("created_at"),
        "confidence": c.get("confidence"),
        "narrative": c.get("narrative"),
        "directive_lines": c.get("directive_lines"),
        "topic_weights": c.get("topic_weights"),
        "skip_rules": c.get("skip_rules"),
    }}


@app.post("/v1/feed/crystal/refresh")
async def refresh_feed_crystal(request: Request):
    """Manually run a feed-orient crystal regeneration (no wake-gate check)."""
    slug = _current_contact_slug(request)
    fresh = await feed_crystal.synthesize(slug)
    if not fresh:
        raise HTTPException(500, "synthesis failed — check server logs")
    return {"status": "ok", "id": fresh.get("id"), "confidence": fresh.get("confidence")}


@app.get("/v1/feed/crystal/events")
async def feed_crystal_events(request: Request, limit: int = 50):
    """Crystal regeneration history (for the ECG card)."""
    slug = _current_contact_slug(request)
    events = await feed_crystal.list_events(slug, limit=limit)
    return {"events": events}


@app.get("/v1/feed/drift")
async def feed_drift(request: Request):
    """Sample current engagement-centroid drift now."""
    slug = _current_contact_slug(request)
    return await feed_crystal.sample_drift(slug)


@app.get("/v1/feed/drift/history")
async def feed_drift_history(request: Request, since_seconds: int | None = None):
    """Drift history for the ECG card."""
    slug = _current_contact_slug(request)
    return {"history": feed_crystal.drift_history(slug, since_seconds=since_seconds)}


@app.get("/v1/feed/confidence/history")
async def feed_confidence_history(request: Request, limit: int = 50):
    """Confidence over time, derived from the confidence: tag on each crystal regen."""
    slug = _current_contact_slug(request)
    events = await feed_crystal.list_events(slug, limit=limit)
    return {"history": [
        {"t": e.get("timestamp"), "v": e.get("confidence")}
        for e in events
        if e.get("confidence") is not None
    ]}


@app.get("/v1/feed/engagement/history")
async def feed_engagement_history(
    request: Request,
    since_seconds: int = 7 * 24 * 3600,
    limit: int = 500,
):
    """Engagement marks for the ECG bottom rule. Returns time + sign per delta."""
    from datetime import datetime, timedelta, timezone
    slug = _current_contact_slug(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=since_seconds)).isoformat()
    try:
        deltas = await delta_client.query(
            tags_include=["feed-engagement", f"contact:{slug}"],
            time_start=cutoff,
            limit=limit,
        )
    except Exception:
        deltas = []
    out = []
    for d in deltas:
        kind = ""
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("engagement:"):
                kind = t.split(":", 1)[1]
                break
        if not kind:
            continue
        sign = 1 if kind in ("more", "chat") else -1 if kind == "less" else 0
        if not sign:
            continue
        out.append({"t": d.get("timestamp"), "v": sign, "k": kind})
    return {"history": out}


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
    missing: list[str] = []
    if not settings.api_key:
        missing.append("api_key")
    if not settings.resolved_base_url:
        missing.append("base_url")
    if not settings.resolved_model:
        missing.append("model")
    return {
        "status": "ok",
        "provider": settings.provider,
        "model": settings.resolved_model,
        "llm_configured": not missing,
        "llm_missing": missing,
    }


# ── Session endpoints ───────────────────────────


@app.post("/v1/sessions")
async def create_session(req: SessionCreate):
    return await db.create_session(req.title)


@app.get("/v1/sessions")
async def list_sessions(request: Request, limit: int = 50):
    # Members see only sessions they participated in. Admins see every
    # session so they can support other contacts. Auth gate upstream
    # ensures request.state.contact is always populated when auth is
    # enforced; first-run installs fall through to the default admin.
    slug = _current_contact_slug(request)
    contact = getattr(request.state, "contact", None) or {}
    filter_slug = None if contact.get("role") == "admin" else slug
    sessions = await db.list_sessions(limit, contact_slug=filter_slug)
    # Group by recency for the sidebar
    now = datetime.now(timezone.utc)
    groups: dict[str, list] = {"today": [], "yesterday": [], "last_7_days": [], "older": []}
    for s in sessions:
        raw = s["updated_at"]
        try:
            parsed = raw if hasattr(raw, "date") else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            delta_days = (now.date() - parsed.date()).days
        except (ValueError, TypeError):
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
async def get_feed_stories(request: Request, limit: int = 20, offset: int = 0):
    """Proxy to delta-store's feed stories endpoint, scoped to current contact."""
    slug = _current_contact_slug(request)
    return await delta_client.feed_stories(
        limit=limit, offset=offset, contact_slug=slug
    )


@app.post("/v1/feed/engagement")
async def write_feed_engagement(req: FeedEngagementRequest, request: Request):
    """Capture a feed engagement signal — input to the feed-orient crystal.

    Only three kinds count: `more` (the + button), `less` (the − button),
    and `chat` (a message in a chat session opened from a card). Click
    alone is not engagement; the chat session it opens is. See
    docs/feed-spec.md.
    """
    kind = (req.kind or "").lower()
    if kind not in ("more", "less", "chat"):
        raise HTTPException(400, f"unknown engagement kind: {kind!r}")
    if not req.card_id:
        raise HTTPException(400, "card_id required")

    contact = getattr(request.state, "contact", None)
    contact_slug = (contact or {}).get("slug")

    # Note on tagging: deliberately NOT using `chat:<slug>` for the chat
    # session linkage — that tag belongs to the chat-listener's session
    # roster, and an engagement delta tagged with it would be processed
    # as a user message and trip an inference turn on the JSON payload.
    # Use `chat-from:<slug>` instead — same retrieval ergonomics, no
    # collision with the listener's chat-trigger filter.
    tags = ["feed-engagement", f"engagement:{kind}", f"engages:{req.card_id}"]
    if req.topic:
        tags.append(f"topic:{req.topic}")
    if req.chat_session:
        tags.append(f"chat-from:{req.chat_session}")
    if contact_slug:
        tags.append(f"contact:{contact_slug}")

    payload = {
        "kind": kind,
        "card_id": req.card_id,
        "topic": req.topic or "",
        "card_excerpt": (req.card_excerpt or "")[:200],
    }
    if req.chat_session:
        payload["chat_session"] = req.chat_session

    written = await delta_client.write(
        content=json.dumps(payload, ensure_ascii=False),
        tags=tags,
        source="consumer-api",
    )
    return {"status": "ok", "id": written.get("id")}


_ENGAGEMENT_KINDS = ("refutes", "affirms", "reply-to")


@app.post("/v1/engagement")
async def write_engagement(req: EngagementRequest, request: Request):
    """First-class repair / affirmation channel on any delta in the lake.

    Writes a small delta whose tags include a single engagement pointer
    (`refutes:<id>`, `affirms:<id>`, or `reply-to:<id>`) plus the caller's
    contact. Content is free-text reasoning. Retrieval folds these into
    the engagement cloud on the target — refutations lower its surfacing,
    affirmations raise it.

    This is the safety net for reflexive sediment auto-writeback: a bad
    synthesis gets a `refutes:` delta pointing at it with the reasoning,
    and the next recall ranks it lower and shows the refutation inline.
    """
    kind = (req.kind or "").lower()
    if kind not in _ENGAGEMENT_KINDS:
        raise HTTPException(
            400, f"unknown engagement kind: {kind!r} (want one of {_ENGAGEMENT_KINDS})"
        )
    target_id = (req.target_id or "").strip()
    if not target_id:
        raise HTTPException(400, "target_id required")

    contact = getattr(request.state, "contact", None)
    contact_slug = (contact or {}).get("slug")

    tags = [f"{kind}:{target_id}"]
    if contact_slug:
        tags.append(f"contact:{contact_slug}")

    content = (req.reason or "").strip()
    written = await delta_client.write(
        content=content,
        tags=tags,
        source="fathom-engagement",
    )
    return {"status": "ok", "id": written.get("id")}


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


@app.get("/v1/usage/history")
async def get_usage_history(since_seconds: int = 7 * 24 * 3600, buckets: int = 60):
    """Bucketed write-count timeline (moments arriving)."""
    items = await usage_module.history(since_seconds=since_seconds, buckets=buckets)
    return {"history": items}


@app.get("/v1/recall/history")
async def get_recall_history(since_seconds: int = 7 * 24 * 3600, buckets: int = 60):
    """Bucketed recall-count timeline (moments retrieved)."""
    items = await recall.history(since_seconds=since_seconds, buckets=buckets)
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


@app.post("/v1/sources", dependencies=[Depends(auth.require_admin)])
async def create_source(req: SourceCreate):
    async with _source_runner() as c:
        r = await c.post("/api/sources", json=req.model_dump())
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.json().get("detail", r.text))
        return r.json()


@app.put("/v1/sources/{source_id}", dependencies=[Depends(auth.require_admin)])
async def update_source(source_id: str, req: SourceUpdate):
    # Include explicitly-set fields (even if None, for "forever" expiry)
    body = {k: v for k, v in req.model_dump(exclude_unset=True).items()}
    async with _source_runner() as c:
        r = await c.put(f"/api/sources/{source_id}", json=body)
        if r.status_code == 404:
            raise HTTPException(404, f"Source not found: {source_id}")
        r.raise_for_status()
        return r.json()


@app.post("/v1/sources/{source_id}/pause", dependencies=[Depends(auth.require_admin)])
async def pause_source(source_id: str):
    async with _source_runner() as c:
        r = await c.post(f"/api/sources/{source_id}/pause")
        r.raise_for_status()
        return r.json()


@app.post("/v1/sources/{source_id}/resume", dependencies=[Depends(auth.require_admin)])
async def resume_source(source_id: str):
    async with _source_runner() as c:
        r = await c.post(f"/api/sources/{source_id}/resume")
        r.raise_for_status()
        return r.json()


@app.post("/v1/sources/{source_id}/poll", dependencies=[Depends(auth.require_admin)])
async def poll_source(source_id: str):
    async with _source_runner() as c:
        r = await c.post(f"/api/sources/{source_id}/poll")
        if r.status_code == 404:
            raise HTTPException(404, f"Source not found: {source_id}")
        r.raise_for_status()
        return r.json()


@app.delete("/v1/sources/{source_id}", dependencies=[Depends(auth.require_admin)])
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


# ── Routines ────────────────────────────────────
#
# Routines are spec deltas in the lake with YAML frontmatter + prompt body.
# Tagged `[spec, routine, routine-id:<id>]`. CRUD operations here write new
# spec deltas with the same routine-id; scheduler + dashboard take latest.
# See docs/routine-spec.md for the canonical field reference.


@app.get("/v1/routines")
async def list_routines_endpoint():
    return {"routines": await routines_mod.list_routines()}


@app.post("/v1/routines", dependencies=[Depends(auth.require_admin)])
async def create_routine_endpoint(body: dict):
    try:
        return await routines_mod.create(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@app.put("/v1/routines/{routine_id}", dependencies=[Depends(auth.require_admin)])
async def update_routine_endpoint(routine_id: str, body: dict):
    try:
        return await routines_mod.update(routine_id, body)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.delete("/v1/routines/{routine_id}", dependencies=[Depends(auth.require_admin)])
async def delete_routine_endpoint(routine_id: str):
    try:
        return await routines_mod.soft_delete(routine_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/v1/routines/{routine_id}/fire", dependencies=[Depends(auth.require_admin)])
async def fire_routine_endpoint(routine_id: str, body: dict | None = None):
    override = (body or {}).get("prompt") if body else None
    try:
        return await routines_mod.fire(routine_id, prompt_override=override)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/v1/routines/preview-schedule")
async def preview_schedule_endpoint(body: dict):
    schedule = (body.get("schedule") or "").strip()
    count = int(body.get("count") or 5)
    if not schedule:
        return {"fires": [], "error": "schedule required"}
    fires = routines_mod.preview_fires(schedule, count=count)
    if not fires:
        return {"fires": [], "error": "invalid cron"}
    return {"fires": fires}


# ── Agents ──────────────────────────────────────
#
# Agent presence is surfaced via `agent-heartbeat` deltas. An agent
# (fathom-agent + its plugins) writes one every ~60s with a 24h expires_at.
# The long TTL keeps the heartbeat visible after an agent goes quiet so
# the dashboard can render a "disconnected" card; whether the agent is
# currently connected is computed from the heartbeat's timestamp (see
# HEARTBEAT_STALE_SECONDS in tools.py).


# ── Agent release info ─────────────────────────────────────────────────────
# Cache the npm registry's "latest" tag for fathom-agent in process memory.
# Every browser refresh would otherwise hit npm, leaking this install's IP
# and wasting their bandwidth. One hour is plenty — agent releases are slow.

_LATEST_AGENT_CACHE: dict = {"version": None, "checked_at": None, "error": None}
_LATEST_AGENT_TTL_SECONDS = 3600


@app.get("/v1/agents/latest-version")
async def agents_latest_version():
    """Return the newest published fathom-agent version from the npm registry."""
    import time as _time
    now = _time.time()
    checked = _LATEST_AGENT_CACHE.get("checked_at")
    if checked and (now - checked) < _LATEST_AGENT_TTL_SECONDS and _LATEST_AGENT_CACHE.get("version"):
        return {
            "latest": _LATEST_AGENT_CACHE["version"],
            "checked_at": datetime.fromtimestamp(checked, timezone.utc).isoformat(),
            "cached": True,
        }
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get("https://registry.npmjs.org/fathom-agent/latest")
            r.raise_for_status()
            data = r.json()
        version = data.get("version")
        _LATEST_AGENT_CACHE.update({"version": version, "checked_at": now, "error": None})
        return {
            "latest": version,
            "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "cached": False,
        }
    except Exception as e:
        _LATEST_AGENT_CACHE.update({"checked_at": now, "error": str(e)})
        return {
            "latest": _LATEST_AGENT_CACHE.get("version"),  # last-known may still be useful
            "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "error": "registry_unreachable",
        }


@app.get("/v1/agents/status")
async def agents_status():
    """Return the most recent heartbeat per host with a connected/disconnected classification.

    Dashboard shows a card for every known host. Fresh heartbeats render as
    connected (glowing robot); stale ones render as disconnected (faded).
    Plugin state travels with the heartbeat so the UI can show badges like
    "routine's permission_mode is not allowed here" without a round trip.
    """
    # Heartbeat deltas linger for 24h. Pull enough to cover realistic fleet
    # sizes — limit=100 easily handles dozens of hosts even with bursty
    # timing where several hosts emit within the same second.
    try:
        deltas = await delta_client.query(limit=100, tags_include=["agent-heartbeat"])
    except Exception as e:
        return {"agents": [], "error": str(e)}

    by_host: dict[str, dict] = {}
    for d in deltas:
        tags = d.get("tags") or []
        host = next((t.split(":", 1)[1] for t in tags if t.startswith("host:")), "unknown")
        ts = d.get("timestamp", "")
        prev = by_host.get(host)
        if prev is None or ts > prev.get("timestamp", ""):
            try:
                payload = json.loads(d.get("content", "{}"))
            except Exception:
                payload = {}
            # Prefer `agent_version` (new, explicit). Fall back to `version`
            # for older agents that only sent the combined field — old agents
            # pinned `version` to the heartbeat schema version (0.10.0), which
            # is what caused the dashboard to show a spurious update chip.
            agent_version = payload.get("agent_version") or payload.get("version")
            age = heartbeat_age_seconds(d)
            by_host[host] = {
                "host": host,
                "timestamp": ts,
                "delta_id": d.get("id"),
                "expires_at": d.get("expires_at"),
                "version": agent_version,
                "schema_version": payload.get("schema_version"),
                "plugins": payload.get("plugins") or {},
                "uptime_s": payload.get("uptime_s"),
                # Local management URL advertised by the agent's local-ui
                # plugin. Only resolvable from the machine itself; dashboard
                # uses it to deep-link the "configure ↗" chip per agent block.
                "agent_url": payload.get("agent_url"),
                # Rotates per agent-process-start. The dashboard's probe
                # compares this to what /api/identity returns, so two
                # agents advertising the same agent_url can still be told
                # apart — the one whose nonce matches is the real target.
                "identity_nonce": payload.get("identity_nonce"),
                "status": "connected" if heartbeat_is_fresh(d) else "disconnected",
                "heartbeat_age_seconds": int(age) if age is not None else None,
            }

    agents = list(by_host.values())
    return {
        "agents": agents,
        # `alive` stays "any known host" so the dashboard shows cards (even
        # disconnected ones) instead of the install view once a host has
        # been seen. Use `connected_count` for "how many are actually up".
        "alive": len(agents) > 0,
        "connected_count": sum(1 for a in agents if a["status"] == "connected"),
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
    request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(""),
    content: str = Form(""),
    expires_at: str = Form(""),
    tags: str = Form(""),
    source: str = Form(""),
):
    """Upload an image as a lake delta. Returns {id, media_hash}.

    Defaults to chat framing (tags: user,participant:user,image; source:
    fathom-chat) for backwards compatibility with the chat UI. Non-chat
    callers (browser extensions, screen capture, imports) pass their own
    comma-separated ``tags`` and ``source`` to override — when ``tags``
    is set, the chat defaults are skipped entirely. ``session_id`` still
    appends ``chat:<slug>`` regardless, so a browse capture can also land
    in a chat session if you want Fathom to see it there.

    ``expires_at`` (optional ISO timestamp) makes the delta short-lived;
    the reaper deletes on/after that time. Caller computes the absolute
    timestamp themselves, matching the heartbeat / sysinfo / chat-event
    pattern used elsewhere.
    """
    file_bytes = await file.read()

    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        tag_list = [db.LAKE_CHAT_TAG, "user", "participant:user", "image"]

    if session_id:
        tag_list.append(f"chat:{session_id}")

    contact = getattr(request.state, "contact", None)
    contact_slug = (contact or {}).get("slug")
    if contact_slug and not any(t.startswith("contact:") for t in tag_list):
        tag_list.append(f"contact:{contact_slug}")

    result = await delta_client.upload_media(
        file_bytes=file_bytes,
        filename=file.filename or "upload.jpg",
        content=content,
        tags=tag_list,
        source=source or db.LAKE_CHAT_SOURCE,
        expires_at=expires_at or None,
    )
    return result


class CaptureContext(BaseModel):
    media_hash: str
    content: str = ""
    tags: list[str] = []
    source: str = "browser-capture"
    expires_at: str | None = None


@app.post("/v1/media/capture-context")
async def capture_context(req: CaptureContext):
    """Write a context delta for a browser-captured image.

    The image is already in delta-store (uploaded via /v1/media/upload).
    This writes a companion text delta linking the media_hash to the
    story content so the lake knows what the image means.

    ``expires_at`` (optional ISO timestamp) makes the context delta
    short-lived alongside the image — both should expire together so
    browse captures don't leave dangling context in the lake.
    """
    c = await delta_client._get()
    body: dict = {
        "content": req.content or f"[captured image:{req.media_hash}]",
        "tags": req.tags or ["browser-capture"],
        "source": req.source,
        "media_hash": req.media_hash,
        "modality": "image",
    }
    if req.expires_at:
        body["expires_at"] = req.expires_at
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


# ── Auth identity ─────────────────────────────


# ── Bootstrap (first-run onboarding) ─────────────
#
# A fresh install has no admin. The dashboard gates on
# bootstrap-status and redirects to /ui/onboarding.html when needed.
# The POST endpoint is one-shot — it creates the first admin contact,
# writes the admin profile delta, optionally adds an email handle, and
# mints a full-scope admin token. Subsequent POSTs fail 409.


class BootstrapBody(BaseModel):
    display_name: str
    slug: str | None = None
    profile: dict | None = None
    email: str | None = None


def _slugify(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "admin"


@app.get("/v1/auth/bootstrap-status")
async def bootstrap_status():
    """Return whether this instance still needs first-run onboarding.

    True when no active admin contact exists in the registry. The UI
    uses this to decide between onboarding.html and the dashboard on
    first boot."""
    slug = await contacts_mod.first_admin_slug()
    return {"needs_bootstrap": slug is None}


@app.post("/v1/auth/bootstrap", status_code=201)
async def bootstrap(body: BootstrapBody):
    """Create the first admin contact and mint its admin token. One-shot."""
    existing = await contacts_mod.first_admin_slug()
    if existing:
        raise HTTPException(409, f"Already bootstrapped (admin: {existing})")

    display_name = (body.display_name or "").strip()
    if not display_name:
        raise HTTPException(400, "display_name is required")

    slug = _slugify(body.slug or display_name)

    # Collision check (include disabled, since re-using a tombstoned slug
    # would silently merge into a prior contact's delta stream).
    existing_row = await delta_client.get_contact_row(slug, include_disabled=True)
    if existing_row:
        raise HTTPException(409, f"Slug '{slug}' already exists")

    initial_profile: dict = {"role": "admin", "display_name": display_name}
    if body.profile:
        for key in ("pronouns", "timezone", "language", "bio", "aliases", "avatar"):
            v = body.profile.get(key)
            if v is not None:
                initial_profile[key] = v

    contact = await contacts_mod.create(
        slug, initial_profile=initial_profile, actor_slug=None
    )

    if body.email:
        email = body.email.strip()
        if email:
            try:
                await delta_client.add_handle(slug, "email", email)
            except Exception:
                log.exception("bootstrap: add_handle(email) failed for %s", slug)

    token_result = auth.create_token(
        name="Admin (bootstrap)",
        scopes=list(auth.ALL_SCOPES.keys()),
        contact_slug=slug,
    )

    # Prime the first-admin cache so subsequent unauthed reads see the
    # new admin without waiting on cache expiry / module reload.
    contacts_mod.invalidate_first_admin_cache()
    auth.invalidate_contact_cache(slug)

    # Re-fetch so handles (and any other derived fields) land on the
    # returned contact.
    hydrated = await contacts_mod.get(slug) or contact

    return {"token": token_result["token"], "contact": hydrated}


@app.get("/v1/auth/me")
async def auth_me(request: Request):
    """Return the current caller's contact + token shape.

    Used by the dashboard shell to know who's logged in and which role
    gates to apply. Never returns the raw token — that's one-time at
    mint.

    `auth_required` reports whether the server currently enforces auth
    (i.e. at least one token has been minted). The login page uses it
    to distinguish "first-run, sign-in optional" from "server is
    locked, redirect to login."
    """
    contact = getattr(request.state, "contact", None)
    token = getattr(request.state, "token", None)
    return {
        "authenticated": (contact is not None) and (token is not None or not auth.auth_required()),
        "auth_required": auth.auth_required(),
        "contact": contact,
        "token": {
            "id": (token or {}).get("id"),
            "name": (token or {}).get("name"),
            "scopes": (token or {}).get("scopes"),
        } if token else None,
    }


# ── Token management ─────────────────────────────


class TokenCreate(BaseModel):
    name: str = ""
    scopes: list[str] | None = None
    contact_slug: str | None = None


@app.post("/v1/tokens", dependencies=[Depends(auth.require_admin)])
async def create_token(req: TokenCreate, request: Request):
    # Default to the caller's own contact. Admins can mint for others by
    # passing contact_slug explicitly. require_admin guarantees a caller
    # contact, so the fallback is purely defensive.
    caller = getattr(request.state, "contact", None)
    default_slug = (caller or {}).get("slug", "")
    slug = req.contact_slug or default_slug
    if not slug:
        raise HTTPException(400, "contact_slug required")
    return auth.create_token(req.name, req.scopes, contact_slug=slug)


@app.get("/v1/scopes")
async def list_scopes():
    return auth.get_scopes()


@app.get("/v1/tokens", dependencies=[Depends(auth.require_admin)])
async def list_tokens():
    return auth.list_tokens()


@app.delete("/v1/tokens/{token_id}", dependencies=[Depends(auth.require_admin)])
async def delete_token(token_id: str):
    deleted = auth.delete_token(token_id)
    if not deleted:
        raise HTTPException(404, "Token not found")
    return {"deleted": True}


# ── Pair-code onboarding ─────────────────────────
#
# POST /v1/pair         → mint a short-lived single-use admission code
# GET  /v1/pair         → list currently-active (unredeemed, unexpired) codes
# POST /v1/pair/redeem  → exchange a code for a real API token (public)
#
# See api/pairing.py for the flow and rationale. The redeem endpoint is in
# PUBLIC_PATHS because the agent has no token yet when it calls it.


class PairCreate(BaseModel):
    note: str = ""
    ttl_seconds: int = 600
    contact_slug: str | None = None


@app.post("/v1/pair", dependencies=[Depends(auth.require_admin)])
async def pair_create(body: PairCreate, request: Request):
    caller = getattr(request.state, "contact", None)
    default_slug = (caller or {}).get("slug", "")
    slug = body.contact_slug or default_slug
    if not slug:
        raise HTTPException(400, "contact_slug required")
    return pairing.create_pair_code(
        ttl_seconds=body.ttl_seconds,
        note=body.note,
        contact_slug=slug,
    )


@app.get("/v1/pair", dependencies=[Depends(auth.require_admin)])
async def pair_list():
    return {"codes": pairing.list_active_codes()}


# ── Contacts (registry admin) ───────────────────
#
# Thin HTTP layer over api/contacts.py. That module owns the merge
# between the delta-store registry row and the latest profile delta,
# and is the only writer of profile deltas. Per docs/contact-spec.md.


PROFILE_SOFT_FIELDS = {
    "display_name", "pronouns", "timezone", "language", "bio", "avatar", "aliases",
}


class ContactCreate(BaseModel):
    slug: str
    display_name: str | None = None
    role: str = "member"
    pronouns: str | None = None
    timezone: str | None = None
    language: str | None = None
    bio: str | None = None
    avatar: str | None = None
    aliases: list[str] | None = None


class ContactUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = None
    pronouns: str | None = None
    timezone: str | None = None
    language: str | None = None
    bio: str | None = None
    avatar: str | None = None
    aliases: list[str] | None = None


class SelfProfileUpdate(BaseModel):
    """Soft fields a contact can edit about themselves. Deliberately
    does NOT include `role` — FastAPI would drop unknown fields from the
    body, and even if the client tries to include role it never reaches
    the update call."""
    display_name: str | None = None
    pronouns: str | None = None
    timezone: str | None = None
    language: str | None = None
    bio: str | None = None
    avatar: str | None = None
    aliases: list[str] | None = None


class HandleBody(BaseModel):
    channel: str
    identifier: str


class ProposeContactIn(BaseModel):
    candidate_slug: str | None = None
    display_name: str
    rationale: str
    source_context: dict | None = None


class AcceptProposalIn(BaseModel):
    slug: str
    display_name: str
    role: str = "member"
    pronouns: str | None = None
    timezone: str | None = None
    language: str | None = None
    bio: str | None = None
    aliases: list[str] | None = None


class RejectProposalIn(BaseModel):
    note: str = ""


def _caller_slug(request: Request) -> str | None:
    contact = getattr(request.state, "contact", None)
    return (contact or {}).get("slug")


@app.get("/v1/contacts", dependencies=[Depends(auth.require_admin)])
async def list_contacts(include_disabled: bool = False):
    return await contacts_mod.list_all(include_disabled=include_disabled)


@app.post("/v1/contacts", dependencies=[Depends(auth.require_admin)])
async def create_contact(req: ContactCreate, request: Request):
    actor = _caller_slug(request)
    payload = req.model_dump(exclude_unset=True)
    slug = payload.pop("slug")
    try:
        created = await contacts_mod.create(
            slug=slug, initial_profile=payload, actor_slug=actor
        )
    except httpx.HTTPStatusError as e:
        detail = "Contact already exists" if e.response.status_code == 409 else str(e)
        raise HTTPException(status_code=e.response.status_code, detail=detail) from e
    auth.invalidate_contact_cache(slug)
    return created


@app.get("/v1/contacts/{slug}", dependencies=[Depends(auth.require_admin)])
async def get_contact(slug: str):
    contact = await contacts_mod.get(slug)
    if not contact:
        raise HTTPException(404, "Contact not found")
    return contact


@app.patch("/v1/contacts/{slug}", dependencies=[Depends(auth.require_admin)])
async def update_contact(slug: str, req: ContactUpdate, request: Request):
    actor = _caller_slug(request)
    fields = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    if not fields:
        existing = await contacts_mod.get(slug)
        if not existing:
            raise HTTPException(404, "Contact not found")
        return existing
    try:
        updated = await contacts_mod.update_profile(
            slug, fields, actor_slug=actor, event="updated"
        )
    except contacts_mod.LastAdminError as e:
        raise HTTPException(409, detail=str(e)) from e
    if not updated:
        raise HTTPException(404, "Contact not found")
    auth.invalidate_contact_cache(slug)
    return updated


@app.delete("/v1/contacts/{slug}", dependencies=[Depends(auth.require_admin)])
async def delete_contact(slug: str, request: Request):
    actor = _caller_slug(request)
    try:
        ok = await contacts_mod.disable(slug, actor_slug=actor)
    except contacts_mod.LastAdminError as e:
        raise HTTPException(409, detail=str(e)) from e
    if not ok:
        raise HTTPException(404, "Contact not found")
    auth.invalidate_contact_cache(slug)
    return {"disabled": slug}


@app.get("/v1/contacts/{slug}/handles", dependencies=[Depends(auth.require_admin)])
async def list_contact_handles(slug: str):
    c = await delta_client._get()
    r = await c.get(f"/contacts/{slug}/handles")
    if r.status_code == 404:
        raise HTTPException(404, "Contact not found")
    r.raise_for_status()
    return r.json()


@app.post("/v1/contacts/{slug}/handles", dependencies=[Depends(auth.require_admin)])
async def add_contact_handle(slug: str, body: HandleBody):
    try:
        handle = await delta_client.add_handle(slug, body.channel, body.identifier)
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=e.response.json().get("detail", str(e)),
        ) from e
    auth.invalidate_contact_cache(slug)
    return handle


@app.delete("/v1/contacts/{slug}/handles", dependencies=[Depends(auth.require_admin)])
async def remove_contact_handle(slug: str, body: HandleBody):
    try:
        await delta_client.remove_handle(slug, body.channel, body.identifier)
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=e.response.json().get("detail", str(e)),
        ) from e
    auth.invalidate_contact_cache(slug)
    return {"deleted": {"channel": body.channel, "identifier": body.identifier}}


# ── Contact proposals (propose-then-confirm) ──────
#
# Any authenticated caller can propose — Fathom in its chat loop,
# a plugin, a bridge, a teammate. Admin accepts or rejects. The
# proposal is sediment either way.


@app.get("/v1/contact-proposals")
async def list_contact_proposals(limit: int = 50):
    """Open (unresolved) proposals — visible to any authed caller so
    Fathom can avoid proposing the same person twice."""
    return await contacts_mod.list_proposals(limit=limit)


@app.post("/v1/contact-proposals")
async def propose_contact(body: ProposeContactIn, request: Request):
    """Low-privilege propose endpoint. The proposer's contact slug is
    stamped on the delta so admins can see who noticed."""
    proposer = _caller_slug(request)
    return await contacts_mod.propose(
        candidate_slug=body.candidate_slug,
        display_name=body.display_name,
        rationale=body.rationale,
        source_context=body.source_context,
        proposer_slug=proposer,
    )


@app.post(
    "/v1/contact-proposals/{proposal_id}/accept",
    dependencies=[Depends(auth.require_admin)],
)
async def accept_contact_proposal(
    proposal_id: str, body: AcceptProposalIn, request: Request
):
    actor = _caller_slug(request)
    extras = {
        k: v for k, v in body.model_dump(exclude_unset=True).items()
        if v is not None and k not in ("slug", "display_name", "role")
    }
    try:
        created = await contacts_mod.accept_proposal(
            proposal_id=proposal_id,
            slug=body.slug,
            display_name=body.display_name,
            role=body.role,
            extra_fields=extras,
            actor_slug=actor,
        )
    except httpx.HTTPStatusError as e:
        detail = "Slug already exists" if e.response.status_code == 409 else str(e)
        raise HTTPException(status_code=e.response.status_code, detail=detail) from e
    auth.invalidate_contact_cache(body.slug)
    return created


@app.post(
    "/v1/contact-proposals/{proposal_id}/reject",
    dependencies=[Depends(auth.require_admin)],
)
async def reject_contact_proposal(
    proposal_id: str, body: RejectProposalIn, request: Request
):
    await contacts_mod.reject_proposal(
        proposal_id,
        actor_slug=_caller_slug(request),
        note=body.note,
    )
    return {"rejected": proposal_id}


# ── Self profile (named endpoint for self-edits) ───


@app.get("/v1/me/profile")
async def get_my_profile(request: Request):
    contact = getattr(request.state, "contact", None)
    if not contact:
        raise HTTPException(401, "Authentication required")
    slug = contact.get("slug")
    fresh = await contacts_mod.get(slug)
    if not fresh:
        raise HTTPException(404, "Profile not found")
    return fresh


@app.patch("/v1/me/profile")
async def update_my_profile(req: SelfProfileUpdate, request: Request):
    """Self-edit for soft profile fields. Role stays admin-only by
    virtue of not being on the Pydantic model."""
    contact = getattr(request.state, "contact", None)
    if not contact:
        raise HTTPException(401, "Authentication required")
    slug = contact.get("slug")

    fields = {
        k: v
        for k, v in req.model_dump(exclude_unset=True).items()
        if v is not None
    }
    if not fields:
        existing = await contacts_mod.get(slug)
        if not existing:
            raise HTTPException(404, "Profile not found")
        return existing

    updated = await contacts_mod.update_profile(
        slug, fields, actor_slug=slug, event="self-edited"
    )
    if not updated:
        raise HTTPException(404, "Profile not found")
    auth.invalidate_contact_cache(slug)
    return updated


class PairRedeem(BaseModel):
    code: str
    host: str = ""


@app.post("/v1/pair/redeem")
async def pair_redeem(body: PairRedeem):
    try:
        return pairing.redeem_pair_code(body.code, body.host)
    except ValueError as e:
        reason = str(e)
        # Map reason → HTTP status so the agent can show a useful message
        status = {
            "unknown_code": 404,
            "already_redeemed": 410,
            "expired": 410,
        }.get(reason, 400)
        raise HTTPException(status, detail=reason) from e


# ── Tool definitions (served to all clients) ─────

LAKE_TOOLS = [
    {
        "name": "remember",
        "description": (
            "Search your memories with a natural language query. Returns a "
            "trail of moments — conversations, notes, research, "
            "photos, sensor data — as an associative chain (first came to mind, "
            "which reminded you of...). Be descriptive: 'Nova mozzarella stretch "
            "kitchen photo' works better than 'nova'. depth='deep' (default) "
            "traces connections; 'shallow' is a single quick search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you're trying to remember."},
                "depth": {
                    "type": "string",
                    "enum": ["deep", "shallow"],
                    "description": "deep = trace connections; shallow = single search.",
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
        "name": "write",
        "description": (
            "Persist a thought, observation, or discovery. Everything you write "
            "becomes part of you — a future self will find it when they need it. "
            "One idea per write."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "What to persist."},
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Tags for filtering (e.g. ['meeting', 'decision']).",
                },
                "source": {"type": "string", "description": "Source label.", "default": "api"},
                "image_b64": {
                    "type": "string",
                    "description": "Optional base64-encoded image bytes. Creates an image-modality delta with `content` as the caption.",
                },
                "image_path": {
                    "type": "string",
                    "description": "Optional absolute path to an image file readable by the api server. Alternative to image_b64.",
                },
            },
            "required": ["content"],
        },
        "endpoint": {"method": "POST", "path": "/v1/deltas"},
        "scope": "lake:write",
    },
    {
        "name": "recall",
        "description": (
            "Examine your memories by tags, source, or time. "
            "For structured retrieval when you know what you're looking for."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Memories must have ALL these tags.",
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
        "name": "mind_stats",
        "description": (
            "Check the state of your memory — total moments, coverage, top tags. "
            "Quick self-check."
        ),
        "parameters": {"type": "object", "properties": {}},
        "endpoint": {"method": "GET", "path": "/v1/stats"},
        "scope": "lake:read",
    },
    {
        "name": "propose_contact",
        "description": (
            "Propose a new contact for admin review. Use when you "
            "encounter a person the lake doesn't know about yet — a "
            "mention in chat, an unknown handle, a correspondent "
            "you've gathered enough evidence on to formally register. "
            "Writes a contact-proposal delta; an admin accepts or "
            "rejects. You never create contacts yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "display_name": {
                    "type": "string",
                    "description": "How people refer to this person (required).",
                },
                "candidate_slug": {
                    "type": "string",
                    "description": (
                        "Suggested URL-safe identifier ('nova', 'bob'). "
                        "Lowercase, no spaces. Admin can override on accept."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "1-3 sentences — who they seem to be, the evidence, "
                        "why they should be a contact."
                    ),
                },
                "source_context": {
                    "type": "object",
                    "description": (
                        "Optional hints — {chat_session, channel, handle, "
                        "delta_ids, …} — whatever helps the admin verify."
                    ),
                },
            },
            "required": ["display_name", "rationale"],
        },
        "endpoint": {"method": "POST", "path": "/v1/contact-proposals"},
        "scope": "lake:write",
    },
    {
        "name": "engage",
        "description": (
            "React to a delta in the lake — refute a synthesis that's "
            "wrong, affirm a memory that keeps proving useful, or reply "
            "to something you're responding to. Your engagement becomes "
            "its own delta and shapes how the target surfaces in future "
            "recalls: refutes lower its rank and travel inline as "
            "reasoning the next recall sees; affirms raise it. Use this "
            "as the repair channel for bad sediment."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "id of the delta you're engaging with.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["refutes", "affirms", "reply-to"],
                    "description": (
                        "refutes: disagree, lowers surfacing. "
                        "affirms: useful/right, raises surfacing. "
                        "reply-to: neutral conversational pointer."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Your reasoning in prose. For refutes this is "
                        "what future recalls see under the delta."
                    ),
                },
            },
            "required": ["target_id", "kind"],
        },
        "endpoint": {"method": "POST", "path": "/v1/engagement"},
        "scope": "lake:write",
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
async def proxy_write_delta(body: dict, request: Request):
    """Raw lake write. Single external path that accepts caller-supplied
    tag lists, so it carries both defenses:

    1. Strip-and-re-stamp `contact:*`. Caller cannot address a delta to
       anyone but themselves.
    2. Reserved-tag scan. Authority-bearing tags must pass their gate
       (see docs/reserved-tags-spec.md + api/reserved_tags.py).

    If the body carries `image_b64` or `image_path`, the delta is written
    as an image-modality delta via `delta_client.upload_media` — content
    becomes the caption. Same tag gates apply.
    """
    contact = getattr(request.state, "contact", None)
    caller_slug = (contact or {}).get("slug")

    # (1) Strip caller-supplied contact:* tags; re-stamp with the
    # authenticated caller's slug if we have one. Internal-tag callers
    # never use this endpoint, so unknown-caller writes don't carry a
    # contact tag at all — that's fine; the reservation scan will catch
    # any authority-bearing write that came in unauthenticated.
    caller_tags = reserved_tags.strip_contact_tags(list(body.get("tags") or []))
    if caller_slug:
        caller_tags.append(f"contact:{caller_slug}")
    body = {**body, "tags": caller_tags}

    # (2) Reservation gate.
    result = await reserved_tags.evaluate(caller_tags, contact)
    if not result.ok:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "reserved_tag",
                "tag": result.tag,
                "gate": result.gate,
                "detail": result.hint or "",
            },
        )

    # (3) Image branch — if the caller attached an image, hand off to
    # upload_media which writes a multimodal delta (modality=image, with
    # the image stored on disk and content kept as the caption).
    image_path = body.get("image_path")
    image_b64 = body.get("image_b64")
    if image_path or image_b64:
        if image_path:
            try:
                file_bytes = await asyncio.to_thread(Path(image_path).read_bytes)
            except (FileNotFoundError, PermissionError, OSError) as e:
                raise HTTPException(status_code=400, detail=f"image_path unreadable: {e}")
            filename = Path(image_path).name or "upload.bin"
        else:
            try:
                file_bytes = base64.b64decode(image_b64, validate=True)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"image_b64 decode failed: {e}")
            filename = "upload.bin"
        return await delta_client.upload_media(
            file_bytes=file_bytes,
            filename=filename,
            content=body.get("content", "") or "",
            tags=caller_tags,
            source=body.get("source") or "api",
            expires_at=body.get("expires_at"),
        )

    c = await delta_client._get()
    r = await c.post("/deltas", json=body)
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
