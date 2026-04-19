"""Background poller that auto-regenerates the crystal when drift crosses red.

Mirrors the trigger fathom2's dashboard used:
  - poll drift every ~60s
  - if drift / threshold >= red_ratio (default 0.55, 0.9 → fires at 0.495)
  - and the last regen was at least cooldown ago
  - call refresh_crystal() to write a fresh crystal to the lake

Started/stopped by the FastAPI lifespan in api/server.py. Disable by
setting FATHOM_crystal_auto_regen=false.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from . import crystal, drift
from .settings import settings

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_last_fired_at: datetime | None = None
_in_flight = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _within_cooldown() -> bool:
    """True if a regen happened recently enough to skip this tick."""
    global _last_fired_at
    cooldown = settings.crystal_regen_cooldown_seconds
    if cooldown <= 0:
        return False

    # Check our local marker first (cheap)
    if _last_fired_at is not None:
        elapsed = (_now() - _last_fired_at).total_seconds()
        if elapsed < cooldown:
            return True

    # Fall back to the lake — covers restarts and multi-process cases
    current = await crystal.latest()
    if current and current.get("created_at"):
        try:
            ts = datetime.fromisoformat(current["created_at"].replace("Z", "+00:00"))
            elapsed = (_now() - ts).total_seconds()
            if elapsed < cooldown:
                _last_fired_at = ts
                return True
        except Exception:
            pass
    return False


async def _trigger_regen() -> None:
    """Call the regen flow. Imports server lazily to avoid circular import."""
    global _last_fired_at, _in_flight
    if _in_flight:
        return
    _in_flight = True
    try:
        from . import server  # lazy
        log.info("auto-regen firing — invoking refresh_crystal()")
        await server.refresh_crystal()
        _last_fired_at = _now()
    except Exception:
        log.exception("auto-regen refresh_crystal failed")
    finally:
        _in_flight = False


async def _check_once() -> dict:
    """One pass: sample drift, decide, maybe fire. Returns the snapshot."""
    snap = await drift.sample()
    threshold = settings.crystal_drift_threshold
    ratio = settings.crystal_drift_red_ratio

    # Bootstrap case — no crystal exists yet. drift.sample() reports drift=0
    # with no_crystal=True in that state, which would leave the ratio test
    # permanently below threshold. Fire unconditionally so fresh installs
    # get a first crystal without requiring drift to grow from a zero
    # baseline (which it can't, because there's nothing to drift from).
    if snap.get("no_crystal"):
        decision = "cooldown" if await _within_cooldown() else "firing-bootstrap"
        if decision == "firing-bootstrap":
            asyncio.create_task(_trigger_regen())
        return {**snap, "auto_regen": decision, "score": 0.0}

    if threshold <= 0:
        return {**snap, "auto_regen": "disabled-no-threshold"}

    score = float(snap.get("drift", 0.0)) / threshold
    decision = "below"
    if score >= ratio:
        if await _within_cooldown():
            decision = "cooldown"
        else:
            decision = "firing"
            asyncio.create_task(_trigger_regen())
    return {**snap, "auto_regen": decision, "score": score}


async def _loop() -> None:
    log.info(
        "auto-regen loop starting (threshold=%.3f, red_ratio=%.3f, poll=%ds)",
        settings.crystal_drift_threshold,
        settings.crystal_drift_red_ratio,
        settings.crystal_drift_poll_seconds,
    )
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            await _check_once()
        except Exception:
            log.exception("auto-regen poll error")
        try:
            await asyncio.wait_for(
                _stop_event.wait(),
                timeout=settings.crystal_drift_poll_seconds,
            )
        except asyncio.TimeoutError:
            pass
    log.info("auto-regen loop stopped")


def start() -> None:
    """Kick off the polling task. Idempotent."""
    global _task, _stop_event
    if not settings.crystal_auto_regen:
        log.info("auto-regen disabled by settings (crystal_auto_regen=false)")
        return
    if _task is not None and not _task.done():
        return
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    """Signal the loop to exit. Awaits the task briefly."""
    global _task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except asyncio.TimeoutError:
            _task.cancel()
        except Exception:
            pass
    _task = None
    _stop_event = None
