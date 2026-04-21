"""Contacts + handles registry — minimum hard state for "who is talking
to Fathom."

After the v2 schema change, the contacts table holds only
`(slug, created_at, disabled_at)`. Everything that describes a person —
display_name, role, pronouns, bio, avatar, timezone, language, aliases —
lives in a `profile + contact:<slug>` delta in the lake, latest-wins.
Reading a contact merges this registry row with the latest profile
delta (the merge happens at the consumer-api layer, not here).

Handles keep their uniqueness contract per (channel, identifier) and
cascade-delete when the contact is hard-deleted. In practice deletion
is a soft tombstone via `disabled_at`; we rarely hard-delete.
"""

from __future__ import annotations

import asyncpg

from .store import _format_ts


def _row_to_contact(row: asyncpg.Record) -> dict:
    return {
        "slug": row["slug"],
        "created_at": _format_ts(row["created_at"]),
        "disabled_at": _format_ts(row["disabled_at"]) if row["disabled_at"] else None,
    }


def _row_to_handle(row: asyncpg.Record) -> dict:
    return {
        "contact_slug": row["contact_slug"],
        "channel": row["channel"],
        "identifier": row["identifier"],
        "created_at": _format_ts(row["created_at"]),
    }


class ContactsStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    # ── Registry (slug, created_at, disabled_at) ────────────────────────

    async def create(self, slug: str) -> dict:
        """Register a slug. Raises on duplicate. All other fields come
        from the profile delta and are written by the caller separately."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO contacts (slug) VALUES ($1)
                RETURNING *
                """,
                slug,
            )
            return _row_to_contact(row)

    async def get(self, slug: str, include_disabled: bool = False) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM contacts WHERE slug = $1", slug)
            if not row:
                return None
            if row["disabled_at"] and not include_disabled:
                return None
            return _row_to_contact(row)

    async def list_all(self, include_disabled: bool = False) -> list[dict]:
        async with self._pool.acquire() as conn:
            if include_disabled:
                rows = await conn.fetch(
                    "SELECT * FROM contacts ORDER BY created_at"
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM contacts WHERE disabled_at IS NULL ORDER BY created_at"
                )
            return [_row_to_contact(r) for r in rows]

    async def disable(self, slug: str) -> bool:
        """Soft-delete. Handles stay attached; the row stays queryable
        with include_disabled=True so the lake's provenance links don't
        become dangling refs."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE contacts SET disabled_at = NOW() WHERE slug = $1 AND disabled_at IS NULL",
                slug,
            )
            return status.endswith(" 1")

    async def reenable(self, slug: str) -> bool:
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE contacts SET disabled_at = NULL WHERE slug = $1",
                slug,
            )
            return status.endswith(" 1")

    async def hard_delete(self, slug: str) -> bool:
        """Remove the registry row entirely, cascading handles. Use
        sparingly — most deletion is soft via disable(). Kept for
        admin-initiated cleanup."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM contacts WHERE slug = $1", slug
            )
            return status.endswith(" 1")

    # ── Handles ─────────────────────────────────────────────────────────

    async def add_handle(
        self, contact_slug: str, channel: str, identifier: str
    ) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO handles (contact_slug, channel, identifier)
                VALUES ($1, $2, $3)
                RETURNING *
                """,
                contact_slug,
                channel,
                identifier,
            )
            return _row_to_handle(row)

    async def remove_handle(
        self, contact_slug: str, channel: str, identifier: str
    ) -> bool:
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                """
                DELETE FROM handles
                WHERE contact_slug = $1 AND channel = $2 AND identifier = $3
                """,
                contact_slug,
                channel,
                identifier,
            )
            return status.endswith(" 1")

    async def list_handles(self, contact_slug: str) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM handles WHERE contact_slug = $1 ORDER BY created_at",
                contact_slug,
            )
            return [_row_to_handle(r) for r in rows]

    async def resolve_handle(self, channel: str, identifier: str) -> str | None:
        """Return contact_slug for a (channel, identifier) pair, or None."""
        async with self._pool.acquire() as conn:
            slug = await conn.fetchval(
                "SELECT contact_slug FROM handles WHERE channel = $1 AND identifier = $2",
                channel,
                identifier,
            )
            return slug

    # ── Backfill (one-shot migration helper) ────────────────────────────

    async def backfill_contact_tag(
        self, contact_slug: str, filter_tags: list[str]
    ) -> dict:
        """Append `contact:<slug>` to every delta whose tags contain ANY of
        the filter tags and which has no `contact:` tag yet.

        Idempotent: running twice does nothing on the second pass.
        Returns counts for logging.
        """
        tag_to_add = f"contact:{contact_slug}"
        async with self._pool.acquire() as conn:
            candidates = await conn.fetchval(
                """
                SELECT COUNT(*) FROM deltas
                WHERE tags && $1
                  AND NOT EXISTS (
                    SELECT 1 FROM unnest(tags) t WHERE t LIKE 'contact:%'
                  )
                """,
                filter_tags,
            )
            updated = await conn.execute(
                """
                UPDATE deltas
                SET tags = array_append(tags, $2)
                WHERE tags && $1
                  AND NOT EXISTS (
                    SELECT 1 FROM unnest(tags) t WHERE t LIKE 'contact:%'
                  )
                """,
                filter_tags,
                tag_to_add,
            )
            affected = int(updated.split()[-1]) if updated else 0
        return {
            "candidates": int(candidates or 0),
            "updated": affected,
            "tag_added": tag_to_add,
            "filter_tags": filter_tags,
        }
