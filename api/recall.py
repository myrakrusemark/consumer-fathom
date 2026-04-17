"""Recall events — moments where Fathom actively pulled from its mind.

Counts only meaningful recalls: searches that Fathom (or the user)
initiates as part of "remembering," not the background filter queries
the system makes to load mood / crystal / drift state.

The hook fires from api.search.search() after a successful semantic
recall, so any caller that goes through the canonical NL recall path
gets counted — chat turns, MCP search_lake tool calls, the API search
endpoint. Internal filter queries (delta_client.query) bypass this on
purpose.

Stored as a rolling event log in JSON, capped, bucketed on read like
the pressure history endpoint. Sibling to usage.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .settings import settings

EVENT_LIMIT: int = 5000  # rolling cap; covers many days at typical use

_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _path() -> Path:
    base = Path(settings.mood_state_path).parent
    return base / "recall-history.json"


def _load_raw() -> dict:
    p = _path()
    if not p.exists():
        return {"events": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"events": []}


def _save_raw(state: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".recall-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


async def record_event(weight: float = 1.0) -> None:
    """Fire-and-forget log of one recall event."""
    if weight <= 0:
        return
    async with _lock:
        state = _load_raw()
        events = state.get("events") or []
        events.append({"t": _iso(_now()), "w": weight})
        if len(events) > EVENT_LIMIT:
            events = events[-EVENT_LIMIT:]
        state["events"] = events
        _save_raw(state)


async def history(since_seconds: int, buckets: int = 60) -> list[dict]:
    """Return bucketed recall counts across the window. Same shape as
    usage.history so the UI can plot them side by side.
    """
    if since_seconds <= 0 or buckets <= 0:
        return []
    async with _lock:
        state = _load_raw()
        events = list(state.get("events") or [])

    now = _now()
    start = now - timedelta(seconds=since_seconds)
    bucket_seconds = since_seconds / buckets
    counts = [0.0] * buckets
    for e in events:
        ts = _parse(e.get("t"))
        if not ts:
            continue
        offset = (ts - start).total_seconds()
        if offset < 0:
            continue
        idx = int(offset / bucket_seconds)
        if idx >= buckets:
            idx = buckets - 1
        counts[idx] += float(e.get("w", 1.0))

    out: list[dict] = []
    for i, c in enumerate(counts):
        tick = start + timedelta(seconds=bucket_seconds * (i + 0.5))
        out.append({"t": _iso(tick), "v": round(c, 3)})
    return out


def fire_and_forget(weight: float = 1.0) -> None:
    """Schedule a record_event from a sync context. No-op if no loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(record_event(weight))
