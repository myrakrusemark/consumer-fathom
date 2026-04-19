"""Backup writer with three-tier tripwire and rotation.

Runs as a background task inside delta-store. Every DELTA_BACKUP_INTERVAL_S,
shells out to pg_dump, classifies the result by delta-count shrink vs the
last known-good baseline, and either rotates the dump into the active set
or quarantines it and halts rotation.

Design: plans/radiant-coalescing-nygaard.md.

Tripwire tiers:
    shrink <  WARN_RATIO     → healthy  (silent, rotate)
    shrink <  LOCKDOWN_RATIO → warning  (rotate, write observation delta)
    shrink >= LOCKDOWN_RATIO → lockdown (quarantine, halt rotation, write blocker)
    new_size <  MIN_SIZE_MB  → lockdown (stub-detection floor)

Lockdown is sticky — only cleared by POST /admin/backup/ack.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("delta-store.backup")

# ── Config ───────────────────────────────────────────────────────────────────

BACKUP_DIR = Path(os.environ.get("DELTA_BACKUP_DIR", "/backups"))
INTERVAL_S = float(os.environ.get("DELTA_BACKUP_INTERVAL_S", "3600"))
WARN_RATIO = float(os.environ.get("DELTA_BACKUP_WARN_RATIO", "0.001"))
LOCKDOWN_RATIO = float(os.environ.get("DELTA_BACKUP_LOCKDOWN_RATIO", "0.02"))
MIN_SIZE_MB = int(os.environ.get("DELTA_BACKUP_MIN_SIZE_MB", "50"))
RETAIN = int(os.environ.get("DELTA_BACKUP_RETAIN", "3"))
ENABLED = os.environ.get("DELTA_BACKUP_ENABLED", "true").lower() == "true"

STATE_FILE = BACKUP_DIR / ".state.json"
QUARANTINE_DIR = BACKUP_DIR / "quarantine"

STATE_HEALTHY = "healthy"
STATE_LOCKED = "locked"

REASON_SIZE_FLOOR = "size_floor"
REASON_COUNT_SHRINK = "count_shrink"
REASON_PG_DUMP_FAILED = "pg_dump_failed"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _default_state() -> dict:
    return {
        "state": STATE_HEALTHY,
        "last_attempt_at": None,
        "last_healthy_at": None,
        "last_good_path": None,
        "last_good_size": None,
        "last_good_delta_count": None,
        "last_reason": None,
    }


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return _default_state()


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _file_info(path: Path) -> dict:
    st = path.stat()
    return {
        "path": path.name,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _rotation_files() -> list[Path]:
    """Files in the hourly rotation set — deltas-*.sql.gz at the top level."""
    if not BACKUP_DIR.exists():
        return []
    return sorted(
        (p for p in BACKUP_DIR.glob("deltas-*.sql.gz") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _quarantine_files() -> list[Path]:
    if not QUARANTINE_DIR.exists():
        return []
    return sorted(
        (p for p in QUARANTINE_DIR.glob("deltas-*.sql.gz") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _daily_files() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(
        (p for p in BACKUP_DIR.glob("daily-*.sql.gz") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _rotate(keep: int) -> None:
    """Prune rotation files, keeping the `keep` most recent. Ignores daily-*.sql.gz."""
    files = _rotation_files()
    for stale in files[keep:]:
        try:
            stale.unlink()
            log.info("Rotated out %s", stale.name)
        except OSError as e:
            log.warning("Failed to rotate %s: %s", stale.name, e)


# ── pg_dump shell ────────────────────────────────────────────────────────────


async def _dump_to(tmp_path: Path, dsn: str) -> tuple[bool, str]:
    """Run pg_dump → gzip → tmp_path. Returns (ok, error_msg_if_not_ok)."""
    cmd = (
        f"pg_dump --no-owner --clean --if-exists '{dsn}' "
        f"| gzip -9 > '{tmp_path}'"
    )
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return False, stderr.decode(errors="replace")[:500]
    return True, ""


# ── Classification ───────────────────────────────────────────────────────────


def _classify(new_size: int, live_count: int, prev_count: int | None) -> tuple[str, str | None]:
    """Return (tier, reason). tier ∈ {'healthy','warning','lockdown'}."""
    # No prior reference — a virgin install legitimately produces a tiny
    # backup. Skip both the size-floor and count-shrink checks until we've
    # seen a real snapshot; otherwise day 0 always trips.
    if prev_count is None or prev_count <= 0:
        return "healthy", None

    if new_size < MIN_SIZE_MB * 1024 * 1024:
        return "lockdown", REASON_SIZE_FLOOR

    shrink = max(0.0, (prev_count - live_count) / prev_count)
    if shrink >= LOCKDOWN_RATIO:
        return "lockdown", REASON_COUNT_SHRINK
    if shrink >= WARN_RATIO:
        return "warning", None
    return "healthy", None


# ── Cycle ────────────────────────────────────────────────────────────────────


async def run_cycle(pool, store) -> dict:
    """Execute one backup cycle. Returns the updated state dict."""
    state = load_state()
    state["last_attempt_at"] = _now_iso()

    live_count = await pool.fetchval("SELECT COUNT(*) FROM deltas")
    prev_count = state.get("last_good_delta_count")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    dsn = os.environ.get("DATABASE_URL", "postgresql://fathom:fathom@postgres:5432/deltas")
    stamp = _now_stamp()
    tmp_path = BACKUP_DIR / f"deltas-{stamp}.sql.gz.tmp"

    ok, err = await _dump_to(tmp_path, dsn)
    if not ok:
        state["last_reason"] = REASON_PG_DUMP_FAILED
        save_state(state)
        log.error("pg_dump failed: %s", err)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return state

    new_size = tmp_path.stat().st_size
    tier, reason = _classify(new_size, live_count, prev_count)

    if tier == "lockdown":
        final = QUARANTINE_DIR / f"deltas-{stamp}.sql.gz"
        tmp_path.replace(final)
        state["state"] = STATE_LOCKED
        state["last_reason"] = reason
        save_state(state)

        shrink_pct = (
            f"{((prev_count - live_count) / prev_count) * 100:.2f}%"
            if prev_count and prev_count > 0 else "n/a"
        )
        msg = (
            f"Backup tripwire: {reason}. live_count={live_count} "
            f"last_good_count={prev_count} shrink={shrink_pct} "
            f"new_size={new_size} quarantined={final.name}. "
            f"Rotation frozen. POST /admin/backup/ack to clear."
        )
        log.error(msg)
        await _write_delta_safe(
            store, msg, ["blocker", "backup-incident", "fathom2"],
        )
        return state

    # healthy or warning — promote and rotate.
    final = BACKUP_DIR / f"deltas-{stamp}.sql.gz"
    tmp_path.replace(final)

    state["state"] = STATE_HEALTHY
    state["last_healthy_at"] = _now_iso()
    state["last_good_path"] = final.name
    state["last_good_size"] = new_size
    state["last_good_delta_count"] = live_count
    state["last_reason"] = None
    save_state(state)
    _rotate(RETAIN + 1)

    if tier == "warning" and prev_count and prev_count > 0:
        shrink = (prev_count - live_count) / prev_count
        msg = (
            f"Backup warning: delta count dipped {shrink * 100:.3f}% "
            f"({prev_count} → {live_count}) — within bounds "
            f"(< {LOCKDOWN_RATIO * 100:.1f}%), rotation proceeded. "
            f"new_size={new_size} bytes."
        )
        log.warning(msg)
        await _write_delta_safe(
            store, msg, ["backup-warning", "observation", "fathom2"],
        )
    else:
        log.info(
            "Backup healthy: %s (%d bytes, %d deltas)",
            final.name, new_size, live_count,
        )

    return state


async def _write_delta_safe(store, content: str, tags: list[str]) -> None:
    if store is None:
        return
    try:
        await store.write(
            content=content,
            tags=tags,
            source="delta-store-backup",
        )
    except Exception:
        log.exception("Failed to write status delta")


# ── Ack ──────────────────────────────────────────────────────────────────────


async def ack(pool, discard_quarantine: bool = False) -> dict:
    """Clear lockdown. Promotes the most recent quarantined dump to rotation
    (if any) and re-anchors the baseline at the current live count.

    Returns {state, promoted_path, discarded_count}.
    """
    state = load_state()
    if state.get("state") != STATE_LOCKED:
        raise ValueError(f"Not in lockdown (state={state.get('state')})")

    q_files = _quarantine_files()
    promoted_path: str | None = None

    if q_files:
        picked = q_files[0]
        stamp = _now_stamp()
        final = BACKUP_DIR / f"deltas-{stamp}.sql.gz"
        picked.replace(final)
        state["last_good_path"] = final.name
        state["last_good_size"] = final.stat().st_size
        promoted_path = final.name

    # Re-anchor count at whatever's live right now.
    state["last_good_delta_count"] = await pool.fetchval("SELECT COUNT(*) FROM deltas")
    state["state"] = STATE_HEALTHY
    state["last_healthy_at"] = _now_iso()
    state["last_reason"] = None
    save_state(state)

    discarded = 0
    if discard_quarantine:
        for f in _quarantine_files():
            try:
                f.unlink()
                discarded += 1
            except OSError:
                pass

    _rotate(RETAIN + 1)

    return {
        "state": state["state"],
        "promoted_path": promoted_path,
        "discarded_count": discarded,
    }


# ── Inventory ────────────────────────────────────────────────────────────────


def inventory() -> dict:
    """Return current rotation / quarantine / daily file listings."""
    return {
        "rotation": [_file_info(p) for p in _rotation_files()],
        "quarantine": [_file_info(p) for p in _quarantine_files()],
        "daily": [_file_info(p) for p in _daily_files()],
    }


# ── Loop ─────────────────────────────────────────────────────────────────────


async def backup_loop() -> None:
    """Long-running task. Registered by server.py lifespan."""
    if not ENABLED:
        log.info("Backup loop disabled (DELTA_BACKUP_ENABLED=false)")
        return

    log.info(
        "Backup loop started: interval=%ss warn=%.3f lockdown=%.3f retain=%d dir=%s",
        INTERVAL_S, WARN_RATIO, LOCKDOWN_RATIO, RETAIN, BACKUP_DIR,
    )
    # Short initial delay so the cycle doesn't race startup.
    await asyncio.sleep(30)

    # Import lazily to avoid circular import at module load.
    from deltas import db as db_mod
    from deltas import server as server_mod

    while True:
        try:
            pool = db_mod._pool
            store = server_mod.store
            if pool is None:
                await asyncio.sleep(INTERVAL_S)
                continue
            await run_cycle(pool, store)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Backup loop error")
        await asyncio.sleep(INTERVAL_S)
