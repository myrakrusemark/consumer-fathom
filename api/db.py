"""Lake-backed sessions (matching loop-api pattern)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from . import delta_client
from .slug import generate_slug

# Match loop-api conventions exactly
LAKE_CHAT_TAG = "fathom-chat"
LAKE_CHAT_SOURCE = "fathom-chat"
LAKE_SESSION_LIST_WINDOW_DAYS = 30
LAKE_SESSION_LIST_LIMIT = 1000


# ── Sessions (lake-backed, matching loop-api) ───
#
# Sessions use combinatronic slugs (adj-adj-animal).
# Deltas are tagged: fathom-chat, chat:{slug}, role (user/assistant)
# Session names: fathom-chat, chat:{slug}, chat-name
# This is the SAME format as loop-api/server.py so sessions
# created by either surface are visible in both.


def _extract_chat_slug(tags: list[str]) -> str | None:
    """Return the chat:<slug> slug from a tag list."""
    for tag in tags:
        if tag.startswith("chat:"):
            return tag[5:]
    return None


async def create_session(title: str = "New session") -> dict:
    """Mint a fresh slug. No delta written until first message (or name)."""
    # Generate a unique slug by checking for collisions in the lake
    c = await delta_client._get()
    for _ in range(10):
        slug = generate_slug()
        r = await c.get("/deltas", params={"tags_include": f"chat:{slug}", "limit": 1})
        if r.status_code == 200 and not r.json():
            break
    return {"id": slug, "title": title, "created_at": datetime.now(timezone.utc).isoformat()}


async def list_sessions(limit: int = 50) -> list[dict]:
    """Aggregate fathom-chat deltas into session list — same as loop-api.

    Each session: {id (slug), title (name), first_seen, last_seen, delta_count, preview}.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=LAKE_SESSION_LIST_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    results = await delta_client.query(
        tags_include=[LAKE_CHAT_TAG],
        time_start=since,
        limit=LAKE_SESSION_LIST_LIMIT,
    )

    buckets: dict[str, dict] = {}
    for d in results:
        tags = d.get("tags") or []
        slug = _extract_chat_slug(tags)
        if not slug:
            slug = "legacy"
        ts = d.get("timestamp") or ""
        b = buckets.setdefault(slug, {
            "id": slug,
            "title": None,
            "_name_ts": "",
            "created_at": ts,
            "updated_at": ts,
            "delta_count": 0,
            "preview": "",
            "_preview_ts": "",
        })
        b["delta_count"] += 1
        if ts and ts < (b["created_at"] or ts):
            b["created_at"] = ts
        if ts and ts > (b["updated_at"] or ""):
            b["updated_at"] = ts

        content = d.get("content") or ""

        if "chat-name" in tags and ts >= b["_name_ts"]:
            b["title"] = content.strip() or None
            b["_name_ts"] = ts
        elif "user" in tags and ts >= b["_preview_ts"]:
            b["preview"] = content[:140]
            b["_preview_ts"] = ts

    # Clean up internal keys and sort
    legacy = buckets.get("legacy")
    if legacy and not legacy["title"]:
        legacy["title"] = "before sessions"

    sessions = []
    for b in buckets.values():
        sessions.append({
            "id": b["id"],
            "title": b["title"] or b["id"],
            "created_at": b["created_at"],
            "updated_at": b["updated_at"],
            "delta_count": b["delta_count"],
            "preview": b.get("preview", ""),
        })
    sessions.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return sessions[:limit]


async def get_session(session_id: str) -> dict | None:
    """Get session metadata by querying its chat-name delta."""
    results = await delta_client.query(
        tags_include=[LAKE_CHAT_TAG, f"chat:{session_id}"],
        limit=1,
    )
    if not results:
        # Session exists but has no deltas yet — still valid if just created
        return {"id": session_id, "title": session_id, "created_at": "", "updated_at": ""}
    d = results[0]
    # Find the name
    name_results = await delta_client.query(
        tags_include=[LAKE_CHAT_TAG, f"chat:{session_id}", "chat-name"],
        limit=1,
    )
    title = session_id
    if name_results:
        title = name_results[0].get("content", session_id).strip() or session_id
    return {
        "id": session_id,
        "title": title,
        "created_at": d.get("timestamp", ""),
        "updated_at": d.get("timestamp", ""),
    }


