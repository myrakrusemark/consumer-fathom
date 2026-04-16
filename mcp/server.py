"""Fathom MCP server — connect any MCP host to your lake.

Reads FATHOM_API_URL and FATHOM_API_KEY from environment.
Run: python -m mcp.server  (or configure in claude_desktop_config.json)
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.environ.get("FATHOM_API_URL", "http://localhost:8201")
API_KEY = os.environ.get("FATHOM_API_KEY", "")

mcp = FastMCP(
    "Fathom",
    instructions=(
        "Fathom is a personal memory lake. Use these tools to search, write, "
        "and query the user's lake of memories — fragments of thought, research, "
        "conversations, photos, and experience. Search before answering. "
        "Follow threads: if a result mentions something unfamiliar, search for that too."
    ),
)


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def _client() -> httpx.Client:
    return httpx.Client(base_url=API_URL, headers=_headers(), timeout=30)


# ── Tools ─────────────────────────────────────────


@mcp.tool()
def search_lake(query: str, limit: int = 20) -> str:
    """Search the memory lake with a natural language query.

    Returns semantically similar memories — conversations, notes, research,
    photos, sensor data, anything in the lake. Start here for any question
    about what the user knows, remembers, or has experienced.

    Args:
        query: What to search for. Be descriptive — "Nova mozzarella stretch
               kitchen photo" works better than "nova".
        limit: Max results (default 20).
    """
    with _client() as c:
        r = c.post("/v1/search", json={"origin": query, "limit": limit})
        r.raise_for_status()
        data = r.json()

    raw_results = data.get("results", data.get("deltas", []))
    if not raw_results:
        return "No results found."

    # Results may be {delta: {...}, distance: ...} or flat delta dicts
    results = []
    for r in raw_results:
        results.append(r.get("delta", r) if isinstance(r, dict) and "delta" in r else r)

    lines = [f"{len(results)} memories found:\n"]
    for d in results:
        ts = (d.get("timestamp") or "")[:16]
        tags = ", ".join((d.get("tags") or [])[:4])
        src = d.get("source", "")
        content = (d.get("content") or "")[:400]
        media = f" [image: {d['media_hash']}]" if d.get("media_hash") else ""
        lines.append(f"[{ts} · {src} · {tags}]{media}\n{content}\n")
    return "\n".join(lines)


@mcp.tool()
def write_delta(content: str, tags: list[str] | None = None, source: str = "mcp") -> str:
    """Write a memory to the lake.

    Use this to save observations, decisions, facts, notes, or anything
    worth remembering. One idea per delta. Tag consistently.

    Args:
        content: The memory content. Be specific and actionable — a future
                 search should find this useful.
        tags: Optional tags for filtering (e.g. ["meeting", "v2", "decision"]).
        source: Source label (default "mcp").
    """
    with _client() as c:
        body = {"content": content, "tags": tags or [], "source": source}
        r = c.post("/v1/deltas", json=body)
        r.raise_for_status()
        data = r.json()

    return f"Written. ID: {data.get('id', '?')}"


@mcp.tool()
def query_deltas(
    tags: list[str] | None = None,
    source: str | None = None,
    time_start: str | None = None,
    limit: int = 30,
) -> str:
    """Query the lake with structured filters (tags, source, time).

    Unlike search_lake (semantic), this is exact filtering. Use it when you
    know what tags or source you want, or need recent deltas from a time window.

    Args:
        tags: Filter to deltas that have ALL these tags.
        source: Filter by source (e.g. "homeassistant/print-farm", "fathom-chat").
        time_start: ISO timestamp — only deltas after this time.
        limit: Max results (default 30).
    """
    with _client() as c:
        params: dict = {"limit": limit}
        if tags:
            params["tags_include"] = ",".join(tags)
        if source:
            params["source"] = source
        if time_start:
            params["time_start"] = time_start
        r = c.get("/v1/deltas", params=params)
        r.raise_for_status()
        data = r.json()

    if not data:
        return "No deltas matched the filter."

    lines = [f"{len(data)} deltas:\n"]
    for d in data:
        ts = (d.get("timestamp") or "")[:16]
        tag_str = ", ".join((d.get("tags") or [])[:4])
        content = (d.get("content") or "")[:300]
        lines.append(f"[{ts} · {tag_str}]\n{content}\n")
    return "\n".join(lines)


@mcp.tool()
def lake_stats() -> str:
    """Get lake statistics — total deltas, embedding coverage, tag counts.

    Quick orientation tool. Call this first if you're unsure what's in the lake.
    """
    with _client() as c:
        stats_r = c.get("/v1/stats")
        stats_r.raise_for_status()
        stats = stats_r.json()

        tags_r = c.get("/v1/tags")
        tags_r.raise_for_status()
        all_tags = tags_r.json()

    # Summarize top tags
    if isinstance(all_tags, dict):
        sorted_tags = sorted(all_tags.items(), key=lambda x: x[1], reverse=True)[:20]
        tag_summary = ", ".join(f"{t}({n})" for t, n in sorted_tags)
    elif isinstance(all_tags, list):
        tag_summary = ", ".join(str(t) for t in all_tags[:20])
    else:
        tag_summary = str(all_tags)[:500]

    return (
        f"Lake: {stats.get('total', '?')} deltas, "
        f"{stats.get('embedded', '?')} embedded ({stats.get('percent', '?')}% coverage)\n\n"
        f"Top tags: {tag_summary}"
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
