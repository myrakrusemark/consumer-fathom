"""Async HTTP client for the delta store API."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .settings import settings

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


async def _get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        headers = {}
        if settings.delta_api_key:
            headers["X-API-Key"] = settings.delta_api_key
        _client = httpx.AsyncClient(
            base_url=settings.delta_store_url,
            headers=headers,
            timeout=30.0,
        )
    return _client


async def close():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# ── Search ──────────────────────────────────────

async def search(
    query: str,
    limit: int = 20,
    radii: dict | None = None,
    tags_include: list[str] | None = None,
) -> dict:
    c = await _get()
    body: dict = {"origin": query, "limit": min(limit, 50)}
    if radii:
        body["radii"] = radii
    if tags_include:
        body["tags_include"] = tags_include
    r = await c.post("/search", json=body)
    r.raise_for_status()
    return r.json()


# ── Write ───────────────────────────────────────

async def write(
    content: str,
    tags: list[str] | None = None,
    source: str = "consumer-api",
) -> dict:
    c = await _get()
    body = {"content": content, "source": source, "tags": tags or []}
    r = await c.post("/deltas", json=body)
    r.raise_for_status()
    result = r.json()
    _record_pressure({"content": content, "tags": tags or [], "source": source})
    return result


def _record_pressure(delta: dict) -> None:
    """Fire-and-forget pressure update. Imported lazily to avoid circular import."""
    try:
        from . import pressure as _pressure
    except Exception:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_pressure.add_delta(delta))
    task.add_done_callback(_swallow_pressure_error)


def _swallow_pressure_error(task: asyncio.Task) -> None:
    exc = task.exception()
    if exc is not None:
        log.debug("pressure update failed: %s", exc)


# ── Query (structured filter) ───────────────────

async def query(
    limit: int = 50,
    tags_include: list[str] | None = None,
    source: str | None = None,
    time_start: str | None = None,
) -> list:
    c = await _get()
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


# ── Plan (compositional query) ──────────────────

async def plan(steps: list[dict]) -> dict:
    c = await _get()
    r = await c.post("/plan", json={"steps": steps})
    r.raise_for_status()
    return r.json()


# ── Single delta ────────────────────────────────

async def get_delta(delta_id: str) -> dict:
    c = await _get()
    r = await c.get(f"/deltas/{delta_id}")
    r.raise_for_status()
    return r.json()


# ── Meta ────────────────────────────────────────

async def tags() -> dict:
    c = await _get()
    r = await c.get("/tags")
    r.raise_for_status()
    return r.json()


async def stats() -> dict:
    c = await _get()
    r = await c.get("/stats")
    r.raise_for_status()
    return r.json()


async def upload_media(
    file_bytes: bytes,
    filename: str,
    content: str = "",
    tags: list[str] | None = None,
    source: str = "fathom-chat",
) -> dict:
    """Upload an image to the delta store, returns {id, media_hash}."""
    import io
    c = await _get()
    files = {"file": (filename, io.BytesIO(file_bytes), "application/octet-stream")}
    data = {
        "content": content,
        "tags": ",".join(tags or []),
        "source": source,
    }
    r = await c.post("/deltas/media/upload", files=files, data=data, timeout=30)
    r.raise_for_status()
    return r.json()


async def recent_deltas_timestamps(limit: int = 5000) -> list[str]:
    """Fetch timestamps of recent deltas for the usage chart."""
    c = await _get()
    r = await c.get("/deltas", params={"limit": limit})
    r.raise_for_status()
    return [d.get("timestamp", "")[:10] for d in r.json() if d.get("timestamp")]


async def feed_stories(limit: int = 50, offset: int = 0) -> dict:
    c = await _get()
    r = await c.get("/feed/stories", params={"limit": limit, "offset": offset})
    r.raise_for_status()
    return r.json()


async def drift(text: str, since: str | None = None) -> dict:
    """Compute crystal drift via the delta-store's /drift endpoint.

    Returns {drift, new_deltas, total_deltas}. Drift is cosine distance
    (0 = aligned, ~2 = opposite) between the supplied text's embedding
    and the lake's exponentially-decayed centroid (7-day half-life).
    """
    c = await _get()
    body = {"text": text, "since": since or ""}
    r = await c.post("/drift", json=body, timeout=20)
    r.raise_for_status()
    return r.json()
