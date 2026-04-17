"""Crystal drift snapshots — sampled lazily, persisted to a small JSON file.

Drift is cosine distance between the current identity crystal and the lake's
exponentially-decayed centroid. Each /v1/drift call samples and appends a
record so the ECG widget can render an actual line over time instead of a
single live value.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import delta_client
from .prompt import load_crystal
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
    """Sample current drift, append to history, return the snapshot.

    If no crystal exists or the delta-store call fails, returns a zero
    sample but still records the timestamp so gaps don't appear in the ECG.
    """
    crystal_text = load_crystal()
    if not crystal_text:
        snapshot = {"drift": 0.0, "new_deltas": 0, "total_deltas": 0, "no_crystal": True}
    else:
        try:
            snapshot = await delta_client.drift(crystal_text)
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
