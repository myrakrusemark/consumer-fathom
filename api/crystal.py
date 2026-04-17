"""Identity crystal — lake as the single source of truth.

Old design kept the canonical crystal in a JSON file on disk and treated
lake deltas tagged identity-crystal as a side-effect history record. That
made the file and the lake able to drift, and it forced two separate
definitions: "what to load" (the file) vs. "what counts as a crystal
event" (the tag).

New design: the lake is canonical. The most recent delta matching the
crystal-regen rule IS the current crystal. The same rule defines what
the ECG counts as a regeneration event.

Detection rule
--------------
A delta is a crystal regeneration if either:
  (a) it has the new tag ``crystal-regen`` (canonical going forward), OR
  (b) it has tag ``identity-crystal`` AND its source is one of the known
      crystal-writing processes (back-compat for historical deltas).

This excludes vault notes, dashboard logs, and research entries that
mention the crystal but aren't generations.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from . import delta_client

# Sources that historically wrote real crystal regenerations.
# Used only for back-compat with deltas written before the crystal-regen
# tag existed. New writes always carry the crystal-regen tag.
LEGACY_REGEN_SOURCES: frozenset[str] = frozenset({
    "consumer-api",
    "loop-api",
    "fathom-loop",
    "recall-loop",
    "claude-code",
    "fathom-control",
})

REGEN_TAG = "crystal-regen"
LEGACY_TAG = "identity-crystal"

# Small TTL cache so chat turns don't hammer the lake. Crystals don't
# change often; even a 10-second window is plenty.
_CACHE_TTL_SECONDS = 10.0
_cache: dict | None = None
_cache_at: float = 0.0
_cache_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def matches_regen(delta: dict) -> bool:
    """Apply the canonical detection rule to a single delta."""
    tags = delta.get("tags") or []
    if REGEN_TAG in tags:
        return True
    if LEGACY_TAG in tags and (delta.get("source") or "") in LEGACY_REGEN_SOURCES:
        return True
    return False


async def _fetch_latest_uncached() -> dict | None:
    """Find the most recent crystal-regen delta in the lake.

    Strategy: prefer the new tag (cheap, exact). Fall back to the legacy
    tag with source filtering applied client-side.
    """
    # Preferred path — anything written under the new convention.
    try:
        results = await delta_client.query(tags_include=[REGEN_TAG], limit=1)
    except Exception:
        results = []
    if results:
        return _to_crystal(results[0])

    # Back-compat path — pull recent identity-crystal deltas, source-filter.
    try:
        legacy = await delta_client.query(tags_include=[LEGACY_TAG], limit=50)
    except Exception:
        legacy = []
    for d in legacy:
        if matches_regen(d):
            return _to_crystal(d)
    return None


def _to_crystal(delta: dict) -> dict:
    return {
        "id": delta.get("id"),
        "text": delta.get("content") or "",
        "created_at": delta.get("timestamp"),
        "source": delta.get("source"),
        "tags": delta.get("tags") or [],
    }


async def latest(force: bool = False) -> dict | None:
    """Return the canonical current crystal. Brief in-memory cache."""
    global _cache, _cache_at
    now = time.monotonic()
    if not force and _cache is not None and (now - _cache_at) < _CACHE_TTL_SECONDS:
        return _cache
    async with _cache_lock:
        if not force and _cache is not None and (time.monotonic() - _cache_at) < _CACHE_TTL_SECONDS:
            return _cache
        fresh = await _fetch_latest_uncached()
        _cache = fresh
        _cache_at = time.monotonic()
    return fresh


async def latest_text() -> str | None:
    """Convenience: just the crystal text, for prompt injection."""
    c = await latest()
    return c.get("text") if c else None


async def write(text: str, source: str = "consumer-api") -> dict:
    """Write a fresh crystal regen to the lake.

    Carries both tags: identity-crystal (legacy compat for any consumer
    that filters by it) and crystal-regen (the new canonical filter).
    Invalidates the cache so the next read sees the fresh write.
    """
    global _cache, _cache_at
    written = await delta_client.write(
        content=text,
        tags=[LEGACY_TAG, REGEN_TAG],
        source=source,
    )
    _cache = None
    _cache_at = 0.0
    return written


async def list_events(limit: int = 50) -> list[dict]:
    """Return crystal regeneration events (filtered, time-sorted) for the ECG."""
    # Pull both tag pools and de-dup; the new tag will catch nothing for a
    # while, but back-fills as new regens land.
    seen: dict[str, dict] = {}
    try:
        new_pool = await delta_client.query(tags_include=[REGEN_TAG], limit=limit)
    except Exception:
        new_pool = []
    try:
        legacy_pool = await delta_client.query(tags_include=[LEGACY_TAG], limit=max(limit * 2, 50))
    except Exception:
        legacy_pool = []
    for d in list(new_pool) + list(legacy_pool):
        if not matches_regen(d):
            continue
        did = d.get("id")
        if did and did not in seen:
            seen[did] = d
    events = []
    for d in seen.values():
        events.append({
            "id": d.get("id"),
            "timestamp": d.get("timestamp"),
            "source": d.get("source"),
            "preview": (d.get("content") or "")[:140],
        })
    events.sort(key=lambda e: e.get("timestamp") or "")
    return events[-limit:]
