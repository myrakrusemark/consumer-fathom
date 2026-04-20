"""Crystal drift snapshots — anchor-based.

Drift is cosine distance between the lake centroid right now and the
anchor centroid snapshotted at the last accepted crystal regen. This
decouples drift from the crystal's own text embedding, so a short or
failure-mode crystal can no longer self-trigger a runaway regen loop.

Each /v1/drift call samples the current centroid, compares to the
anchor, and appends a point to drift-history.json for the ECG widget.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import crystal as crystal_module
from . import crystal_anchor
from . import delta_client
from .settings import settings

HISTORY_LIMIT: int = 1000

_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _path() -> Path:
    """Place drift-history.json next to the mood-state file."""
    base = Path(settings.mood_state_path).parent
    return base / "drift-history.json"


def _load_raw() -> dict:
    p = _path()
    if not p.exists():
        return {"history": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"history": []}


def _save_raw(state: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".drift-", dir=str(p.parent))
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


async def sample() -> dict:
    """Sample current drift against the anchor, append to history.

    Returns a snapshot dict with at least {drift, sampled_at}. Optional
    flags: no_crystal (no crystal ever generated), no_anchor (crystal
    exists but anchor file missing — usually a pre-anchor-era install
    or a corrupted sidecar), error (centroid fetch failed).
    """
    try:
        current = await crystal_module.latest()
    except Exception:
        # Lake unreachable — do NOT return no_crystal (would spuriously
        # trigger a bootstrap-fire in auto_regen on a transient hiccup).
        entry_t = _iso(_now())
        return {"drift": 0.0, "new_deltas": 0, "total_deltas": 0, "error": True, "sampled_at": entry_t}
    anchor = await crystal_anchor.load()

    if not current or not current.get("text"):
        snapshot = {"drift": 0.0, "new_deltas": 0, "total_deltas": 0, "no_crystal": True}
    elif not anchor:
        # Crystal present but no anchor — don't signal drift (the
        # auto-regen poller reads no_anchor and skips, rather than
        # firing a bootstrap regen against a state that isn't actually
        # empty). Operator intervention or next accepted regen will
        # populate the anchor.
        snapshot = {"drift": 0.0, "new_deltas": 0, "total_deltas": 0, "no_anchor": True}
    else:
        try:
            c = await delta_client.centroid()
            vec = c.get("centroid")
            total = int(c.get("total_deltas") or 0)
            if not vec:
                snapshot = {"drift": 0.0, "new_deltas": 0, "total_deltas": total, "empty_lake": True}
            else:
                d = crystal_anchor.cosine_distance(anchor["centroid"], vec)
                snapshot = {
                    "drift": round(d, 4),
                    "new_deltas": 0,
                    "total_deltas": total,
                }
        except Exception:
            snapshot = {"drift": 0.0, "new_deltas": 0, "total_deltas": 0, "error": True}

    now = _now()
    entry = {
        "t": _iso(now),
        "v": float(snapshot.get("drift", 0.0)),
        "new": int(snapshot.get("new_deltas", 0)),
        "total": int(snapshot.get("total_deltas", 0)),
    }
    async with _lock:
        state = _load_raw()
        history = state.get("history") or []
        history.append(entry)
        if len(history) > HISTORY_LIMIT:
            history = history[-HISTORY_LIMIT:]
        state["history"] = history
        _save_raw(state)

    return {**snapshot, "sampled_at": entry["t"]}


async def history(since_seconds: int | None = None) -> list[dict]:
    """Return drift history. Optionally filter to last N seconds."""
    async with _lock:
        state = _load_raw()
        items = list(state.get("history") or [])
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
