"""Crystal drift anchor — a snapshot of the lake centroid at the moment a
crystal was accepted.

Drift is computed as the cosine distance between this anchor and the
current lake centroid. Because the anchor is fresh right after a regen,
drift reads exactly 0 at that moment by construction — independent of
the crystal text's own embedding. A subsequent bad or short crystal
cannot spike drift, which was the failure mode that caused runaway
regens.

Storage: single JSON sidecar next to drift-history.json. One anchor at
a time — overwritten on each accepted regen.
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

_lock = asyncio.Lock()


def _path() -> Path:
    base = Path(settings.mood_state_path).parent
    return base / "crystal-anchor.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".anchor-", dir=str(p.parent))
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


async def save(centroid: list[float], crystal_id: str | None) -> dict:
    """Persist the anchor. Returns the written record."""
    record = {
        "crystal_id": crystal_id,
        "centroid": list(centroid),
        "dim": len(centroid),
        "saved_at": _now_iso(),
    }
    async with _lock:
        _atomic_write(record)
    return record


async def load() -> dict | None:
    async with _lock:
        p = _path()
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except Exception:
            return None
    if not data.get("centroid"):
        return None
    return data


def cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cos(a, b) for two already-normalized or arbitrary vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
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
