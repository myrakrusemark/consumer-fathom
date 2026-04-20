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

from . import crystal, crystal_anchor, delta_client, drift
from .settings import settings

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_last_fired_at: datetime | None = None
_in_flight = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _within_cooldown() -> bool:
    """True if a regen happened recently enough to skip this tick.

    On lake unreachable, returns True — fail safe. We'd rather miss a
    needed regen than fire another runaway while the lake is flaky.
    """
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
    try:
        current = await crystal.latest()
    except Exception:
        log.warning("auto-regen cooldown check: lake unreachable, failing safe (treating as within cooldown)")
        return True
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


async def _self_heal_anchor() -> bool:
    """Crystal exists but anchor is missing — snapshot current centroid.

    This covers (a) installs from before anchors existed, (b) a wiped
    /data volume, (c) a corrupted sidecar. Self-heal rather than fire a
    regen, because the crystal itself is fine — we just lost the ruler.
    Returns True on success.
    """
    try:
        c = await delta_client.centroid()
        vec = c.get("centroid")
        if not vec:
            return False
        try:
            current = await crystal.latest()
        except Exception:
            current = None
        await crystal_anchor.save(vec, (current or {}).get("id"))
        log.info("auto-regen self-healed missing crystal anchor from current centroid")
        return True
    except Exception:
        log.exception("auto-regen anchor self-heal failed")
        return False


async def _check_once() -> dict:
    """One pass: sample drift, decide, maybe fire. Returns the snapshot."""
    snap = await drift.sample()
    threshold = settings.crystal_drift_threshold
    ratio = settings.crystal_drift_red_ratio

    # Lake-unreachable — skip the tick rather than fire. This is the
    # guardrail against the 2026-04-19 runaway: transport errors must
    # never be interpreted as "no crystal" or "drift high."
    if snap.get("error"):
        return {**snap, "auto_regen": "skipped-lake-unreachable", "score": 0.0}

    # Bootstrap case — no crystal has ever been generated. Under the
    # anchor-based drift design this is the ONLY condition that fires
    # unconditionally; a crystal that merely has a missing anchor is
    # self-healed (see below) rather than regenerated.
    if snap.get("no_crystal"):
        decision = "cooldown" if await _within_cooldown() else "firing-bootstrap"
        if decision == "firing-bootstrap":
            asyncio.create_task(_trigger_regen())
        return {**snap, "auto_regen": decision, "score": 0.0}

    # Crystal exists but anchor file is missing — self-heal by snapshotting
    # the current centroid. Do NOT fire a regen: the crystal itself is fine,
    # we only lost the measurement baseline.
    if snap.get("no_anchor"):
        healed = await _self_heal_anchor()
        return {**snap, "auto_regen": "self-healed-anchor" if healed else "no-anchor-heal-failed", "score": 0.0}

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
