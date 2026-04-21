"""Feed loop — page-view-debounced consumer of the feed-orient crystal.

The crystal lives in api/feed_crystal.py and answers "what to put in
Myra's feed right now." This module answers "when, and what cards
land." It runs in-process inside consumer-api — no agent, no routine,
no external scheduler. The dashboard load is the wake event.

Each fire:
  1. Wake-gate the crystal (regen if drift/confidence say so).
  2. Read the latest crystal.
  3. For each directive_line:
     • Skip if a fresh-enough card already exists.
     • Otherwise spend a budget on fathom_think to produce one card.
     • Write a `feed-card` delta tagged back to the directive line.
  4. Return.

A single-flight lock keeps simultaneous visits from firing the loop
twice. Status is exposed via `current_status()` so the UI indicator can
show "generating…" while it runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import delta_client, feed_crystal
from .settings import settings

log = logging.getLogger(__name__)
# uvicorn's default config keeps app loggers at WARNING. Pin to INFO so the
# feed-loop's per-line decisions land in `podman logs` for debugging.
logging.getLogger(__name__).setLevel(logging.INFO)

CARD_TAG = "feed-card"
CARD_SOURCE = "fathom-feed"


def _contact_tag(contact_slug: str) -> str:
    return f"contact:{contact_slug}"


def _empty_status() -> dict[str, Any]:
    return {
        "generating": False,
        "started_at": None,
        "finished_at": None,
        "lines_total": 0,
        "lines_done": 0,
        "last_reason": None,
        # True only while an LLM call is actually in flight (crystal
        # synthesis or card production via fathom_think). Distinct from
        # `generating` — a no-op run where all directive lines are fresh
        # never fires an LLM, so `llm_active` stays false throughout and
        # the UI pulse correctly doesn't flash.
        "llm_active": False,
        "llm_active_count": 0,  # counter so concurrent calls nest cleanly
        # Short human-readable label of what the current LLM call is
        # actually doing — "Updating feed directive", "Generating card:
        # wolves-of-yellowstone", etc. Set before entering an LLM-bounded
        # section, cleared when llm_active_count returns to zero.
        "activity_label": None,
        # Populated when a run finishes. Tells the UI what the most recent
        # visit actually produced — often zero cards (all topics fresh, no
        # directive lines yet), which without this field is indistinguishable
        # from "the system is broken" to anyone watching the page.
        "last_outcome": None,  # {summary, detail, cards_written, at}
    }


def _llm_active_enter(contact_slug: str, label: str | None = None) -> None:
    st = _status.setdefault(contact_slug, _empty_status())
    st["llm_active_count"] = st.get("llm_active_count", 0) + 1
    st["llm_active"] = True
    if label:
        st["activity_label"] = label


def _llm_active_exit(contact_slug: str) -> None:
    st = _status.setdefault(contact_slug, _empty_status())
    n = max(0, st.get("llm_active_count", 0) - 1)
    st["llm_active_count"] = n
    st["llm_active"] = n > 0
    if n == 0:
        st["activity_label"] = None


# Per-run tallies, reset in _run_once and folded into last_outcome at the
# end. Split out from the public status dict so we can distinguish "zero
# because nothing fired yet" from "zero because every line was fresh" at
# summarize-time.
_run_tallies: dict[str, dict[str, int]] = {}


def _tally_reset(contact_slug: str) -> None:
    _run_tallies[contact_slug] = {
        "cards_written": 0,
        "lines_skipped_fresh": 0,
        "lines_timed_out": 0,
        "lines_model_skipped": 0,
        "lines_format_failed": 0,
        "lines_missing_fields": 0,
    }


def _tally_inc(contact_slug: str, key: str) -> None:
    t = _run_tallies.get(contact_slug)
    if t is not None:
        t[key] = t.get(key, 0) + 1


def _summarize_outcome(contact_slug: str, had_crystal: bool, had_lines: bool) -> dict:
    """Fold per-run tally + structural facts (crystal, lines) into a one-line
    outcome the UI can render as a status pip + tooltip. Summary values are
    stable identifiers the frontend switches on; detail is the human string.
    """
    t = _run_tallies.get(contact_slug) or {}
    cards = t.get("cards_written", 0)
    fresh = t.get("lines_skipped_fresh", 0)
    timeouts = t.get("lines_timed_out", 0)
    skipped = t.get("lines_model_skipped", 0)
    format_fail = t.get("lines_format_failed", 0)
    missing = t.get("lines_missing_fields", 0)
    at = _now().isoformat()

    if not had_crystal:
        return {
            "summary": "cold_start",
            "detail": (
                f"No crystal yet — ran one broad curiosity card. "
                f"Engage with a few cards (thumbs, clicks) and a real feed directive forms."
            ),
            "cards_written": cards,
            "at": at,
        }
    if not had_lines:
        return {
            "summary": "no_directives",
            "detail": (
                "The crystal has no directive lines. Keep engaging — the next "
                "crystal regen will derive them from your signals."
            ),
            "cards_written": cards,
            "at": at,
        }
    if cards > 0:
        plural = "s" if cards != 1 else ""
        return {
            "summary": "generated",
            "detail": f"Wrote {cards} new card{plural}.",
            "cards_written": cards,
            "at": at,
        }
    # No cards written despite having a crystal + lines. Narrate why.
    total_skipped_active = timeouts + skipped + format_fail + missing
    if fresh > 0 and total_skipped_active == 0:
        plural = "s" if fresh != 1 else ""
        return {
            "summary": "all_fresh",
            "detail": (
                f"All {fresh} directive line{plural} already have cards "
                f"newer than their freshness window — nothing needed generating."
            ),
            "cards_written": 0,
            "at": at,
        }
    reasons = []
    if fresh: reasons.append(f"{fresh} already-fresh")
    if timeouts: reasons.append(f"{timeouts} timed out")
    if skipped: reasons.append(f"{skipped} model-skipped")
    if format_fail: reasons.append(f"{format_fail} format-failed")
    if missing: reasons.append(f"{missing} missing title/body")
    return {
        "summary": "no_cards",
        "detail": (
            f"Ran, but no cards were written ({', '.join(reasons) or 'unknown reason'})."
        ),
        "cards_written": 0,
        "at": at,
    }


# Per-contact single-flight locks. Myra's feed fire shouldn't block Bob's —
# each contact gets its own asyncio.Lock, minted lazily on first use.
_run_locks: dict[str, asyncio.Lock] = {}

# Per-contact UI status. Read by /v1/feed/status for the "generating…"
# indicator, written atomically inside the matching lock.
_status: dict[str, dict[str, Any]] = {}

# Per-contact visit-debounce state. Each call to mark_visit(slug) may
# schedule a fire, but only if enough time has passed since that contact's
# last one.
_last_fire_at: dict[str, float] = {}  # monotonic seconds, keyed by slug
_pending_visits: dict[str, asyncio.Task] = {}


def _lock_for(contact_slug: str) -> asyncio.Lock:
    lock = _run_locks.get(contact_slug)
    if lock is None:
        lock = asyncio.Lock()
        _run_locks[contact_slug] = lock
    return lock


def _now() -> datetime:
    return datetime.now(timezone.utc)


def current_status(contact_slug: str) -> dict:
    """Snapshot for the /v1/feed/status endpoint, scoped to one contact."""
    return dict(_status.get(contact_slug) or _empty_status())


def _set_status(contact_slug: str, **kwargs) -> None:
    st = _status.setdefault(contact_slug, _empty_status())
    st.update(kwargs)


# ── Visit debouncer ──────────────────────────────────────────────────────


async def mark_visit(contact_slug: str) -> dict:
    """Called when the dashboard loads. Schedules a fire if cooldown allows.

    Each contact has its own debouncer, lock, and pending-fire task — a
    visit from Bob never blocks Myra's feed from firing.

    Returns a small dict describing what happened — the UI doesn't strictly
    need it, but it's useful for debugging without watching server logs.
    """
    elapsed = time.monotonic() - _last_fire_at.get(contact_slug, 0.0)
    debounce = settings.feed_loop_visit_debounce_seconds
    if elapsed < debounce:
        return {"scheduled": False, "reason": f"debounced({int(elapsed)}s/{debounce}s)"}
    if _lock_for(contact_slug).locked():
        return {"scheduled": False, "reason": "already-running"}
    pending = _pending_visits.get(contact_slug)
    if pending and not pending.done():
        return {"scheduled": False, "reason": "already-pending"}
    _pending_visits[contact_slug] = asyncio.create_task(
        _run_once(contact_slug, reason="visit")
    )
    return {"scheduled": True}


async def force_fire(contact_slug: str, reason: str = "manual") -> dict:
    """Fire the loop immediately, skipping the visit-debounce cooldown.

    Used by `POST /v1/feed/refresh` (the existing manual-kick endpoint).
    Still respects this contact's single-flight lock.
    """
    if _lock_for(contact_slug).locked():
        return {"fired": False, "reason": "already-running"}
    asyncio.create_task(_run_once(contact_slug, reason=reason))
    return {"fired": True}


# ── The loop itself ──────────────────────────────────────────────────────


async def _run_once(contact_slug: str, reason: str = "unspecified") -> None:
    lock = _lock_for(contact_slug)
    if lock.locked():
        return
    async with lock:
        _last_fire_at[contact_slug] = time.monotonic()
        started = _now().isoformat()
        _tally_reset(contact_slug)
        _set_status(
            contact_slug,
            generating=True,
            started_at=started,
            finished_at=None,
            lines_total=0,
            lines_done=0,
            last_reason=reason,
        )
        # Structural facts captured by _do_run via closure so the summary
        # knows whether the loop actually had a crystal or directive lines
        # to work with.
        run_facts = {"had_crystal": False, "had_lines": False}
        try:
            await _do_run(contact_slug, reason, run_facts)
        except Exception:
            log.exception("feed_loop: run failed (contact=%s)", contact_slug)
        finally:
            outcome = _summarize_outcome(contact_slug, run_facts["had_crystal"], run_facts["had_lines"])
            _set_status(contact_slug, generating=False, finished_at=_now().isoformat(), last_outcome=outcome)


async def _do_run(contact_slug: str, reason: str, run_facts: dict) -> None:
    # Wake-gate the crystal. The predicate checks drift, confidence, and
    # the cold-start min-signal guard — see api/feed_crystal.should_regen.
    try:
        should, why = await feed_crystal.should_regen(contact_slug)
    except Exception:
        print(
            f"feed_loop[{contact_slug}]: should_regen check failed; proceeding without regen",
            flush=True,
        )
        should, why = False, "predicate-error"
    print(
        f"feed_loop[{contact_slug}]: wake reason={reason}, regen-decision={should} ({why})",
        flush=True,
    )
    if should:
        _llm_active_enter(contact_slug, label="Rederiving feed directive from engagement")
        try:
            await feed_crystal.synthesize(contact_slug)
        except Exception as e:
            print(
                f"feed_loop[{contact_slug}]: crystal synthesize failed: {type(e).__name__}: {e}; using stale crystal",
                flush=True,
            )
        finally:
            _llm_active_exit(contact_slug)

    crystal = await feed_crystal.latest(contact_slug, force=True)
    if not crystal:
        # Cold-start path — no crystal yet, no signal yet either. Run a
        # broadly-curious single fire so the lake gets some sediment we
        # can later distill from.
        print(f"feed_loop[{contact_slug}]: cold-start path (no crystal)", flush=True)
        await _cold_start_fire(contact_slug)
        return

    run_facts["had_crystal"] = True
    lines = crystal.get("directive_lines") or []
    if not lines:
        print(f"feed_loop[{contact_slug}]: crystal has no directive lines; skipping", flush=True)
        return
    run_facts["had_lines"] = True

    print(
        f"feed_loop[{contact_slug}]: crystal id={crystal.get('id')}, {len(lines)} directive line(s)",
        flush=True,
    )
    _set_status(contact_slug, lines_total=len(lines), lines_done=0)
    for i, line in enumerate(lines):
        try:
            await _fire_line(contact_slug, line, crystal)
        except Exception as e:
            print(
                f"feed_loop[{contact_slug}]: line {line.get('id')} failed: {type(e).__name__}: {e}",
                flush=True,
            )
        _set_status(contact_slug, lines_done=i + 1)
    print(f"feed_loop[{contact_slug}]: run complete ({len(lines)} lines processed)", flush=True)


_CARD_OUTPUT_INSTRUCTIONS = (
    "Respond with ONLY a JSON object — no markdown fences, no commentary.\n"
    "Schema:\n"
    "  {\n"
    '    "title": string                       // one-sentence headline (≤120 chars)\n'
    '    "body":  string                       // 2-4 sentences of plain prose\n'
    '    "tail":  string?                      // ≤8 words. Source citation, timestamp, stat, or next step. SKIP if you have nothing concrete — empty is better than restating the title.\n'
    '    "body_image": string?                 // media_hash or URL\n'
    '    "body_image_layout": "hero" | "thumb" // default "hero"\n'
    '    "media": string[]?                    // additional images\n'
    '    "link": string?                       // primary source URL — must start with http(s)\n'
    '    "links": [{title: string, url: string}]?  // additional related links (bundling)\n'
    "  }\n"
    "\n"
    "IMAGES — if the deltas you searched contain images (a media_hash on a "
    "delta, or markdown image URLs like ![](https://…)), include the strongest "
    "one in body_image and any extras in media. A weather card without weather "
    "imagery, a science card without a diagram, an RSS post without its photo — "
    "these are broken cards. The reader came for the picture as much as the prose.\n"
    "\n"
    "LINKS — if a candidate is marked 🔗[link=…], include that URL in `link`. The "
    "RSS source plugin appends `[Source](url)` to every item, so the link is the "
    "canonical article. If you bundled multiple candidates, the strongest goes in "
    "`link` and the rest go in `links` with short descriptive titles. A card without "
    "a link is a card without provenance — always include one when the candidate has "
    "it. Copy the URL exactly; do not paraphrase or invent.\n"
    "\n"
    "BUNDLING — if your search returns several deltas on the same topic or moment, "
    "you can compose ONE card that synthesizes across them. Pick the single strongest "
    "image for body_image, gather other notable images into media, the canonical link "
    "in `link`, the rest in `links`, and let the body reference what they have in "
    "common. Better one rich card than three thin ones.\n"
    "\n"
    "If you genuinely cannot satisfy the slot (no real answer exists, or a "
    'SKIP rule fires), respond with `{"skip": true, "reason": "<short>"}` instead.\n'
)


async def _cold_start_fire(contact_slug: str) -> None:
    """One broad-strokes card when there's no crystal and no engagement yet."""
    directive = (
        f"There's no feed-orient crystal yet — this reader ({contact_slug}) has "
        "not given any signal about what they want in their feed. Pick ONE "
        "genuinely interesting thing happening in the world right now "
        "(curiosity-default), search the web or the lake for an authoritative "
        "source, and produce a single feed card.\n\n"
        + _CARD_OUTPUT_INSTRUCTIONS
    )
    _llm_active_enter(contact_slug, label="First card — picking something curious")
    try:
        await asyncio.wait_for(
            _produce_card(contact_slug, line=None, crystal=None, directive=directive),
            timeout=settings.feed_loop_budget_seconds,
        )
    except asyncio.TimeoutError:
        log.info("feed_loop: cold-start fire timed out (contact=%s)", contact_slug)
    finally:
        _llm_active_exit(contact_slug)


