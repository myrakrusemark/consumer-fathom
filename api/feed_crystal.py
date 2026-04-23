"""Feed-orient crystal — Fathom's task-shaped model of "what to put in
Myra's feed right now."

Borrows the **mood pattern**, not the identity-crystal pattern: cheap
focused synthesis, in-process, no background poller. Wake-gated regen
runs on the same page-view event that fires the feed loop (in
api/feed_loop.py).

The crystal lives in the lake as a `crystal:feed-orient` delta. Latest
wins. Content is structured JSON so the confidence scorer has something
to check — see docs/feed-spec.md for the schema.

This module owns:
  • load / write the crystal
  • synthesize a fresh crystal from engagement + chat + recent cards
  • engagement-centroid drift (anchor sidecar mirrors crystal_anchor.py)
  • drift history sidecar
  • confidence scoring against the latest crystal
  • the regen predicate the wake-gate consults

It does NOT own:
  • the feed loop itself (that's feed_loop.py)
  • the page-view debouncer (also feed_loop.py)
  • UI rendering (ui/index.html)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import delta_client
from .prompt import FEED_CRYSTAL_DIRECTIVE
from .providers import llm
from .settings import settings

log = logging.getLogger(__name__)
# Same reason as feed_loop and chat_listener: uvicorn keeps app loggers at
# WARNING by default; pin to INFO so regen + confidence decisions show in
# the operational trail.
logging.getLogger(__name__).setLevel(logging.INFO)

# Tag conventions — the spec is the source of truth (docs/feed-spec.md).
CRYSTAL_TAG = "crystal:feed-orient"
ENGAGEMENT_TAG = "feed-engagement"
CARD_TAG = "feed-card"
CRYSTAL_SOURCE = "consumer-api"

# Brief in-memory cache so repeated reads inside one wake don't hammer the
# lake. The crystal changes on regen (minutes apart at fastest); 5 seconds
# is plenty short to feel live. Keyed by contact_slug so each contact has
# their own crystal cached independently.
_CACHE_TTL_SECONDS = 5.0
_cache: dict[str, dict | None] = {}
_cache_at: dict[str, float] = {}
_cache_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` fences if the model emitted them anyway."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


# ── Crystal load / write ─────────────────────────────────────────────────


def _contact_tag(contact_slug: str) -> str:
    return f"contact:{contact_slug}"


async def _fetch_latest_uncached(contact_slug: str) -> dict | None:
    """Find the most recent crystal:feed-orient delta for this contact."""
    tags = [CRYSTAL_TAG, _contact_tag(contact_slug)]
    results = await delta_client.query(tags_include=tags, limit=1)
    if not results:
        return None
    return _to_crystal(results[0])


def _to_crystal(delta: dict) -> dict:
    """Adapt a delta into the in-process crystal shape."""
    raw = delta.get("content") or ""
    try:
        parsed = json.loads(_strip_fences(raw))
        if not isinstance(parsed, dict):
            parsed = {"narrative": str(parsed), "directive_lines": [], "topic_weights": {}, "skip_rules": []}
    except Exception:
        parsed = {"narrative": raw, "directive_lines": [], "topic_weights": {}, "skip_rules": []}
    confidence = _confidence_from_tags(delta.get("tags") or [])
    return {
        "id": delta.get("id"),
        "created_at": delta.get("timestamp"),
        "source": delta.get("source"),
        "tags": delta.get("tags") or [],
        "confidence": confidence,
        "narrative": parsed.get("narrative") or "",
        "directive_lines": parsed.get("directive_lines") or [],
        "topic_weights": parsed.get("topic_weights") or {},
        "skip_rules": parsed.get("skip_rules") or [],
        "raw": parsed,
    }


def _confidence_from_tags(tags: list[str]) -> float | None:
    for t in tags:
        if isinstance(t, str) and t.startswith("confidence:"):
            try:
                return float(t.split(":", 1)[1])
            except (ValueError, IndexError):
                return None
    return None


async def latest(contact_slug: str, force: bool = False) -> dict | None:
    """Return the canonical current feed-orient crystal for this contact. Cache-gated."""
    now = time.monotonic()
    cached = _cache.get(contact_slug)
    cached_at = _cache_at.get(contact_slug, 0.0)
    if not force and cached is not None and (now - cached_at) < _CACHE_TTL_SECONDS:
        return cached
    async with _cache_lock:
        cached = _cache.get(contact_slug)
        cached_at = _cache_at.get(contact_slug, 0.0)
        if not force and cached is not None and (time.monotonic() - cached_at) < _CACHE_TTL_SECONDS:
            return cached
        fresh = await _fetch_latest_uncached(contact_slug)
        _cache[contact_slug] = fresh
        _cache_at[contact_slug] = time.monotonic()
    return fresh


def _invalidate_cache(contact_slug: str | None = None) -> None:
    if contact_slug is None:
        _cache.clear()
        _cache_at.clear()
    else:
        _cache.pop(contact_slug, None)
        _cache_at.pop(contact_slug, None)


async def _write_crystal(contact_slug: str, payload: dict, confidence: float | None) -> dict:
    """Write a crystal-feed-orient delta and snapshot the drift anchor."""
    tags = [CRYSTAL_TAG, _contact_tag(contact_slug)]
    if confidence is not None:
        tags.append(f"confidence:{round(confidence, 4)}")
    written = await delta_client.write(
        content=json.dumps(payload, ensure_ascii=False),
        tags=tags,
        source=CRYSTAL_SOURCE,
    )
    _invalidate_cache(contact_slug)
    # Snapshot the engagement-centroid as the new drift anchor — independent
    # of the crystal's own embedding, so a bad crystal can't self-trigger
    # a runaway regen (the 2026-04-19 lesson).
    try:
        await _snapshot_anchor(contact_slug, written.get("id"))
    except Exception:
        log.exception("feed_crystal: anchor snapshot failed (non-fatal)")
    return written


async def list_events(contact_slug: str, limit: int = 50) -> list[dict]:
    """Crystal regen events for the ECG card, scoped to this contact."""
    try:
        results = await delta_client.query(
            tags_include=[CRYSTAL_TAG, _contact_tag(contact_slug)],
            limit=limit,
        )
    except Exception:
        return []
    events = []
    for d in results:
        events.append({
            "id": d.get("id"),
            "timestamp": d.get("timestamp"),
            "confidence": _confidence_from_tags(d.get("tags") or []),
        })
    events.sort(key=lambda e: e.get("timestamp") or "")
    return events[-limit:]


# ── Synthesis ────────────────────────────────────────────────────────────


async def _fetch_engagements_since(
    contact_slug: str, since_iso: str | None, limit: int = 200
) -> list[dict]:
    """All feed-engagement deltas for this contact newer than the cutoff."""
    try:
        results = await delta_client.query(
            tags_include=[ENGAGEMENT_TAG, _contact_tag(contact_slug)],
            time_start=since_iso or "",
            limit=limit,
        )
    except Exception:
        log.exception("feed_crystal: engagement fetch failed")
        return []
    return results


async def _fetch_recent_cards(contact_slug: str, limit: int = 50) -> list[dict]:
    """Recent feed-card deltas for this contact — what's already been shown."""
    try:
        return await delta_client.query(
            tags_include=[CARD_TAG, _contact_tag(contact_slug)],
            limit=limit,
        )
    except Exception:
        return []


