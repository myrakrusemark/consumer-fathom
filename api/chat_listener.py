"""Chat listener — Fathom's lake-driven response loop.

Fathom's identity is "a distributed system that thinks, remembers, reflects,
acts, and speaks." Until now, Fathom only spoke in response to an HTTP
request. The agent side already lived in the lake — polling for deltas
addressed to it and reacting. This module is the symmetric half:
Fathom-the-mind also lives in the lake now, listening for any new delta
in any chat session, and taking a turn per delta.

The trigger layer is uniform: every new chat delta is a potential turn.
The response layer is where choice lives — Fathom can speak, or answer
with `<...>` to stay silent. Silence is the default; speaking is a choice.

One process, one listener. No distributed locks, no dedup protocol. If
Fathom ever fan-outs (Accelerando-style), that's a conscious merge point,
not a race condition.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from . import db, delta_client

# The uvicorn default config doesn't raise app loggers above WARNING, so
# INFO lines from this module would be swallowed. We want the operational
# trail visible in `podman logs` for debugging turns, so pin this logger
# to INFO explicitly.
logging.getLogger(__name__).setLevel(logging.INFO)

# Short-term history window — last N messages fed back in as context.
# Mirrors the constant in server.py's chat_completions; keeping them in
# sync by convention since this listener is Fathom's other entry point.
SHORTTERM_TURNS = 6

log = logging.getLogger(__name__)

# How often the listener wakes up to check the lake. Short enough that
# conversation feels live; long enough to avoid spinning on an empty lake.
POLL_INTERVAL_SECONDS = 3

# How long ephemeral chat-event deltas (tool uses, silent acks, image
# views) stick around in the lake before the delta-store reaps them.
# Long enough that a user who just switched tabs and comes back sees the
# trail; short enough that they don't accumulate and clutter queries.
EVENT_TTL_SECONDS = 300

# Sources whose deltas should NOT trigger a Fathom turn:
#   - fathom-chat: Fathom's own chat writes. Would loop forever.
#   - fathom-mood, fathom-feed, consumer-api:route: other consumer-api
#     writes. These are side effects, not conversation.
# Anything else landing with a `chat:<slug>` tag is treated as a
# participant's message and fires a turn.
IGNORED_SOURCES = {
    "fathom-chat",
    "fathom-chat-event",  # ephemeral tool/silence events Fathom wrote
    "fathom-mood",
    "fathom-feed",
    "consumer-api:route",  # Fathom's own route_to_agent writes
}


class ChatListener:
    """Polls the lake for new chat deltas and fires inference turns.

    Holds a single `last_seen` timestamp across all sessions. Starts at
    process boot time so a restart doesn't retrigger historical messages.
    Each tick: query deltas with chat:* tags newer than last_seen, group
    by session, fire one turn per session (not one per delta — many deltas
    in a short window should produce one response, not many).
    """

    def __init__(self) -> None:
        # Start from "now" so a restart doesn't fire on historical deltas.
        # Future work: persist to disk so a crash mid-response doesn't
        # drop the trigger — for now, losing a turn on crash is acceptable.
        self._last_seen = datetime.now(timezone.utc).isoformat()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Per-session locks so concurrent deltas in the same session are
        # processed serially — avoids overlapping inference for one chat.
        # Separate sessions can still run concurrently.
        self._session_locks: dict[str, asyncio.Lock] = {}

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        print(f"chat-listener: started (polling every {POLL_INTERVAL_SECONDS}s)", flush=True)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        print("chat-listener: stopped", flush=True)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                log.exception("chat-listener: tick error: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        # Pull every delta newer than last_seen. The delta-store supports
        # tag filters but we want any chat:* tag; the cheap approach is to
        # query by timestamp and filter in-process. Volume here is low.
        try:
            fresh = await delta_client.query(
                limit=200,
                time_start=self._last_seen,
            )
        except Exception as e:
            log.warning("chat-listener: query failed: %s", e)
            return

        if not fresh:
            return

        # Group deltas by session so one session's flurry of messages turns
        # into one response, not N. Grouping uses the chat:<slug> tag —
        # deltas without it aren't chat and are ignored entirely.
        by_session: dict[str, list[dict]] = {}
        max_ts = self._last_seen
        for d in fresh:
            ts = d.get("timestamp") or ""
            if ts > max_ts:
                max_ts = ts
            if ts <= self._last_seen:
                continue  # belt-and-suspenders — should be filtered by time_start
            # Skip our own conversation writes. Without this we'd loop.
            if d.get("source") in IGNORED_SOURCES:
                continue
            session_slug = _chat_slug(d.get("tags") or [])
            if not session_slug:
                continue
            # Skip deltas that are Fathom's own chat turns (defensive —
            # IGNORED_SOURCES catches them by source, but a manually written
            # delta tagged participant:fathom should also be skipped).
            if "participant:fathom" in (d.get("tags") or []):
                continue
            by_session.setdefault(session_slug, []).append(d)

        self._last_seen = max_ts

        if not by_session:
            return

        # Fire turns in parallel across sessions, serial within.
        await asyncio.gather(
            *(self._process_session(slug, deltas) for slug, deltas in by_session.items()),
            return_exceptions=True,
        )

    async def _process_session(self, slug: str, new_deltas: list[dict]) -> None:
        # One lock per session so overlapping ticks don't race on the same
        # conversation. The lock is cheap; we create them lazily.
        lock = self._session_locks.setdefault(slug, asyncio.Lock())
        async with lock:
            try:
                await self._take_turn(slug, new_deltas)
            except Exception as e:
                log.exception("chat-listener: session %s turn failed: %s", slug, e)

    async def _take_turn(self, slug: str, new_deltas: list[dict]) -> None:
        # Import here to avoid a circular import — server.py imports
        # from this module too (to start/stop the listener in lifespan).
        from .server import fathom_think

        # Latest message content from the new batch becomes the "user
        # message" sent to the LLM. Full session history gives context.
        new_deltas_sorted = sorted(new_deltas, key=lambda d: d.get("timestamp") or "")
        latest = new_deltas_sorted[-1]
        latest_content = (latest.get("content") or "").strip()
        if not latest_content:
            return

        print(
            f"chat-listener: turn in {slug} ({len(new_deltas_sorted)} new deltas, "
            f"trigger source={latest.get('source')})",
            flush=True,
        )

        # Tool events (remember / recall / image_view / etc.) are
        # surfaced as short-lived deltas tagged with this session so the
        # UI's existing poll picks them up — same visual trail users had
        # when tool use streamed over SSE, now just routed through the
        # lake with a TTL. Deltas reap automatically after EVENT_TTL.
        # _resolve_tools calls this synchronously, so we fire-and-forget
        # the write as a background task to avoid blocking the tool loop
        # on a lake write — the write is best-effort UI signal, not a
        # correctness dependency.
        def on_tool_event(kind: str, name: str, data: dict) -> None:
            if kind != "result":
                return
            asyncio.create_task(write_chat_event(slug, name, data))

        history_msgs = await db.get_messages(slug)
        # Map the session history into OpenAI-ish {role, content} pairs.
        # Agent messages come back with role='agent' — present them to the
        # LLM as assistant-side context (they're Fathom's body's speech,
        # which Fathom should read as its own prior turns).
        history: list[dict] = []
        for m in history_msgs:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "agent":
                host = m.get("host") or "body"
                role = "assistant"
                content = f"[from body {host}]\n{content}"
            if role in ("user", "assistant") and content:
                history.append({"role": role, "content": content})
        # Trim to short-term window — fathom_think itself also trims, but
        # being explicit here keeps the boundary visible.
        history = history[-SHORTTERM_TURNS:]

        # The last message IS the trigger — strip it from history so it's
        # not duplicated as both history and user_message.
        if history and history[-1].get("content", "").endswith(latest_content):
            history = history[:-1]

        messages = await fathom_think(
            user_message=latest_content,
            history=history,
            recall=True,
            session_slug=slug,
            on_tool_event=on_tool_event,
        )
        reply_text = (messages[-1].get("content") or "").strip() if messages else ""
        if not reply_text or reply_text == "<...>":
            # Active silence — Fathom heard, chose not to speak. Write a
            # short-lived ack delta so the UI knows the turn happened.
            # No persistence value, just a live receipt.
            print(f"chat-listener: silence in {slug} (<...>)", flush=True)
            try:
                await write_chat_event(slug, "silence", {})
            except Exception as e:
                print(f"chat-listener: silence ack failed: {e}", flush=True)
            return

        # Persist Fathom's reply the same way the old chat endpoint did.
        # db.add_message tags it with participant:fathom so the listener's
        # next tick skips it (own-writes filter).
        await db.add_message(slug, "assistant", reply_text)
        await db.touch_session(slug)


def _chat_slug(tags: list[str]) -> str | None:
    for t in tags:
        if t.startswith("chat:"):
            return t[len("chat:"):]
    return None


async def write_chat_event(session_slug: str, kind: str, data: dict) -> None:
    """Drop an ephemeral chat-event delta into the session.

    Tool uses (remember/recall/see_image/etc.) and silent acks all go
    through this. It's a normal lake delta with an expires_at window so
    the delta-store's reap loop cleans it up — no separate transport,
    no in-memory state, the UI just polls /v1/sessions/{id} like
    always and renders events alongside messages.

    Tag contract:
      fathom-chat           — so it's findable with the same chat query
      chat:<slug>           — session membership
      chat-event            — distinguishes from durable user/fathom messages
      event:<kind>          — the kind of thing that happened
      participant:fathom    — Fathom did this (keeps own-writes filter honest)
    """
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=EVENT_TTL_SECONDS)
    ).isoformat()
    tags = [
        "fathom-chat",
        f"chat:{session_slug}",
        "chat-event",
        f"event:{kind}",
        "participant:fathom",
    ]
    # Extra fields per event shape — a media_hash for image views, a
    # count for recall/remember, etc. Callers pass whatever matters.
    content = json.dumps({"kind": kind, **data})
    await delta_client.write(
        content=content,
        tags=tags,
        source="fathom-chat-event",
        expires_at=expires_at,
    )


# Module-level singleton — one listener per process.
listener = ChatListener()
