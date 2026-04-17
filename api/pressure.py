"""Mood-layer pressure metric.

Tracks how much "salient activity" has accumulated since the last mood
synthesis. State is a single JSON file written atomically; decay is applied
on read so no background job is needed.

Volume pressure rises when deltas arrive (weighted by source / tag) and
decays exponentially over time. When a wake event happens (a chat request),
the consumer reads the pressure and decides whether to synthesize.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .settings import settings

# Source weight table — how much each delta source contributes to pressure.
# Tunable; these are first-pass values.
SOURCE_WEIGHTS: dict[str, float] = {
    "fathom-chat": 1.5,       # conversation turn (assistant or user)
    "fathom-feed": 0.5,       # feed item
    "fathom-mood": 0.0,       # mood deltas don't drive their own re-synthesis
    "fathom-source-runner": 0.4,
    "fathom-agent": 0.2,
    "claude-code": 0.8,
}
USER_TAG_BOOST: float = 0.5   # any delta tagged "user" gets an extra bump
DEFAULT_WEIGHT: float = 0.3   # unknown source

# Rolling pressure history kept inside the same JSON file (small, bounded).
HISTORY_LIMIT: int = 1000

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


def _decay(pressure: float, last_updated: datetime, now: datetime) -> float:
    """Exponential decay with half-life from settings."""
    half_life = max(1, settings.mood_decay_half_life_seconds)
    elapsed = (now - last_updated).total_seconds()
    if elapsed <= 0:
        return pressure
    return pressure * math.pow(0.5, elapsed / half_life)


def _delta_weight(delta: dict) -> float:
    source = (delta.get("source") or "").strip()
    weight = SOURCE_WEIGHTS.get(source, DEFAULT_WEIGHT)
    tags = delta.get("tags") or []
    if "user" in tags:
        weight += USER_TAG_BOOST
    return max(0.0, weight)


def _empty_state() -> dict:
    now_iso = _iso(_now())
    return {
        "volume_pressure": 0.0,
        "last_wake_at": None,
        "last_synthesis_at": None,
        "updated_at": now_iso,
        "history": [],
    }


def _push_history(state: dict, value: float, when: datetime) -> None:
    """Append a sample to the rolling history, trimming to HISTORY_LIMIT."""
    history = state.get("history") or []
    history.append({"t": _iso(when), "v": round(value, 4)})
    if len(history) > HISTORY_LIMIT:
        history = history[-HISTORY_LIMIT:]
    state["history"] = history


def _load_raw() -> dict:
    p = Path(settings.mood_state_path)
    if not p.exists():
        return _empty_state()
    try:
        return json.loads(p.read_text())
    except Exception:
        return _empty_state()


def _save_raw(state: dict) -> None:
    p = Path(settings.mood_state_path)
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


async def add_delta(delta: dict) -> None:
    """Record a new delta in the pressure metric. Fire-and-forget safe."""
    weight = _delta_weight(delta)
    if weight <= 0:
        return
    async with _lock:
        state = _load_raw()
        now = _now()
        last = _parse(state.get("updated_at")) or now
        decayed = _decay(state.get("volume_pressure", 0.0), last, now)
        new_value = decayed + weight
        state["volume_pressure"] = new_value
        state["updated_at"] = _iso(now)
        _push_history(state, new_value, now)
        _save_raw(state)


async def read_pressure() -> dict:
    """Return current pressure with on-read decay applied. Does not write."""
    async with _lock:
        state = _load_raw()
        now = _now()
        last = _parse(state.get("updated_at")) or now
        volume = _decay(state.get("volume_pressure", 0.0), last, now)
        last_wake = _parse(state.get("last_wake_at"))
        last_synth = _parse(state.get("last_synthesis_at"))
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
    """Decide whether the current wake should trigger a fresh mood synthesis.

    Returns (decision, reason). Reason is a short tag for logging / UI.
    """
    p = await read_pressure()
    if p["volume"] >= p["threshold"]:
        return True, "pressure"
    if p["last_synthesis_at"] is None:
        return True, "first-run"
    if p["time_since_wake_seconds"] is not None and p["time_since_wake_seconds"] >= p["contrast_wake_seconds"]:
        return True, "contrast-wake"
    return False, "below-threshold"


async def mark_wake() -> None:
    """Record that a wake event happened."""
    async with _lock:
        state = _load_raw()
        state["last_wake_at"] = _iso(_now())
        _save_raw(state)


async def mark_synthesis() -> None:
    """Reset pressure after a mood synthesis."""
    async with _lock:
        state = _load_raw()
        now = _now()
        state["volume_pressure"] = 0.0
        state["last_synthesis_at"] = _iso(now)
        state["updated_at"] = _iso(now)
        _push_history(state, 0.0, now)
        _save_raw(state)


async def history(since_seconds: int | None = None) -> list[dict]:
    """Return rolling pressure history. Optionally filter to last N seconds."""
    async with _lock:
        state = _load_raw()
        history = list(state.get("history") or [])
    if since_seconds is None:
        return history
    cutoff = _now().timestamp() - since_seconds
    out: list[dict] = []
    for entry in history:
        ts = _parse(entry.get("t"))
        if ts and ts.timestamp() >= cutoff:
            out.append(entry)
    return out