async def _fetch_line_candidates(line: dict, limit: int = 20) -> list[dict]:
    """Pre-fetch a candidate pool for a directive line.

    The model's semantic-search-on-topic was missing relevant content
    (e.g. searching "clever science humor" doesn't surface a Quanta
    article titled "Wonder All Around Us"). Pulling candidates by tag
    + recency + image-bearing tells the model "here are concrete deltas
    that fit this slot — pick from them, don't go fishing."

    Strategies, deduplicated and merged:
      1. Engagement-anchored: deltas tagged `topic:<line_topic>` (the
         crystal's own taxonomy, present once engagement has built up).
      2. Visually-rich recents: rss + browser-extension deltas with
         media_hash or inline markdown images.
      3. Topic semantic search via the lake's /search endpoint.

    Returns newest-first, capped at `limit`.
    """
    topic = (line.get("topic") or "").strip()
    line_id = (line.get("id") or "").strip()
    seen: set[str] = set()
    pool: list[dict] = []

    def _add(d: dict) -> None:
        did = d.get("id")
        if did and did not in seen:
            seen.add(did)
            pool.append(d)

    # 1. Topic-tagged content
    if topic:
        try:
            for d in await delta_client.query(tags_include=[f"topic:{topic}"], limit=limit):
                _add(d)
        except Exception:
            pass

    # 2. Visually-rich recent deltas (rss + browser-extension).
    # The rss source plugin creates two delta families per item: the rich
    # digest delta (source like `rss/<source-id>`, content 1000+ chars with
    # markdown image AND `[Source](url)` link) AND a thin upload-sidecar
    # delta (source=`rss`, content="<title>" only, ≤100 chars). Both share
    # the media_hash; both are tagged `rss`+`feed`. Only the digest carries
    # `feed:<domain>` — that's our reliable filter.
    # Limit is large because the sidecar uploads dominate recency — each
    # poll cycle creates ~30 sidecars per source, all with fresh
    # write-time timestamps, while the rich digest deltas use the
    # article's pubDate (often yesterday or older). 1000 is enough to
    # reach a few days of digests on a modest-volume install.
    try:
        rss_results = await delta_client.query(tags_include=["rss"], limit=1000)
        for d in rss_results:
            tags = d.get("tags") or []
            content = d.get("content") or ""
            # Keep only digest deltas: distinguished by a `feed:<domain>` tag.
            # The sidecar uploads only carry the bare `feed` tag.
            has_feed_domain = any(isinstance(t, str) and t.startswith("feed:") for t in tags)
            if not has_feed_domain:
                continue
            has_image = bool(d.get("media_hash")) or "![" in content
            if not has_image:
                continue
            _add(d)
    except Exception:
        pass

    # Browser-extension deltas (Reddit captures, etc.) don't have the
    # sidecar problem — keep the original simple shape.
    try:
        for d in await delta_client.query(tags_include=["browser-extension"], limit=15):
            content = d.get("content") or ""
            has_image = bool(d.get("media_hash")) or "![" in content
            if has_image:
                _add(d)
    except Exception:
        pass

    # 3. Semantic search on the topic + line keywords. Catches near-misses
    # (e.g. line "physics-breakthroughs" surfacing Quanta articles).
    if topic or line_id:
        query = f"{topic} {line_id}".replace("-", " ").strip()
        try:
            res = await delta_client.search(query=query, limit=limit)
            results = res.get("results") if isinstance(res, dict) else None
            if results:
                for d in results:
                    _add(d)
        except Exception:
            pass

    pool.sort(key=lambda d: d.get("timestamp") or "", reverse=True)
    return pool[:limit]


