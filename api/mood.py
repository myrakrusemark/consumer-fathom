"""Mood layer — the "carrier wave" between identity crystal and the lake.

Synthesis runs wake-gated: pressure accumulates passively, and the next chat
request decides whether to regenerate the mood. The mood delta is then
prepended to the system prompt alongside the crystal.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from . import delta_client, pressure, search as search_module
from .prompt import MOOD_DIRECTIVE
from .providers import llm
from .settings import settings

log = logging.getLogger(__name__)

MOOD_TAGS = ["mood-delta", "carrier-wave"]
MOOD_SOURCE = "fathom-mood"
RECENT_ACTIVITY_LIMIT = 50

_STATE_RE = re.compile(r"[^a-z]")


def _sanitize_state(raw: str) -> str:
    """Lowercase, strip non-letters; cap at 24 chars. Empty -> 'unset'."""
    cleaned = _STATE_RE.sub("", (raw or "").lower())[:24]
    return cleaned or "unset"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` fences if the model emitted them anyway."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_mood_payload(text: str) -> dict:
    """Parse the LLM's mood JSON. Falls back to wrapping raw text on parse failure."""
    raw = _strip_fences(text)
    try:
        obj = json.loads(raw)
        carrier = (obj.get("carrier_wave") or "").strip()
        threads = obj.get("threads") or []
        if not isinstance(threads, list):
            threads = []
        threads = [str(t).strip() for t in threads if str(t).strip()][:4]
        state = _sanitize_state(obj.get("state") or "")
        if not carrier:
            raise ValueError("empty carrier_wave")
        return {"state": state, "carrier_wave": carrier, "threads": threads}
    except Exception:
        log.warning("mood synthesis returned non-JSON; storing as raw carrier wave")
        return {"state": "unset", "carrier_wave": raw or text.strip(), "threads": []}


def _state_from_tags(tags: list[str]) -> str:
    """Pull the feeling:* tag off a delta if present."""
    for t in tags or []:
        if isinstance(t, str) and t.startswith("feeling:"):
            return t.split(":", 1)[1] or "unset"
    return "unset"


def _format_prior_mood(prior: dict | None) -> str:
    if not prior:
        return "(no prior mood — this is your first carrier wave)"
    ts = prior.get("timestamp") or ""
    age_label = ""
    parsed = None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
    except Exception:
        parsed = None
    if parsed:
        elapsed = (_now() - parsed).total_seconds()
        hours = elapsed / 3600
        if hours < 1:
            age_label = f"({int(elapsed / 60)} minutes ago — anchor weight: heavy)"
        elif hours < 4:
            age_label = f"({hours:.1f} hours ago — anchor weight: moderate)"
        else:
            age_label = f"({hours:.1f} hours ago — anchor weight: light, mostly faded)"
    state = _state_from_tags(prior.get("tags") or [])
    state_label = f" [previous state: {state}]" if state and state != "unset" else ""
    content = (prior.get("content") or "").strip()
    return f"Prior mood {age_label}{state_label}:\n{content}"


async def _fetch_prior_mood() -> dict | None:
    try:
        results = await delta_client.query(tags_include=["mood-delta"], limit=1)
    except Exception:
        log.exception("failed to fetch prior mood")
        return None
    return results[0] if results else None


async def latest_mood() -> dict | None:
    """Return the most recent mood delta from the lake, parsed.

    Used by the UI endpoint and by the wake check when no fresh synthesis fires.
    """
    prior = await _fetch_prior_mood()
    if not prior:
        return None
    parsed = _parse_mood_payload(prior.get("content") or "")
    state = parsed["state"]
    if state == "unset":
        # Fallback: state may have been written only as a tag on older deltas
        state = _state_from_tags(prior.get("tags") or [])
    return {
        "id": prior.get("id"),
        "state": state,
        "carrier_wave": parsed["carrier_wave"],
        "threads": parsed["threads"],
        "synthesized_at": prior.get("timestamp"),
    }


