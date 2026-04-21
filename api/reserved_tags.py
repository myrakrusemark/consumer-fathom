"""Authority-bearing tags — the narrow list of tags that are NOT data.

See docs/reserved-tags-spec.md for the full design. This module owns the
registry and the gate-evaluation function. The gate is invoked from
POST /v1/deltas (the single external path that accepts caller-supplied
tag lists); internal writers that call delta_client.write directly are
inside the trust boundary and bypass this check.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from . import delta_client


# ── Gate identifiers ──────────────────────────────

GATE_ADMIN = "admin"
GATE_ADMIN_OR_SELF = "admin_or_self"
GATE_SESSION_MEMBER_OR_ADMIN = "session_member_or_admin"
GATE_INTERNAL = "internal"

# Plain human-readable hint pointed at the right admin endpoint — used in
# 403 responses so a caller who stumbled into a reserved tag can find the
# correct path. Do not include sensitive state.
GATE_HINTS = {
    "profile": "Writable via POST /v1/contacts/<slug> (admin) or PATCH /v1/me/profile (self).",
    "contact-deleted": "Writable via DELETE /v1/contacts/<slug> (admin).",
    "crystal:identity": "Writable via POST /v1/crystal/refresh (admin).",
    "crystal:feed-orient": "Writable via POST /v1/feed/crystal/refresh.",
    "feed-anchor": "Written internally by the feed-crystal module.",
    "routine-fire": "Writable via POST /v1/routines/<id>/fire (admin).",
    "routine-definition": "Writable via POST /v1/routines (admin).",
    "resonance-allowed": "Writable via POST /hooks/activation/sources (admin).",
    "chat-deleted": "Writable via DELETE /v1/sessions/<id>.",
    "chat-name": "Writable via PATCH /v1/sessions/<id>.",
    # Gate-side defaults for tags whose gate is admin_or_self via /v1/deltas:
    "agent-heartbeat": "Must be tagged with the authenticated caller's own contact.",
    "routine-summary": "Must be tagged with the authenticated caller's own contact.",
}

# Hints for prefix-reserved tags. Looked up by the prefix (with colon).
GATE_PREFIX_HINTS = {
    "handle:": "Writable via POST /v1/contacts/<slug>/handles (admin — uniqueness checked there).",
}


# ── Registry ──────────────────────────────────────
#
# Exact-match entries and prefix entries are stored separately so a tag
# like "handle:telegram:abc" matches the "handle:" prefix cleanly without
# colliding with an exact "handle" entry. All lookups go through the
# resolve() function, never through the dicts directly.

_EXACT: dict[str, str] = {
    # Authority-bearing tags that already have a named endpoint. All of
    # them are written by consumer-api code that calls delta_client.write
    # directly (not through /v1/deltas), so GATE_INTERNAL cleanly says
    # "use the named endpoint." No content-level bypass surface.
    "profile": GATE_INTERNAL,              # /v1/contacts/<slug> (admin), /v1/me/profile (self)
    "contact-deleted": GATE_INTERNAL,      # DELETE /v1/contacts/<slug>
    "crystal:identity": GATE_INTERNAL,     # POST /v1/crystal/refresh
    "crystal:feed-orient": GATE_INTERNAL,  # POST /v1/feed/crystal/refresh
    "feed-anchor": GATE_INTERNAL,          # feed_crystal._snapshot_anchor
    "routine-fire": GATE_INTERNAL,         # POST /v1/routines/<id>/fire
    "routine-definition": GATE_INTERNAL,   # POST /v1/routines
    "resonance-allowed": GATE_INTERNAL,    # POST /hooks/activation/sources
    "chat-deleted": GATE_INTERNAL,         # DELETE /v1/sessions/<id>
    "chat-name": GATE_INTERNAL,            # PATCH /v1/sessions/<id>
    # Tags external callers legitimately produce via /v1/deltas, bound to
    # the caller's own contact by strip-and-re-stamp. admin_or_self reads
    # "the tag's contact:<slug> must equal the caller" which is always
    # true after strip-and-re-stamp on a self-write.
    "agent-heartbeat": GATE_ADMIN_OR_SELF,  # agent plugin presence
    "routine-summary": GATE_ADMIN_OR_SELF,  # CLI report from a spawned routine
}

_PREFIX: dict[str, str] = {
    # handle bindings must check cross-contact uniqueness; admin endpoint
    # owns that check. Raw writes are refused regardless of caller.
    "handle:": GATE_INTERNAL,
}


def resolve(tag: str) -> str | None:
    """Return the gate that governs this tag, or None if it's plain data."""
    if not isinstance(tag, str):
        return None
    gate = _EXACT.get(tag)
    if gate is not None:
        return gate
    for prefix, gate in _PREFIX.items():
        if tag.startswith(prefix):
            return gate
    return None