async def update_session(session_id: str, title: str) -> dict | None:
    """Rename by writing a chat-name delta — latest wins."""
    await delta_client.write(
        content=title,
        tags=[LAKE_CHAT_TAG, f"chat:{session_id}", "chat-name"],
        source=LAKE_CHAT_SOURCE,
    )
    return {"id": session_id, "title": title}


async def delete_session(session_id: str) -> bool:
    """Tombstone a session."""
    await delta_client.write(
        content=f"session deleted: {session_id}",
        tags=[LAKE_CHAT_TAG, f"chat:{session_id}", "chat-deleted"],
        source=LAKE_CHAT_SOURCE,
    )
    return True


async def touch_session(session_id: str):
    """No-op — updated_at is derived from latest delta timestamp."""
    pass


# ── Messages (lake-backed, matching loop-api tags) ──


async def add_message(
    session_id: str,
    role: str,
    content: str | None = None,
    tool_calls: str | None = None,
    tool_call_id: str | None = None,
) -> str:
    """Write a chat message as a delta — same tag format as loop-api."""
    tags = [LAKE_CHAT_TAG, f"chat:{session_id}", role]
    result = await delta_client.write(
        content=content or "",
        tags=tags,
        source=LAKE_CHAT_SOURCE,
    )
    return result.get("id", "")


async def get_messages(session_id: str, limit: int = 200) -> list[dict]:
    """Load session history from the lake.

    Includes deltas from all participants: the human (participant:user or
    tag 'user'), Fathom (participant:fathom or tag 'assistant'), and local
    agents (participant:agent:<host>). Agent deltas are surfaced with
    role='agent' and host extracted from their participant tag so the UI
    can render them distinctly.
    """
    # Query by session tag alone — the chat:<slug> tag is unique to chat and
    # catches agent outputs and routing deltas in addition to user/fathom
    # messages. Historical Fathom+user deltas also have fathom-chat tag.
    results = await delta_client.query(
        tags_include=[f"chat:{session_id}"],
        limit=limit,
    )
    # Newest-first from API, reverse for chronological
    results.reverse()
    messages = []
    for d in results:
        tags = d.get("tags", [])
        # Skip session metadata deltas.
        if "chat-name" in tags or "chat-deleted" in tags:
            continue

        # Derive role. Prefer participant:* tag (new convention). Fall back
        # to legacy role tags (user/assistant) for older sessions.
        role = None
        host = None
        for t in tags:
            if t.startswith("participant:agent:"):
                role = "agent"
                host = t[len("participant:agent:"):]
                break
            if t == "participant:user":
                role = "user"
                break
            if t == "participant:fathom":
                role = "assistant"
                break
        if role is None:
            if "user" in tags:
                role = "user"
            elif "assistant" in tags:
                role = "assistant"
        if role is None:
            continue

        msg = {
            "id": d.get("id"),
            "role": role,
            "content": d.get("content"),
            "created_at": d.get("timestamp"),
        }
        if host:
            msg["host"] = host
        if "signoff" in tags:
            msg["signoff"] = True
        if any(t.startswith("to:agent:") for t in tags):
            # Fathom's delegation message — surface it so the UI can render
            # "Fathom → agent" inline in the conversation.
            msg["routing"] = True
            msg["to_host"] = next(
                (t[len("to:agent:"):] for t in tags if t.startswith("to:agent:")),
                None,
            )
        if d.get("media_hash"):
            msg["media_hash"] = d["media_hash"]
        messages.append(msg)
    return messages