_MARKDOWN_IMG_RE = re.compile(r'!\[[^\]]*\]\((https?://[^\s)]+)\)')
# Match a markdown link `[label](url)` that is NOT preceded by `!` (which
# would make it an image). The negative lookbehind keeps image markdown
# from getting double-counted in the link extractor.
_MARKDOWN_LINK_RE = re.compile(r'(?<!!)\[([^\]]+)\]\((https?://[^\s)]+)\)')


def _extract_external_url(content: str) -> str | None:
    """First http(s) markdown image URL in the content, if any.

    Preferred over media_hash for body_image because <img> tags can't pass
    the Authorization header that /v1/media/{hash} currently requires —
    pre-existing auth issue on the media route. External URLs render
    natively in the browser without auth.
    """
    if not content:
        return None
    m = _MARKDOWN_IMG_RE.search(content)
    return m.group(1) if m else None


def _extract_source_link(content: str) -> str | None:
    """First markdown link in the content. The RSS source plugin appends
    `[Source](url)` to every item, so this is usually the canonical article
    URL. Other sources may use other labels — the link is the link.
    """
    if not content:
        return None
    m = _MARKDOWN_LINK_RE.search(content)
    return m.group(2) if m else None


def _format_candidates(pool: list[dict]) -> str:
    """Compact candidate listing for the per-line directive."""
    if not pool:
        return "(no candidates pre-fetched — fall back to the search tools)"
    lines = []
    for d in pool[:20]:
        ts = (d.get("timestamp") or "")[:16]
        src = (d.get("source") or "?")[:24]
        did = (d.get("id") or "")[:12]
        media_hash = d.get("media_hash") or ""
        content = (d.get("content") or "").strip().split("\n", 1)[0][:140]
        # Surface BOTH external URLs and media_hashes when present, with
        # external URLs preferred (the model is told this in the directive).
        # External URLs work directly in <img> tags; in-lake hashes currently
        # don't render because the /v1/media route requires auth that <img>
        # can't pass. Until that's fixed, lean on URLs.
        ext_url = _extract_external_url(d.get("content") or "")
        source_link = _extract_source_link(d.get("content") or "")
        # URLs are NOT truncated — the model needs the full string to copy
        # exactly. A truncated URL is worse than no URL: the model assumes
        # what it sees is complete and ships a broken image / dead link.
        marks = []
        if ext_url:
            marks.append(f"🖼[url={ext_url}]")
        if media_hash:
            marks.append(f"📷[hash={media_hash}]")
        if source_link:
            marks.append(f"🔗[link={source_link}]")
        mark = " ".join(marks) if marks else "  "
        lines.append(f"  {mark} [{ts}] {src:24s} ({did}) {content}")
    return "\n".join(lines)


