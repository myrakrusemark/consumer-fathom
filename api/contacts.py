"""Contacts — merge registry row + latest profile delta into a full contact.

The registry row (delta-store Postgres) is the authoritative "does this
slug exist / is it disabled" check. The profile delta (lake) is the
authoritative source for every soft field: display_name, role, pronouns,
timezone, language, bio, avatar, aliases.

This module merges the two. Every contact-read path goes through
`get(slug)` or `list_all()` here, which are the only functions that know
about the merge. The auth middleware caches the merged dict for 60s.

Writes:
- `create(slug, initial_profile)` — registers the slug, writes the first
  profile delta.
- `update_profile(slug, changes, actor_slug)` — merges changes into the
  current profile, writes a fresh profile delta. Role field is NOT
  enforced here — caller endpoints are responsible for stripping
  role from self-edit bodies before calling this.
- `disable(slug)` — sets the registry tombstone, writes a
  `contact-deleted` delta.
"""

from __future__ import annotations

import json
import logging

from . import delta_client


log = logging.getLogger(__name__)


# Profile fields — the JSON shape stored in the `profile` delta.
# Kept permissive: unknown fields are allowed (future-proofing), but
# these are the ones the UI and system know about.
PROFILE_DEFAULTS: dict = {
    "role": "member",
    "display_name": "",
    "pronouns": "",
    "timezone": "",
    "language": "",
    "bio": "",
    "avatar": "",
    "aliases": [],
}


def _fallback_profile(slug: str) -> dict:
    """Returned when a contact exists in the registry but has no profile
    delta yet (created but not written). Keeps the UI from crashing on
    missing fields."""
    return {
        **PROFILE_DEFAULTS,
        "display_name": slug,
    }


async def _fetch_latest_profile(slug: str) -> dict | None:
    """Find the most recent `profile + contact:<slug>` delta."""
    try:
        results = await delta_client.query(
            tags_include=["profile", f"contact:{slug}"],
            limit=1,
        )
    except Exception:
        log.exception("contacts: profile fetch failed for %s", slug)
        return None
    if not results:
        return None
    delta = results[0]
    raw = delta.get("content") or ""
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None
    except json.JSONDecodeError:
        return None
    return {
        **PROFILE_DEFAULTS,
        **parsed,
        "_profile_delta_id": delta.get("id"),
        "_profile_updated_at": delta.get("timestamp"),
    }


async def _write_profile_delta(
    slug: str, profile: dict, event: str, actor_slug: str | None
) -> None:
    """Write a fresh `profile + contact:<slug>` delta with the merged
    content. Bypasses the reserved-tag gate by calling delta_client.write
    directly — this IS the trusted internal path."""
    # Don't bake the _-prefixed meta fields into the content.
    payload = {k: v for k, v in profile.items() if not k.startswith("_")}
    tags = ["contact", f"contact:{slug}", "profile", f"profile-event:{event}"]
    if actor_slug and actor_slug != slug:
        tags.append(f"actor:{actor_slug}")
    try:
        await delta_client.write(
            content=json.dumps(payload, ensure_ascii=False),
            tags=tags,
            source="dashboard" if actor_slug else "system",
        )
    except Exception:
        log.exception("contacts: profile delta write failed for %s", slug)


async def get(slug: str, include_disabled: bool = False) -> dict | None:
    """Merged contact dict, or None if the slug doesn't exist (or is
    disabled and include_disabled=False)."""
    row = await delta_client.get_contact_row(slug, include_disabled=include_disabled)
    if not row:
        return None
    profile = await _fetch_latest_profile(slug) or _fallback_profile(slug)
    return {**row, **profile}


async def list_all(include_disabled: bool = False) -> list[dict]:
    rows = await delta_client.list_contact_rows(include_disabled=include_disabled)
    out = []
    for row in rows:
        profile = await _fetch_latest_profile(row["slug"]) or _fallback_profile(row["slug"])
        out.append({**row, **profile})
    return out


async def create(
    slug: str,
    initial_profile: dict | None = None,
    actor_slug: str | None = None,
) -> dict:
    """Register a slug and write its first profile delta atomically
    from the caller's perspective. Raises httpx.HTTPStatusError on
    duplicate slug."""
    row = await delta_client.create_contact_row(slug)
    profile = {**PROFILE_DEFAULTS, **(initial_profile or {})}
    # If the admin didn't set a display_name, default to the slug so the
    # UI has something readable from the first frame.
    if not profile.get("display_name"):
        profile["display_name"] = slug
    await _write_profile_delta(slug, profile, event="created", actor_slug=actor_slug)
    return {**row, **profile}


async def update_profile(
    slug: str,
    changes: dict,
    actor_slug: str | None,
    event: str = "updated",
) -> dict | None:
    """Merge `changes` into the current profile and write a fresh delta.

    Returns the new merged contact, or None if the slug doesn't exist.
    Callers are responsible for stripping fields that the caller isn't
    allowed to change (e.g. role on a self-edit).
    """
    row = await delta_client.get_contact_row(slug)
    if not row:
        return None
    current = await _fetch_latest_profile(slug) or _fallback_profile(slug)
    # Drop private meta keys before merging
    base = {k: v for k, v in current.items() if not k.startswith("_")}
    merged = {**base, **{k: v for k, v in changes.items() if v is not None}}
    await _write_profile_delta(slug, merged, event=event, actor_slug=actor_slug)
    return {**row, **merged}


async def disable(slug: str, actor_slug: str | None) -> bool:
    """Tombstone the contact. Sets registry disabled_at and writes a
    `contact-deleted` delta for lake-side provenance."""
    try:
        await delta_client.disable_contact_row(slug)
    except Exception:
        return False
    tags = [
        "contact",
        f"contact:{slug}",
        "contact-deleted",
        f"profile-event:disabled",
    ]
    if actor_slug and actor_slug != slug:
        tags.append(f"actor:{actor_slug}")
    try:
        await delta_client.write(
            content=f"Contact {slug} disabled.",
            tags=tags,
            source="dashboard" if actor_slug else "system",
        )
    except Exception:
        log.exception("contacts: tombstone delta write failed for %s", slug)
    return True