def hint_for(tag: str) -> str:
    """Human-readable pointer to the correct admin endpoint for this tag."""
    direct = GATE_HINTS.get(tag)
    if direct:
        return direct
    for prefix in _PREFIX:
        if tag.startswith(prefix):
            return GATE_PREFIX_HINTS.get(prefix, "")
    return ""


# ── Session-membership cache ──────────────────────
#
# A tier-2 gate (chat-deleted, chat-name) asks "has this contact written
# into this session before?" A lake query per protected write is cheap
# at steady state but wasteful on repeat. Cache the positive answers
# lazily — once confirmed, a pair stays true for the life of the process
# (session membership only grows; nobody is removed from a session).

_member_cache: set[tuple[str, str]] = set()
_member_lock = asyncio.Lock()


async def is_session_member(contact_slug: str, session_slug: str) -> bool:
    """True iff this contact has previously written into this session."""
    key = (contact_slug, session_slug)
    if key in _member_cache:
        return True
    async with _member_lock:
        if key in _member_cache:
            return True
        try:
            results = await delta_client.query(
                tags_include=[f"chat:{session_slug}", f"contact:{contact_slug}"],
                limit=1,
            )
        except Exception:
            return False
        if results:
            _member_cache.add(key)
            return True
    return False


# ── Gate evaluation ───────────────────────────────


@dataclass
class GateResult:
    ok: bool
    tag: str | None = None
    gate: str | None = None
    hint: str | None = None


async def evaluate(
    tags: list[str],
    caller_contact: dict | None,
) -> GateResult:
    """Check a write's tag list against the registry.

    Returns GateResult(ok=True) if every reserved tag passes. Returns
    GateResult(ok=False, tag=<first-violation>, gate=<...>, hint=<...>)
    on the first failure so the caller can short-circuit.

    `caller_contact` is the authenticated caller's contact dict (slug,
    role, …). When None, only `internal`-gated tags are possible, and
    none of them pass.
    """
    caller_slug = (caller_contact or {}).get("slug")
    is_admin = (caller_contact or {}).get("role") == "admin"

    # Snapshot the tag's own contact: anchor if present — used by
    # admin_or_self. After strip-and-re-stamp in the endpoint, this is
    # always the caller's own slug on a self-write. For admin-written
    # deltas addressed to others (internal paths), this gate doesn't
    # apply because internals bypass evaluate() entirely.
    tag_contact = None
    for t in tags:
        if isinstance(t, str) and t.startswith("contact:"):
            tag_contact = t[len("contact:"):]
            break

    # Chat session referenced in the tag set, for session_member_or_admin.
    session_slug = None
    for t in tags:
        if isinstance(t, str) and t.startswith("chat:"):
            session_slug = t[len("chat:"):]
            break

    for tag in tags:
        gate = resolve(tag)
        if gate is None:
            continue

        if gate == GATE_INTERNAL:
            return GateResult(False, tag=tag, gate=gate, hint=hint_for(tag))

        if gate == GATE_ADMIN:
            if is_admin:
                continue
            return GateResult(False, tag=tag, gate=gate, hint=hint_for(tag))

        if gate == GATE_ADMIN_OR_SELF:
            if is_admin:
                continue
            if caller_slug and tag_contact == caller_slug:
                continue
            return GateResult(False, tag=tag, gate=gate, hint=hint_for(tag))

        if gate == GATE_SESSION_MEMBER_OR_ADMIN:
            if is_admin:
                continue
            if caller_slug and session_slug:
                if await is_session_member(caller_slug, session_slug):
                    continue
            return GateResult(False, tag=tag, gate=gate, hint=hint_for(tag))

        # Unknown gate — fail closed.
        return GateResult(False, tag=tag, gate=gate, hint=hint_for(tag))

    return GateResult(True)


def strip_contact_tags(tags: list[str]) -> list[str]:
    """Drop every caller-supplied `contact:*` tag. Universal precondition
    to every raw write. The authenticated caller's `contact:<slug>` is
    stamped separately by the endpoint so a caller cannot address a
    delta to someone else."""
    return [t for t in (tags or []) if not (isinstance(t, str) and t.startswith("contact:"))]