async def _fire_line(contact_slug: str, line: dict, crystal: dict) -> None:
    """One directive line → one feed card (subject to freshness check)."""
    line_id = (line.get("id") or "").strip() or "unnamed"
    topic = (line.get("topic") or "").strip()
    freshness_h = float(line.get("freshness_hours") or 12)

    # Freshness check — skip if this contact already has a card for this
    # line that's newer than the freshness window.
    if await _has_fresh_card(contact_slug, line_id, freshness_h):
        print(
            f"feed_loop[{contact_slug}]: line {line_id} skipped (fresh card exists, window={freshness_h}h)",
            flush=True,
        )
        _tally_inc(contact_slug, "lines_skipped_fresh")
        return
    print(
        f"feed_loop[{contact_slug}]: line {line_id} firing (topic={line.get('topic')}, weight={line.get('weight')})",
        flush=True,
    )

    # Pre-fetch candidates so the model isn't betting on semantic-search
    # to surface the right content. See _fetch_line_candidates.
    candidates = await _fetch_line_candidates(line, limit=20)
    print(f"feed_loop: line {line_id} candidates pre-fetched: {len(candidates)}", flush=True)
    candidates_block = _format_candidates(candidates)

    skip_if = (line.get("skip_if") or "").strip()
    skip_clause = f"\nSKIP CONDITION: {skip_if}" if skip_if else ""
    skip_rules = crystal.get("skip_rules") or []
    skip_block = ("\nGENERAL SKIP RULES:\n  - " + "\n  - ".join(skip_rules)) if skip_rules else ""

    directive = (
        f"You are filling one slot in Myra's feed.\n\n"
        f"OVERALL FEED ORIENTATION (from the crystal):\n{crystal.get('narrative') or '(none)'}\n\n"
        f"THIS SLOT:\n"
        f"  id:      {line_id}\n"
        f"  topic:   {topic or '(none)'}\n"
        f"  weight:  {line.get('weight') or 'unspecified'}\n"
        f"  freshness window: {freshness_h}h"
        f"{skip_clause}{skip_block}\n\n"
        f"=== CANDIDATES FROM THE LAKE (pre-fetched, sorted newest first) ===\n"
        f"{candidates_block}\n\n"
        f"Pick the strongest candidate (or two related ones — see BUNDLING) and "
        f"write the card. Image preference, in order:\n"
        f"  1. PREFER 🖼[url=…] — external URLs render directly in <img> tags. Copy the "
        f"URL exactly into body_image.\n"
        f"  2. Use 📷[hash=…] only if no URL is available — copy the hash EXACTLY (16 "
        f"hex chars, no truncation, no paraphrasing).\n"
        f"For links: 🔗[link=…] — copy that URL into the `link` field exactly. If you "
        f"bundled multiple candidates, the strongest goes in `link` and the rest in "
        f"`links`. Cards without a link feel orphaned; always include one when any "
        f"candidate has it.\n"
        f"If you make up a hash or URL, the validation pass drops it and the card ships "
        f"image-less or link-less. If the candidates don't fit, you can still call the "
        f"search tools — but candidates are the cheap path and usually contain what "
        f"you need.\n\n"
        + _CARD_OUTPUT_INSTRUCTIONS
    )

    label_topic = topic or line_id
    _llm_active_enter(contact_slug, label=f"Generating card: {label_topic}")
    try:
        await asyncio.wait_for(
            _produce_card(
                contact_slug,
                line=line,
                crystal=crystal,
                directive=directive,
                candidates=candidates,
            ),
            timeout=settings.feed_loop_budget_seconds,
        )
    except asyncio.TimeoutError:
        print(f"feed_loop[{contact_slug}]: line {line_id} timed out", flush=True)
        _tally_inc(contact_slug, "lines_timed_out")
    finally:
        _llm_active_exit(contact_slug)