async def _fetch_lake_topic_summary(window_hours: int = 72) -> str:
    """A compact survey of what content is *available* to the loop right now.

    Without this, the crystal designs directive lines purely from Myra's
    engagement signals — which means new sources (NASA images, fresh RSS
    feeds, etc.) never make it into the rotation because she's never had
    a chance to engage with them. Including a recency snapshot lets the
    crystal propose lines that the loop can actually fulfill.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    try:
        all_recent = await delta_client.query(time_start=cutoff, limit=300)
    except Exception:
        return ""
    # Count by source and by image-presence so the crystal sees both volume
    # and visual richness signals.
    by_source: dict[str, dict] = {}
    for d in all_recent:
        src = d.get("source") or "?"
        bucket = by_source.setdefault(src, {"count": 0, "with_image": 0, "samples": []})
        bucket["count"] += 1
        if d.get("media_hash") or "![" in (d.get("content") or ""):
            bucket["with_image"] += 1
        if len(bucket["samples"]) < 2:
            content = (d.get("content") or "").strip().split("\n", 1)[0][:80]
            if content:
                bucket["samples"].append(content)
    if not by_source:
        return "(lake is quiet — nothing new in the last 72h)"
    lines = []
    for src, b in sorted(by_source.items(), key=lambda kv: -kv[1]["count"]):
        if b["count"] < 2:
            continue  # skip noise
        img_part = f", {b['with_image']} with images" if b['with_image'] else ""
        sample = " · ".join(b['samples'])
        lines.append(f"  {src}: {b['count']} deltas{img_part}  e.g.: {sample}")
    return "\n".join(lines) if lines else "(lake is quiet)"


def _format_engagements(deltas: list[dict]) -> str:
    """Compact one-line-per-engagement formatter for the synthesis prompt."""
    if not deltas:
        return "(no engagement deltas yet)"
    lines = []
    for d in deltas:
        try:
            payload = json.loads(d.get("content") or "{}")
        except Exception:
            payload = {}
        kind = payload.get("kind") or "?"
        topic = payload.get("topic") or ""
        excerpt = (payload.get("card_excerpt") or "")[:120]
        topic_str = f"[{topic}] " if topic else ""
        lines.append(f"  {kind:>5} · {topic_str}{excerpt}")
    return "\n".join(lines)


def _format_recent_cards(deltas: list[dict]) -> str:
    """One-line-per-card formatter so the model knows what was already shown."""
    if not deltas:
        return "(no cards shown yet)"
    lines = []
    for d in deltas[:30]:
        try:
            payload = json.loads(d.get("content") or "{}")
        except Exception:
            payload = {}
        title = payload.get("title") or payload.get("narrative") or (d.get("content") or "")[:80]
        topic = ""
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("topic:"):
                topic = t.split(":", 1)[1]
                break
        topic_str = f"[{topic}] " if topic else ""
        lines.append(f"  {topic_str}{title[:120]}")
    return "\n".join(lines)


async def _fetch_chat_engagements_since(
    contact_slug: str, since_iso: str | None, limit: int = 50
) -> str:
    """Chat-from-card user messages, formatted compactly. Empty if none.

    The chat-engagement deltas carry `chat:<session>` and we follow that to
    the user's first message in the session, which is the actual content.
    """
    engs = await _fetch_engagements_since(contact_slug, since_iso, limit=limit)
    sessions: list[str] = []
    for d in engs:
        # Chat-engagement deltas link back to the seeded chat session via
        # the `chat-from:<slug>` tag (NOT `chat:<slug>` — see api/server.py
        # write_feed_engagement for why).
        is_chat = any(t == "engagement:chat" for t in (d.get("tags") or []))
        if not is_chat:
            continue
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("chat-from:"):
                sessions.append(t.split(":", 1)[1])
                break
    if not sessions:
        return "(no chat-from-card sessions yet)"
    # Pull the first user message from each session. Best-effort.
    summaries: list[str] = []
    for slug in sessions[:limit]:
        try:
            msgs = await delta_client.query(
                tags_include=[f"chat:{slug}", "participant:user"],
                limit=3,
            )
        except Exception:
            continue
        if not msgs:
            continue
        # Lake returns newest-first; first message is the oldest.
        first = msgs[-1]
        text = (first.get("content") or "").strip()[:200]
        if text:
            summaries.append(f"  • {text}")
    return "\n".join(summaries) if summaries else "(no chat-from-card text yet)"


async def synthesize(contact_slug: str) -> dict | None:
    """Run a feed-orient crystal regeneration for this contact.

    Reads engagement + chat + recent cards + prior crystal, calls the LLM,
    writes the result. Returns the in-process crystal shape on success,
    None on failure.
    """
    prior = await _fetch_latest_uncached(contact_slug)
    cutoff = (prior or {}).get("created_at")

    engagements = await _fetch_engagements_since(contact_slug, cutoff, limit=200)
    chats = await _fetch_chat_engagements_since(contact_slug, cutoff, limit=50)
    cards = await _fetch_recent_cards(contact_slug, limit=50)
    lake_survey = await _fetch_lake_topic_summary(window_hours=72)

    parts: list[str] = []
    parts.append(f"=== Recent feed-engagement deltas (since {cutoff or 'beginning'}) ===\n{_format_engagements(engagements)}")
    parts.append(f"=== Chat-from-card user messages ===\n{chats}")
    parts.append(f"=== Recent feed cards already shown ===\n{_format_recent_cards(cards)}")
    parts.append(f"=== What's actually in the lake right now (last 72h, by source) ===\n{lake_survey}")
    if prior:
        parts.append(
            "=== Previous crystal (for continuity) ===\n"
            + json.dumps({
                "narrative": prior.get("narrative"),
                "directive_lines": prior.get("directive_lines"),
                "topic_weights": prior.get("topic_weights"),
                "skip_rules": prior.get("skip_rules"),
            }, ensure_ascii=False, indent=2)
        )
    else:
        parts.append("=== Previous crystal ===\n(none — this is the first regeneration)")

    user_message = "\n\n".join(parts)

    try:
        resp = await llm.chat.completions.create(
            model=settings.resolved_model,
            messages=[
                {"role": "system", "content": FEED_CRYSTAL_DIRECTIVE},
                {"role": "user", "content": user_message},
            ],
            temperature=0.4,
        )
    except Exception:
        log.exception("feed_crystal: LLM call failed")
        return None

    text = resp.choices[0].message.content if resp.choices else ""
    raw = _strip_fences(text or "")
    try:
        payload = json.loads(raw)
    except Exception:
        log.warning("feed_crystal: synthesis returned non-JSON; wrapping as narrative")
        payload = {
            "version": 1,
            "narrative": (text or "").strip()[:1000],
            "directive_lines": [],
            "topic_weights": {},
            "skip_rules": [],
        }

    # Compute confidence of the prior crystal NOW (before we overwrite it),
    # tagged on the fresh crystal we're about to write so the ECG can show
    # how the previous regen actually performed.
    prior_confidence = (
        await score_confidence(contact_slug, prior, engagements) if prior else None
    )

    written = await _write_crystal(contact_slug, payload, confidence=prior_confidence)

    # Re-fetch to return the canonical in-process shape
    fresh = await latest(contact_slug, force=True)
    if fresh:
        log.info(
            "feed_crystal: regen accepted (contact=%s, id=%s, prior_confidence=%s, lines=%d)",
            contact_slug,
            written.get("id"),
            prior_confidence,
            len(fresh.get("directive_lines") or []),
        )
    return fresh


# ── Confidence scorer ────────────────────────────────────────────────────


def _engagement_sign(kind: str) -> int:
    """+1 for positive engagement, -1 for negative."""
    if kind in ("more", "chat"):
        return 1
    if kind == "less":
        return -1
    return 0


def _engagement_recency_weight(engagement_ts: str | None) -> float:
    """Exponential decay weight for an engagement based on its age.

    Returns a value in (0, 1]. A fresh engagement is ~1.0; an engagement
    one half-life old is 0.5; very old engagements asymptote to near zero.
    Silently returns 1.0 on parse failure so a malformed timestamp doesn't
    erase the signal.
    """
    if not engagement_ts:
        return 1.0
    try:
        dt = datetime.fromisoformat(engagement_ts.replace("Z", "+00:00"))
    except Exception:
        return 1.0
    age_seconds = max(0.0, (_now() - dt).total_seconds())
    half_life = max(1.0, float(settings.feed_engagement_half_life_seconds))
    return math.pow(0.5, age_seconds / half_life)


async def score_confidence(
    contact_slug: str,
    crystal: dict | None,
    engagements: list[dict] | None = None,
) -> float | None:
    """Confidence = how well the crystal predicted recent engagement.

    For each engagement delta after the crystal was written:
      • Look up its topic on the engagement delta itself (no card join needed).
      • Look up topic_weights[topic] on the crystal (the crystal's prediction).
      • Weight the contribution by recency — a week-old hit matters less
        than one from yesterday (exponential decay on engagement age).
      • Engagement matches prediction → hit. Mismatches → miss.
      • Topic with weight ~0 is indeterminate (doesn't count either way).

    Confidence = (hits + 1) / (hits + misses + 2)  -- Laplace smoothing,
    where hits/misses are the recency-weighted sums (still real numbers,
    still bounded by 0 ≤ hits + misses ≤ count of signal engagements).

    Returns None if there's no crystal or engagements is empty AND not loaded
    (we still return a smoothed score when loaded-but-empty so the ECG has a
    point to plot).
    """
    if not crystal:
        return None
    weights = crystal.get("topic_weights") or {}
    if engagements is None:
        engagements = await _fetch_engagements_since(
            contact_slug, crystal.get("created_at")
        )
    hits = 0.0
    misses = 0.0
    for d in engagements:
        try:
            payload = json.loads(d.get("content") or "{}")
        except Exception:
            payload = {}
        topic = payload.get("topic") or ""
        kind = payload.get("kind") or ""
        sign = _engagement_sign(kind)
        if not sign:
            continue
        weight = weights.get(topic, 0.0)
        if abs(weight) < 0.05:
            continue  # indeterminate prediction — skip
        recency = _engagement_recency_weight(d.get("timestamp"))
        if (weight > 0 and sign > 0) or (weight < 0 and sign < 0):
            hits += recency
        else:
            misses += recency
    return round((hits + 1) / (hits + misses + 2), 4)


# ── Drift anchor (engagement subset) ─────────────────────────────────────


def _anchor_path(contact_slug: str) -> Path:
    return (
        Path(settings.mood_state_path).parent
        / f"feed-crystal-anchor.{contact_slug}.json"
    )


def _drift_history_path(contact_slug: str) -> Path:
    return (
        Path(settings.mood_state_path).parent
        / f"feed-drift-history.{contact_slug}.json"
    )


def _atomic_write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".feed-anchor-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


async def _engagement_centroid(contact_slug: str) -> dict:
    """Fetch the centroid over this contact's feed-engagement deltas only."""
    return await delta_client.centroid(
        tags_include=[ENGAGEMENT_TAG, _contact_tag(contact_slug)]
    )


async def _snapshot_anchor(contact_slug: str, crystal_id: str | None) -> None:
    """Save the engagement-centroid at this moment as the drift anchor."""
    try:
        c = await _engagement_centroid(contact_slug)
    except Exception:
        log.exception("feed_crystal: anchor centroid fetch failed")
        return
    vec = c.get("centroid")
    if not vec:
        # No engagement deltas yet, or all unembedded. Don't save an empty anchor.
        return
    record = {
        "contact_slug": contact_slug,
        "crystal_id": crystal_id,
        "centroid": list(vec),
        "dim": len(vec),
        "saved_at": _now_iso(),
    }
    _atomic_write(_anchor_path(contact_slug), record)


def load_anchor(contact_slug: str) -> dict | None:
    p = _anchor_path(contact_slug)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    if not data.get("centroid"):
        return None
    return data


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cos(a, b) for two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    import math
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return float(1.0 - dot / (math.sqrt(na) * math.sqrt(nb)))


async def sample_drift(contact_slug: str) -> dict:
    """Sample current engagement-centroid drift against the anchor.

    Appends to the drift-history sidecar for the ECG. Returns the snapshot.
    """
    anchor = load_anchor(contact_slug)
    snapshot: dict
    if not anchor:
        snapshot = {"drift": 0.0, "no_anchor": True}
    else:
        try:
            c = await _engagement_centroid(contact_slug)
            vec = c.get("centroid")
            total = int(c.get("total_deltas") or 0)
            if not vec:
                snapshot = {"drift": 0.0, "total_deltas": total, "empty_subset": True}
            else:
                d = _cosine_distance(anchor["centroid"], vec)
                snapshot = {"drift": round(d, 4), "total_deltas": total}
        except Exception:
            log.exception("feed_crystal: drift sample failed")
            snapshot = {"drift": 0.0, "error": True}

    now_iso = _now_iso()
    entry = {"t": now_iso, "v": float(snapshot.get("drift", 0.0))}
    p = _drift_history_path(contact_slug)
    try:
        if p.exists():
            state = json.loads(p.read_text())
        else:
            state = {"history": []}
    except Exception:
        state = {"history": []}
    history = state.get("history") or []
    history.append(entry)
    if len(history) > 1000:
        history = history[-1000:]
    state["history"] = history
    try:
        _atomic_write(p, state)
    except Exception:
        log.exception("feed_crystal: drift history write failed")

    return {**snapshot, "sampled_at": now_iso}


def drift_history(contact_slug: str, since_seconds: int | None = None) -> list[dict]:
    p = _drift_history_path(contact_slug)
    if not p.exists():
        return []
    try:
        state = json.loads(p.read_text())
    except Exception:
        return []
    items = state.get("history") or []
    if since_seconds is None:
        return items
    cutoff = _now().timestamp() - since_seconds
    out: list[dict] = []
    for entry in items:
        try:
            ts = datetime.fromisoformat(entry["t"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ts.timestamp() >= cutoff:
            out.append(entry)
    return out


# ── Regen predicate ──────────────────────────────────────────────────────


async def should_regen(contact_slug: str) -> tuple[bool, str]:
    """Decide whether the wake should regen this contact's feed-orient crystal.

    Predicate (drift OR confidence) AND (cooldown) AND (min_signal).

    Returns (decision, reason). Reason is for logging/telemetry.
    """
    crystal = await latest(contact_slug)

    # Bootstrap case — no crystal yet. Defer to min_signal check below.
    if not crystal:
        engagements = await _fetch_engagements_since(
            contact_slug, None, limit=settings.feed_min_signal_engagements + 1
        )
        if len(engagements) < settings.feed_min_signal_engagements:
            return False, f"bootstrap:not-enough-signal({len(engagements)}/{settings.feed_min_signal_engagements})"
        return True, f"bootstrap:enough-signal({len(engagements)})"

    # Cooldown — don't regen too often even if signals say so.
    cooldown_s = settings.feed_crystal_cooldown_seconds
    created_at = crystal.get("created_at") or ""
    if created_at:
        try:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            elapsed = (_now() - ts).total_seconds()
            if elapsed < cooldown_s:
                return False, f"cooldown({int(elapsed)}s/{cooldown_s}s)"
        except Exception:
            pass

    # Min-signal guard — the cold-start fail-open lesson from auto_regen.py.
    # If no engagement has accumulated since last regen, the crystal can't
    # be wrong about anything yet. Fail open (skip), don't fire blindly.
    new_engagements = await _fetch_engagements_since(
        contact_slug, created_at, limit=settings.feed_min_signal_engagements + 1
    )
    if len(new_engagements) < settings.feed_min_signal_engagements:
        return False, f"not-enough-new-signal({len(new_engagements)}/{settings.feed_min_signal_engagements})"

    # Drift — the lake of engagement has shifted away from the anchor.
    drift_snapshot = await sample_drift(contact_slug)
    drift_value = float(drift_snapshot.get("drift") or 0.0)
    if drift_value > settings.feed_drift_threshold:
        return True, f"drift({drift_value:.3f}>{settings.feed_drift_threshold})"

    # Confidence — recent predictions don't match observed engagement.
    confidence = await score_confidence(contact_slug, crystal, new_engagements)
    if confidence is not None and confidence < settings.feed_confidence_floor:
        return True, f"low-confidence({confidence:.3f}<{settings.feed_confidence_floor})"

    return False, f"stable(drift={drift_value:.3f},conf={confidence})"
