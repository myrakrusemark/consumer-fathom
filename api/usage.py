"""Usage timeline — sibling to pressure, but unweighted and undecayed.

Pressure asks "how much salient activity has built up since I last
checked in" (weighted by source, exponentially decayed, reset at every
mood synthesis).

Usage asks "how busy was the lake at each moment" — a raw count of
fragments-per-bucket across the window. No weights, no decay, no reset.

Both pull from the same lake query, just bucketed at whatever resolution
the chosen window calls for so the two lines render at matching density.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import delta_client

USAGE_QUERY_LIMIT: int = 5000


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


async def history(since_seconds: int, buckets: int = 60) -> list[dict]:
    """Return delta-count timeline across the window in N buckets.

    Each entry: {t: iso, v: int}. v is a raw count, not weighted.
    """
    if since_seconds <= 0 or buckets <= 0:
        return []

    now = _now()
    start = now - timedelta(seconds=since_seconds)
    try:
        results = await delta_client.query(
            time_start=start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            limit=USAGE_QUERY_LIMIT,
        )
    except Exception:
        results = []

    bucket_seconds = since_seconds / buckets
    counts = [0] * buckets
    for d in results or []:
        ts = _parse(d.get("timestamp"))
        if not ts:
            continue
        offset = (ts - start).total_seconds()
        if offset < 0:
            continue
        idx = int(offset / bucket_seconds)
        if idx >= buckets:
            idx = buckets - 1
        counts[idx] += 1

    out: list[dict] = []
    for i, c in enumerate(counts):
        tick = start + timedelta(seconds=bucket_seconds * (i + 0.5))
        out.append({"t": _iso(tick), "v": c})
    return out