def _strip_fences(text: str) -> str:
    import re
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


# How many parse attempts before we give up on a line. The first attempt
# does the real work (search, write); retries are cheap re-format nudges.
MAX_FORMAT_ATTEMPTS = 3


def _parse_card_payload(text: str) -> dict | None:
    """Try to parse a card payload out of the assistant's final message.

    Returns the parsed dict on success, None if it isn't valid JSON.
    Skip-payloads (`{"skip": true, ...}`) round-trip as-is so the caller
    can distinguish "model deliberately skipped" from "model produced
    garbage."
    """
    raw = _strip_fences(text or "")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _candidate_hashes(pool: list[dict] | None) -> set[str]:
    """Real media_hash values from the pre-fetched candidates.

    Used to drop hallucinated hashes from the model's output — flash models
    sometimes invent plausible-looking hex strings that aren't in the lake.
    """
    out: set[str] = set()
    for d in pool or []:
        h = (d.get("media_hash") or "").strip()
        if h:
            out.add(h)
    return out


def _validate_body_image(value: str, valid_hashes: set[str]) -> str:
    """Keep value if it's a real URL or a known media_hash; else drop."""
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("http://") or v.startswith("https://"):
        return v
    if v in valid_hashes:
        return v
    # Looks like a hash but isn't in the candidate pool — hallucination.
    return ""


