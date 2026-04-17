"""Recall timeline — deltas flowing OUT of the lake.

Sibling to usage.py (deltas flowing in). The actual event log lives at
the delta-store, which counts every retrieval across every client
(consumer-api, loop-api, MCP, CLI). This module just proxies the
bucketed history so the Stats widget can plot capture vs recall side
by side.
"""

from __future__ import annotations

from . import delta_client


async def history(since_seconds: int, buckets: int = 60) -> list[dict]:
    """Bucketed delta-retrieval counts across the window. {t, v} shape."""
    if since_seconds <= 0 or buckets <= 0:
        return []
    try:
        return await delta_client.retrievals_history(
            since_seconds=since_seconds, buckets=buckets
        )
    except Exception:
        return []
