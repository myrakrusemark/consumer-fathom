"""Mood-layer pressure metric — derived from the lake.

Pressure answers: "how much salient activity has built up since the last
mood synthesis?" It's not stored. We compute it on read by querying the
lake for deltas in the relevant window, applying source/tag weights, and
exponentially decaying each contribution.

The only state we persist is the wake-control file (last_synthesis_at +
last_wake_at) — the reset and contrast-wake markers. Everything else is
a derived view, sibling to /v1/usage.

This means pressure sees ALL lake activity — the local agent, the chat
loop, the source-runner, anything anyone writes to the lake — not just
what flows through this api process.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import delta_client
from .settings import settings

# ── Weights ─────────────────────────────────────
# Source weights — how much each delta source contributes to pressure.
SOURCE_WEIGHTS: dict[str, float] = {
    "fathom-chat": 1.5,
    "fathom-feed": 0.5,
    "fathom-mood": 0.0,           # mood deltas don't drive their own resynthesis
    "fathom-source-runner": 0.4,
    "fathom-agent": 0.2,
    "claude-code": 0.8,
    "consumer-api": 0.8,
}
USER_TAG_BOOST: float = 0.5
DEFAULT_WEIGHT: float = 0.3

# How far back to look when computing pressure. We never look further than
# this — older deltas have decayed to negligible weight anyway and the
# query gets expensive.
PRESSURE_WINDOW_HOURS: int = 36
PRESSURE_QUERY_LIMIT: int = 2000

# ── Persisted state (the small bit) ─────────────
_lock = asyncio.Lock()


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


def _state_path() -> Path:
    return Path(settings.mood_state_path)


def _empty_state() -> dict:
    return {
        "last_wake_at": None,
        "last_synthesis_at": None,
    }


def _load_raw() -> dict:
    p = _state_path()
    if not p.exists():
        return _empty_state()
    try:
        data = json.loads(p.read_text())
        # Strip any legacy keys from the previous counter-based design.
        return {
            "last_wake_at": data.get("last_wake_at"),
            "last_synthesis_at": data.get("last_synthesis_at"),
        }
    except Exception:
        return _empty_state()


def _save_raw(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mood-state-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ── Weight + decay primitives ───────────────────


def _delta_weight(delta: dict) -> float:
    source = (delta.get("source") or "").strip()
    weight = SOURCE_WEIGHTS.get(source, DEFAULT_WEIGHT)
    tags = delta.get("tags") or []
    if "user" in tags:
        weight += USER_TAG_BOOST
    return max(0.0, weight)


def _decay_factor(seconds_ago: float) -> float:
    """Exponential decay using the configured half-life."""
    if seconds_ago <= 0:
        return 1.0
    half_life = max(1, settings.mood_decay_half_life_seconds)
    return math.pow(0.5, seconds_ago / half_life)


# ── Lake fetch ──────────────────────────────────


async def _fetch_window_deltas(window_hours: int = PRESSURE_WINDOW_HOURS) -> list[dict]:
    """Pull all deltas from the last N hours, slim shape (source + tags + ts)."""
    since = _now() - timedelta(hours=window_hours)
    try:
        results = await delta_client.query(
            time_start=since.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            limit=PRESSURE_QUERY_LIMIT,
        )
    except Exception:
        return []
    return results or []


# ── Public: read ────────────────────────────────


async def read_pressure() -> dict:
    """Compute current pressure from the lake.

    Pressure = sum over deltas since last_synthesis_at of:
                   weight(source, tags) × decay(now - delta_ts)

    If no synthesis has ever fired, we use the full window.
    """
    state = _load_raw()
    last_synth = _parse(state.get("last_synthesis_at"))
    last_wake = _parse(state.get("last_wake_at"))
    now = _now()

    deltas = await _fetch_window_deltas()
    cutoff = last_synth if last_synth else now - timedelta(hours=PRESSURE_WINDOW_HOURS)

    volume = 0.0
    for d in deltas:
        ts = _parse(d.get("timestamp"))
        if not ts or ts <= cutoff:
            continue
        w = _delta_weight(d)
        if w <= 0:
            continue
        volume += w * _decay_factor((now - ts).total_seconds())

    time_since_wake = (now - last_wake).total_seconds() if last_wake else None
    time_since_synth = (now - last_synth).total_seconds() if last_synth else None
    return {
        "volume": volume,
        "last_wake_at": last_wake,
        "last_synthesis_at": last_synth,
        "time_since_wake_seconds": time_since_wake,
        "time_since_synthesis_seconds": time_since_synth,
        "threshold": settings.mood_pressure_threshold,
        "contrast_wake_seconds": settings.mood_contrast_wake_seconds,
    }


async def should_synthesize() -> tuple[bool, str]:
    p = await read_pressure()
    if p["volume"] >= p["threshold"]:
        return True, "pressure"
    if p["last_synthesis_at"] is None:
        return True, "first-run"
    if p["time_since_wake_seconds"] is not None and p["time_since_wake_seconds"] >= p["contrast_wake_seconds"]:
        return True, "contrast-wake"
    return False, "below-threshold"


async def history(since_seconds: int | None = None, buckets: int = 60) -> list[dict]:
    """Compute a rolling pressure curve over the window.

    For each tick (default 60 buckets across the window), evaluate
    pressure-as-of-that-tick using the same weight + decay rules,
    re-anchored to whichever mood-synthesis event was most recent at
    that tick. Yields a continuous line for the ECG.
    """
    window_seconds = since_seconds or PRESSURE_WINDOW_HOURS * 3600
    deltas = await _fetch_window_deltas(window_hours=int(window_seconds / 3600) + 1)

    # Pre-extract (timestamp, weight) for non-zero contributors only.
    enriched: list[tuple[datetime, float, bool]] = []
    for d in deltas:
        ts = _parse(d.get("timestamp"))
        if not ts:
            continue
        w = _delta_weight(d)
        is_synth = "mood-delta" in (d.get("tags") or [])
        if w <= 0 and not is_synth:
            continue
        enriched.append((ts, w, is_synth))
    enriched.sort(key=lambda e: e[0])

    now = _now()
    start = now - timedelta(seconds=window_seconds)
    half_life = max(1, settings.mood_decay_half_life_seconds)

    out: list[dict] = []
    step = window_seconds / max(1, buckets)
    for i in range(buckets + 1):
        tick = start + timedelta(seconds=step * i)
        # Anchor: most recent synthesis at-or-before this tick.
        anchor = None
        for ts, _w, is_synth in enriched:
            if not is_synth:
                continue
            if ts <= tick:
                anchor = ts
            else:
                break
        # Sum decayed contributions from each delta after the anchor and
        # before the tick.
        total = 0.0
        for ts, w, _is_synth in enriched:
            if w <= 0:
                continue
            if anchor is not None and ts <= anchor:
                continue
            if ts > tick:
                break
            seconds_ago = (tick - ts).total_seconds()
            total += w * math.pow(0.5, seconds_ago / half_life)
        out.append({"t": _iso(tick), "v": round(total, 4)})

    return out


# ── Public: write (just the markers) ────────────


async def mark_wake() -> None:
    async with _lock:
        state = _load_raw()
        state["last_wake_at"] = _iso(_now())
        _save_raw(state)


async def mark_synthesis() -> None:
    async with _lock:
        state = _load_raw()
        state["last_synthesis_at"] = _iso(_now())
        _save_raw(state)