def _validate_media_list(values: list[str], valid_hashes: set[str]) -> list[str]:
    """Same validation, applied across the media[] array."""
    out: list[str] = []
    for v in values or []:
        kept = _validate_body_image(str(v), valid_hashes)
        if kept:
            out.append(kept)
    return out


async def _produce_card(
    contact_slug: str,
    line: dict | None,
    crystal: dict | None,
    directive: str,
    candidates: list[dict] | None = None,
) -> None:
    """Run fathom_think; parse the JSON-shaped final assistant message; write a card.

    Retries on non-JSON output up to MAX_FORMAT_ATTEMPTS, each time feeding
    the previous garbled output back to the model with a louder format
    nudge. The whole call is still bounded by the slot's wall-clock budget
    (`asyncio.wait_for` in the caller) — retries don't get bonus time.

    `candidates` is the pre-fetched pool used to validate body_image and
    media values — drops any hash the model invented that isn't in the lake.
    """
    from .server import fathom_think  # lazy — avoid circular import
    line_id = (line or {}).get("id") or "(cold-start)"

    user_message = "Produce the card for the slot described above."
    last_failed_excerpt: str | None = None
    payload: dict | None = None

    for attempt in range(1, MAX_FORMAT_ATTEMPTS + 1):
        # On retries, prepend a stronger format-correction nudge that
        # quotes the previous failed output so the model sees what it did.
        if attempt > 1 and last_failed_excerpt is not None:
            nudge = (
                f"⚠ Your previous attempt was not valid JSON. The output started with:\n"
                f"---\n{last_failed_excerpt}\n---\n\n"
                f"Attempt {attempt} of {MAX_FORMAT_ATTEMPTS}. Respond with ONLY the "
                f"JSON object specified in the directive above. No prose, no markdown "
                f"fences, no commentary. Just the object."
            )
            this_message = nudge + "\n\n" + user_message
            # Skip the search/tool work on retries — the failed prior attempt
            # already had a chance. Tighten the round budget so a bad retry
            # can't burn more wall-clock than necessary.
            this_max_rounds = max(2, settings.feed_loop_budget_tool_calls // 3)
        else:
            this_message = user_message
            this_max_rounds = settings.feed_loop_budget_tool_calls

        messages = await fathom_think(
            user_message=this_message,
            directive=directive,
            recall=False,
            max_rounds=this_max_rounds,
        )
        last = messages[-1] if messages else {}
        text = (last.get("content") or "").strip()
        if not text:
            log.info("feed_loop: line %s attempt %d — empty final message", line_id, attempt)
            last_failed_excerpt = "(empty message)"
            continue

        candidate = _parse_card_payload(text)
        if candidate is None:
            print(f"feed_loop: line {line_id} attempt {attempt} — non-JSON; will retry. excerpt: {text[:200]!r}", flush=True)
            last_failed_excerpt = text[:240].replace("\n", " ")
            continue

        # Got valid JSON. Stop here even if the payload turns out to be
        # malformed-but-valid (e.g. missing fields) — that's a content
        # problem, not a format problem, and retrying won't help.
        payload = candidate
        if attempt > 1:
            print(f"feed_loop: line {line_id} recovered on attempt {attempt}", flush=True)
        break

    if payload is None:
        print(f"feed_loop: line {line_id} — gave up after {MAX_FORMAT_ATTEMPTS} attempts (lost cause)", flush=True)
        _tally_inc(contact_slug, "lines_format_failed")
        return
    if payload.get("skip"):
        print(f"feed_loop: line {line_id} — model skipped: {payload.get('reason')}", flush=True)
        _tally_inc(contact_slug, "lines_model_skipped")
        return
    if not payload.get("title") or not payload.get("body"):
        print(f"feed_loop: line {line_id} — JSON valid but missing title/body; skipping. payload keys: {list(payload.keys())}", flush=True)
        _tally_inc(contact_slug, "lines_missing_fields")
        return

    valid_hashes = _candidate_hashes(candidates)
    raw_body_image = str(payload.get("body_image", "") or "")
    body_image = _validate_body_image(raw_body_image, valid_hashes)
    if raw_body_image and not body_image:
        print(f"feed_loop: line {line_id} dropped hallucinated body_image={raw_body_image!r}", flush=True)
    raw_media = [str(m) for m in (payload.get("media") or []) if m]
    media = _validate_media_list(raw_media, valid_hashes)
    if len(raw_media) != len(media):
        print(f"feed_loop: line {line_id} dropped {len(raw_media) - len(media)} hallucinated media entr(ies)", flush=True)

    # Links: only http(s) URLs. The model could in principle invent a URL,
    # but unlike media_hash we can't validate against a candidate set —
    # links can legitimately come from web search. The http(s) shape is
    # the only floor we enforce; everything else is on the model.
    raw_link = str(payload.get("link", "") or "").strip()
    link = raw_link if raw_link.startswith(("http://", "https://")) else ""
    raw_links = payload.get("links") or []
    links: list[dict] = []
    for entry in raw_links:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url", "") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        title = str(entry.get("title", "") or "").strip()[:120]
        links.append({"title": title, "url": url})

    card = {
        "title": str(payload.get("title", ""))[:200],
        "body": str(payload.get("body", "")),
        "tail": str(payload.get("tail", "") or ""),
        "body_image": body_image,
        "body_image_layout": payload.get("body_image_layout") or "hero",
        "media": media,
        "link": link,
        "links": links,
    }
    tags = [
        CARD_TAG,
        "feed-story",  # back-compat with existing UI reader
        _contact_tag(contact_slug),
    ]
    if line and line.get("id"):
        tags.append(f"directive-line:{line['id']}")
    if line and line.get("topic"):
        tags.append(f"topic:{line['topic']}")
    if crystal and crystal.get("id"):
        tags.append(f"crystal:{crystal['id']}")
    try:
        await delta_client.write(
            content=json.dumps(card, ensure_ascii=False),
            tags=tags,
            source=CARD_SOURCE,
        )
        _tally_inc(contact_slug, "cards_written")
    except Exception:
        log.exception("feed_loop: card delta write failed")


async def _has_fresh_card(
    contact_slug: str, line_id: str, freshness_hours: float
) -> bool:
    """True if this contact already has a card for this line newer than the window."""
    try:
        results = await delta_client.query(
            tags_include=[CARD_TAG, f"directive-line:{line_id}", _contact_tag(contact_slug)],
            limit=1,
        )
    except Exception:
        return False
    if not results:
        return False
    ts = results[0].get("timestamp") or ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return False
    age = _now() - dt
    return age < timedelta(hours=freshness_hours)