async def mood_history(limit: int = 200) -> list[dict]:
    """Return recent mood deltas as a timeline, oldest-first.

    Each entry: {id, state, carrier_wave, synthesized_at}. The ECG widget
    uses this to render the colored mood band + state-change event markers.
    """
    try:
        results = await delta_client.query(tags_include=["mood-delta"], limit=limit)
    except Exception:
        log.exception("failed to fetch mood history")
        return []
    timeline: list[dict] = []
    for d in results:
        parsed = _parse_mood_payload(d.get("content") or "")
        state = parsed["state"]
        if state == "unset":
            state = _state_from_tags(d.get("tags") or [])
        timeline.append({
            "id": d.get("id"),
            "state": state,
            "carrier_wave": parsed["carrier_wave"],
            "synthesized_at": d.get("timestamp"),
        })
    timeline.sort(key=lambda e: e.get("synthesized_at") or "")
    return timeline


async def _fetch_recent_activity(session_slug: str | None) -> str:
    try:
        result = await search_module.search(
            text="recent activity threads conversations decisions discoveries",
            depth="shallow",
            session_slug=session_slug,
            limit=RECENT_ACTIVITY_LIMIT,
        )
    except Exception:
        log.exception("failed to fetch recent activity for mood synthesis")
        return ""
    return result.get("as_prompt") or ""


async def synthesize_mood(session_slug: str | None = None) -> dict | None:
    """Run a mood synthesis: read recent activity + prior mood, write a new mood delta."""
    recent = await _fetch_recent_activity(session_slug)
    prior = await _fetch_prior_mood()

    if not recent and not prior:
        log.info("mood synthesis skipped — no recent activity and no prior mood")
        return None

    user_payload_parts: list[str] = []
    if recent:
        user_payload_parts.append(f"=== Recent activity ===\n{recent}")
    user_payload_parts.append(f"=== Prior mood ===\n{_format_prior_mood(prior)}")
    user_message = "\n\n".join(user_payload_parts)

    try:
        resp = await llm.chat.completions.create(
            model=settings.resolved_model,
            messages=[
                {"role": "system", "content": MOOD_DIRECTIVE},
                {"role": "user", "content": user_message},
            ],
            temperature=0.7,
        )
    except Exception:
        log.exception("mood synthesis LLM call failed")
        return None

    text = resp.choices[0].message.content if resp.choices else ""
    parsed = _parse_mood_payload(text or "")

    threads_block = ""
    if parsed["threads"]:
        threads_block = "\n\nThreads:\n" + "\n".join(f"- {t}" for t in parsed["threads"])
    delta_content = parsed["carrier_wave"] + threads_block

    state = parsed["state"]
    delta_tags = MOOD_TAGS + [f"feeling:{state}"]

    written = None
    try:
        written = await delta_client.write(
            content=delta_content,
            tags=delta_tags,
            source=MOOD_SOURCE,
        )
    except Exception:
        log.exception("failed to write mood delta")

    await pressure.mark_synthesis()

    return {
        "id": (written or {}).get("id"),
        "state": state,
        "carrier_wave": parsed["carrier_wave"],
        "threads": parsed["threads"],
        "synthesized_at": _now().isoformat(),
        "prior_mood_id": (prior or {}).get("id"),
    }


async def maybe_synthesize_on_wake(session_slug: str | None = None) -> dict | None:
    """Called at the start of every wake event.

    Marks the wake, decides whether to synthesize, returns the mood (fresh
    or fetched) so the caller can inject it into the system prompt.
    """
    decision, reason = await pressure.should_synthesize()
    await pressure.mark_wake()

    if decision:
        log.info("mood synthesis firing (reason=%s)", reason)
        fresh = await synthesize_mood(session_slug=session_slug)
        if fresh:
            return fresh
        # synthesis failed — fall back to whatever's in the lake
    return await latest_mood()
