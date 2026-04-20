"""Async HTTP client for the delta store API."""
from __future__ import annotations

import httpx

from .settings import settings

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
    expires_at: str | None = None,
) -> dict:
    c = await _get()
    body = {"content": content, "source": source, "tags": tags or []}
    if expires_at:
        body["expires_at"] = expires_at
    r = await c.post("/deltas", json=body)
    r.raise_for_status()
    return r.json()


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


async def retrievals_history(since_seconds: int, buckets: int = 60) -> list[dict]:
    """Fetch bucketed delta-retrieval timeline from the lake."""
    c = await _get()
    r = await c.get(
        "/stats/retrievals/history",
        params={"since_seconds": since_seconds, "buckets": buckets},
    )
    r.raise_for_status()
    return r.json().get("history", [])


async def usage_history(since_seconds: int, buckets: int = 60) -> list[dict]:
    """Fetch bucketed delta-write timeline from the lake (SQL-bucketed, no row cap)."""
    c = await _get()
    r = await c.get(
        "/stats/usage/history",
        params={"since_seconds": since_seconds, "buckets": buckets},
    )
    r.raise_for_status()
    return r.json().get("history", [])


async def pressure_history(
    *,
    since_seconds: int,
    buckets: int,
    weights: dict[str, float],
    default_weight: float,
    user_tag_boost: float,
    half_life_seconds: int,
) -> list[dict]:
    """Fetch bucketed weighted-decay pressure curve (SQL-computed, no row cap)."""
    c = await _get()
    r = await c.post(
        "/stats/pressure/history",
        json={
            "since_seconds": since_seconds,
            "buckets": buckets,
            "weights": weights,
            "default_weight": default_weight,
            "user_tag_boost": user_tag_boost,
            "half_life_seconds": half_life_seconds,
        },
    )
    r.raise_for_status()
    return r.json().get("history", [])


async def pressure_volume(
    *,
    cutoff_ts: str | None,
    window_seconds: int,
    weights: dict[str, float],
    default_weight: float,
    user_tag_boost: float,
    half_life_seconds: int,
) -> float:
    """Single weighted-decay pressure value since cutoff (or window)."""
    c = await _get()
    r = await c.post(
        "/stats/pressure/volume",
        json={
            "cutoff_ts": cutoff_ts,
            "window_seconds": window_seconds,
            "weights": weights,
            "default_weight": default_weight,
            "user_tag_boost": user_tag_boost,
            "half_life_seconds": half_life_seconds,
        },
    )
    r.raise_for_status()
    return float(r.json().get("volume", 0.0))


async def upload_media(
    file_bytes: bytes,
    filename: str,
    content: str = "",
    tags: list[str] | None = None,
    source: str = "fathom-chat",
    expires_at: str | None = None,
) -> dict:
    """Upload an image to the delta store, returns {id, media_hash}."""
    import io
    c = await _get()
    files = {"file": (filename, io.BytesIO(file_bytes), "application/octet-stream")}
    data: dict = {
        "content": content,
        "tags": ",".join(tags or []),
        "source": source,
    }
    if expires_at:
        data["expires_at"] = expires_at
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

    Used at crystal-write time to validate that a candidate crystal
    actually describes the lake — drift outside the accept band (see
    api.server.refresh_crystal) means the LLM produced an artifact that
    doesn't reflect current mental state.
    """
    c = await _get()
    body = {"text": text, "since": since or ""}
    r = await c.post("/drift", json=body, timeout=20)
    r.raise_for_status()
    return r.json()


async def centroid() -> dict:
    """Fetch the raw lake centroid vector from delta-store.

    Returns {centroid: [floats]|None, dim, total_deltas}. Called at
    crystal-write time to snapshot the anchor, and at each drift tick
    to compute how far the lake has moved since the anchor was set.
    """
    c = await _get()
    r = await c.get("/centroid", timeout=20)
    r.raise_for_status()
    return r.json()
