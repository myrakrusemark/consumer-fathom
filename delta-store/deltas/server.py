"""Delta store v2 HTTP API.

Fully async. Postgres + pgvector backend. All v1 endpoints preserved.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Security, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel as _BaseModel

from deltas import backup, retrievals
from deltas.contacts import ContactsStore
from deltas.db import close_pool, init_pool
from deltas.models import (
    BackupAckRequest,
    BackupAckResult,
    BackupFile,
    BackupStateOut,
    BatchIn,
    BatchResult,
    ContactIn,
    ContactOut,
    DeltaIn,
    DeltaOut,
    DimensionWeights,
    HandleIn,
    HandleOut,
    PlanRequest,
    PlanResponse,
    ResolvedHandle,
    SearchRequest,
    SearchResult,
    WriteResult,
)
from deltas.query import QueryEngine
from deltas.store import DeltaStore, _format_ts, _vec_to_list

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("delta-store")

# ── Config ───────────────────────────────────────────────────────────────────

MEDIA_DIR = Path(os.environ.get("DELTA_MEDIA_DIR", "/data/media"))
API_KEY = os.environ.get("DELTA_API_KEY", "")
EMBED_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "32"))
EMBED_INTERVAL = float(os.environ.get("EMBED_INTERVAL", "5"))
FACET_WEBHOOK_URL = os.environ.get("FACET_WEBHOOK_URL", "")
FACET_THRESHOLD = float(os.environ.get("FACET_THRESHOLD", "0.72"))
REAP_INTERVAL = float(os.environ.get("REAP_INTERVAL", "300"))

# ── Auth ─────────────────────────────────────────────────────────────────────

store: DeltaStore | None = None
contacts_store: ContactsStore | None = None
query_engine: QueryEngine | None = None
plan_executor = None  # PlanExecutor — set after import in lifespan
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: str | None = Security(api_key_header)):  # noqa: B008
    if not API_KEY:
        return
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ── Background embed loop ───────────────────────────────────────────────────


def _blend(a: list[float], b: list[float]) -> list[float]:
    """Average two vectors and L2-normalize."""
    n = min(len(a), len(b))
    if n == 0:
        return a or b
    out = [(a[i] + b[i]) / 2.0 for i in range(n)]
    norm = sum(x * x for x in out) ** 0.5
    return [x / norm for x in out] if norm > 0 else out


_FACET_SKIP_TAGS = {
    "assistant",
    "identity-crystal",
    "context",
    "feed-story",
    "feed-engagement",
    "synthesis",
}
_facet_labels: list[str] = []
_facet_texts: list[str] = []
_facet_embeddings: list[list[float]] = []
_facet_http = None

# ── Resonance source allow-list ─────────────────────────────────────────────
# Only deltas whose `source` is in this set can trigger facet activation.
# Default: empty (no resonance). Managed via GET/POST /hooks/activation/sources
# and persisted to RESONANCE_PATH. Enabling an agent-written source (e.g.
# fathom-loop, or any workspace source from claude-code hooks) creates a
# feedback-loop risk — the Settings UI flags those with a warning.
RESONANCE_PATH = Path(os.environ.get("RESONANCE_PATH", "/data/resonance.json"))
_resonance_allowed: set[str] = set()


def _load_resonance() -> None:
    global _resonance_allowed
    try:
        with open(RESONANCE_PATH) as f:
            data = json.load(f)
        allowed = data.get("allowed", [])
        if isinstance(allowed, list):
            _resonance_allowed = {str(s) for s in allowed}
    except FileNotFoundError:
        _resonance_allowed = set()
    except Exception:
        log.warning("Failed to load resonance allow-list", exc_info=True)
        _resonance_allowed = set()


def _save_resonance() -> None:
    try:
        RESONANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RESONANCE_PATH, "w") as f:
            json.dump({"allowed": sorted(_resonance_allowed)}, f)
    except Exception:
        log.warning("Failed to save resonance allow-list", exc_info=True)


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    return dot


def _check_facet_activation(delta: dict, embedding: list[float]) -> dict | None:
    """Return activation payload if this delta matches crystal facets, else None."""
    if not FACET_WEBHOOK_URL or not _facet_embeddings:
        return None

    source = delta.get("source", "")
    if source not in _resonance_allowed:
        return None

    tags = set(delta.get("tags", []))
    if tags & _FACET_SKIP_TAGS:
        return None

    matching_facets = []
    for i, fe in enumerate(_facet_embeddings):
        sim = _cosine_sim(embedding, fe)
        if sim >= FACET_THRESHOLD:
            matching_facets.append({"label": _facet_labels[i], "similarity": round(sim, 4)})

    if not matching_facets:
        return None

    session_id = ""
    for tag in tags:
        if tag.startswith("session:"):
            session_id = tag[8:]
            break

    best_sim = max(f["similarity"] for f in matching_facets)

    return {
        "id": delta["id"],
        "content": delta["content"],
        "tags": list(tags),
        "source": delta.get("source", ""),
        "timestamp": delta.get("timestamp", ""),
        "session_id": session_id,
        "similarity": round(best_sim, 4),
        "channel": "facet",
        "matching_facets": matching_facets,
    }


def _fire_facet_webhook(payload: dict) -> None:
    try:
        _facet_http.post(FACET_WEBHOOK_URL, json=payload, timeout=5)
        log.info(
            "Facet activation for delta %s (sim=%.3f, facets=%d)",
            payload["id"],
            payload["similarity"],
            len(payload["matching_facets"]),
        )
    except Exception:
        log.debug("Facet webhook failed (non-critical)", exc_info=True)


async def embed_loop():
    global _facet_http
    from deltas.embedder import embed_image, embed_texts
    from deltas.media import resolve as resolve_media

    if FACET_WEBHOOK_URL:
        import httpx

        _facet_http = httpx.Client()
        _load_resonance()
        log.info(
            "Facet activation ready: threshold=%.2f, webhook=%s, allowed_sources=%d",
            FACET_THRESHOLD,
            FACET_WEBHOOK_URL,
            len(_resonance_allowed),
        )

    log.info("Embed loop started (batch=%d, interval=%.0fs)", EMBED_BATCH_SIZE, EMBED_INTERVAL)
    while True:
        try:
            batch = await store.unembedded(limit=EMBED_BATCH_SIZE)
            if not batch:
                await asyncio.sleep(EMBED_INTERVAL)
                continue

            contents = [d["content"] for d in batch]
            tag_strings = [" ".join(d["tags"]) or "unknown" for d in batch]
            text_embs = await asyncio.to_thread(embed_texts, contents)
            prov_embs = await asyncio.to_thread(embed_texts, tag_strings)

            batch_candidates: list[dict] = []
            for i, d in enumerate(batch):
                emb = text_embs[i]
                if d.get("media_hash"):
                    path = resolve_media(MEDIA_DIR, d["media_hash"])
                    if path:
                        img_emb = await asyncio.to_thread(embed_image, str(path))
                        if d["content"] and not d["content"].startswith("[image:"):
                            emb = _blend(emb, img_emb)
                        else:
                            emb = img_emb
                await store.update_embeddings(d["id"], emb, prov_embs[i])

                candidate = _check_facet_activation(d, emb)
                if candidate:
                    batch_candidates.append(candidate)

            # Fire at most one activation per batch — the highest-similarity match
            if batch_candidates:
                best = max(batch_candidates, key=lambda p: p["similarity"])
                _fire_facet_webhook(best)
                if len(batch_candidates) > 1:
                    log.info(
                        "Batch deduplication: %d matches, fired best (sim=%.3f)",
                        len(batch_candidates),
                        best["similarity"],
                    )

            stats = await store.embedding_stats()
            log.info("Embedded %d deltas (%d pending)", len(batch), stats["pending"])
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Embed loop error")
            await asyncio.sleep(EMBED_INTERVAL)


async def reap_loop():
    while True:
        await asyncio.sleep(REAP_INTERVAL)
        try:
            reaped = await store.reap_expired()
            if reaped:
                log.info("Reaped %d expired deltas", reaped)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Reap loop error")


# ── App lifecycle ────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store, contacts_store, query_engine, plan_executor

    pool = await init_pool()
    store = DeltaStore(pool)
    contacts_store = ContactsStore(pool)
    query_engine = QueryEngine(store=store, pool=pool)

    from deltas.embedder import embed_text
    from deltas.plan import PlanExecutor

    plan_executor = PlanExecutor(pool=pool, embed_fn=embed_text)

    embed_task = asyncio.create_task(embed_loop())
    reap_task = asyncio.create_task(reap_loop())
    backup_task = asyncio.create_task(backup.backup_loop())
    yield
    embed_task.cancel()
    reap_task.cancel()
    backup_task.cancel()
    await close_pool()


app = FastAPI(title="Delta Store v2", lifespan=lifespan, dependencies=[Depends(verify_api_key)])
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes: Write ────────────────────────────────────────────────────────────


@app.post("/deltas", response_model=WriteResult)
async def write_delta(delta: DeltaIn):
    delta_id = await store.write(**delta.model_dump())
    if delta_id is None:
        return JSONResponse(
            status_code=200,
            content={"id": None, "deduped": True},
        )
    return WriteResult(id=delta_id)


@app.post("/deltas/batch", response_model=BatchResult)
async def write_batch(batch: BatchIn):
    count = await store.write_batch([d.model_dump() for d in batch.deltas])
    return {"count": count}


# ── Routes: Media ────────────────────────────────────────────────────────────


class MediaDeltaIn(_BaseModel):
    content: str = ""
    tags: list[str] = []
    source: str = "unknown"
    image_base64: str
    expires_at: str | None = None


@app.post("/deltas/media", response_model=WriteResult)
async def write_media_delta_b64(req: MediaDeltaIn):
    from deltas.media import ingest

    try:
        image_bytes = base64.b64decode(req.image_base64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {e}") from e

    media_hash = await asyncio.to_thread(ingest, MEDIA_DIR, image_bytes)
    content = req.content or f"[image:{media_hash}]"
    delta_id = await store.write(
        content=content,
        modality="image",
        tags=req.tags,
        source=req.source,
        media_hash=media_hash,
        expires_at=req.expires_at,
    )
    return WriteResult(id=delta_id, media_hash=media_hash)


@app.post("/deltas/media/upload", response_model=WriteResult)
async def write_media_delta_upload(
    file: UploadFile = File(...),  # noqa: B008
    content: str = Form(""),
    tags: str = Form(""),
    source: str = Form("unknown"),
    expires_at: str = Form(""),
):
    from deltas.media import ingest

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    media_hash = await asyncio.to_thread(ingest, MEDIA_DIR, image_bytes)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    delta_content = content or f"[image:{media_hash}]"
    delta_id = await store.write(
        content=delta_content,
        modality="image",
        tags=tag_list,
        source=source,
        media_hash=media_hash,
        expires_at=expires_at or None,
    )
    return WriteResult(id=delta_id, media_hash=media_hash)


@app.get("/media/{media_hash}")
async def serve_media(media_hash: str):
    from deltas.media import resolve

    clean = media_hash.replace(".webp", "")
    if not all(c in "0123456789abcdef" for c in clean):
        raise HTTPException(status_code=400, detail="Invalid media hash")

    path = resolve(MEDIA_DIR, clean)
    if path is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return FileResponse(path, media_type="image/webp")


class BatchGetIn(_BaseModel):
    ids: list[str]


@app.post("/deltas/batch-get", response_model=list[DeltaOut])
async def batch_get(req: BatchGetIn):
    results = []
    for delta_id in req.ids[:500]:
        d = await store.get(delta_id)
        if d:
            results.append(d)
    return results


# ── Routes: Read ─────────────────────────────────────────────────────────────


@app.get("/deltas/strata")
async def strata():
    """PCA-projected semantic coordinates for 3D strata visualization."""
    import numpy as np

    rows = await store.embedded_rows()
    if not rows:
        return []

    ids, timestamps, sources, modalities, lengths, embeddings = [], [], [], [], [], []
    for r in rows:
        emb = r["embedding"]
        if emb is None:
            continue
        vec = _vec_to_list(emb)
        ids.append(r["id"])
        timestamps.append(_format_ts(r["timestamp"]))
        sources.append(r["source"])
        modalities.append(r["modality"] or "text")
        lengths.append(r["content_length"])
        embeddings.append(vec)

    mat = np.array(embeddings, dtype=np.float32)

    mean = mat.mean(axis=0)
    centered = mat - mean
    _, _, Vt = np.linalg.svd(centered, full_matrices=False)
    proj = centered @ Vt[:2].T

    for axis in range(2):
        col = proj[:, axis]
        lo, hi = col.min(), col.max()
        rng = hi - lo if hi > lo else 1.0
        proj[:, axis] = (col - lo) / rng * 2 - 1

    return [
        {
            "id": ids[i],
            "t": timestamps[i],
            "s": sources[i],
            "m": modalities[i],
            "len": lengths[i],
            "x": round(float(proj[i, 0]), 4),
            "z": round(float(proj[i, 1]), 4),
        }
        for i in range(len(ids))
    ]


@app.get("/deltas/{delta_id}", response_model=DeltaOut)
async def get_delta(delta_id: str):
    result = await store.get(delta_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Delta not found")
    return result


@app.get("/deltas", response_model=list[DeltaOut])
async def query_deltas(
    time_start: str | None = None,
    time_end: str | None = None,
    tags_include: list[str] | None = Query(None),  # noqa: B008
    tags_exclude: list[str] | None = Query(None),  # noqa: B008
    modality: str | None = None,
    source: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    results = await store.query(
        time_start=time_start,
        time_end=time_end,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        modality=modality,
        source=source,
        limit=limit,
        offset=offset,
    )
    # Structured queries are bookkeeping reads (dashboards, pressure,
    # usage aggregation) — not memory recall. Only /search, /search/image,
    # and /plan count toward the retrieval timeline.
    return results


# ── Routes: Search ───────────────────────────────────────────────────────────


@app.post("/search", response_model=SearchResult)
async def search(req: SearchRequest):
    try:
        origin_image_path = None
        if req.origin_image:
            from deltas.media import resolve as resolve_media

            path = resolve_media(MEDIA_DIR, req.origin_image)
            if path:
                origin_image_path = str(path)

        result = await query_engine.search(
            origin=req.origin,
            origin_ids=req.origin_ids,
            origin_image=origin_image_path,
            radii=req.radii,
            radius=req.radius,
            session_id=req.session_id,
            tags_include=req.tags_include,
            tags_exclude=req.tags_exclude,
            modality=req.modality,
            create_subset=req.create_subset,
            subset_id=req.subset_id,
            limit=req.limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    retrievals.fire_and_forget(len(result.results))
    return result


@app.post("/search/image", response_model=SearchResult)
async def search_by_image(
    file: UploadFile = File(...),  # noqa: B008
    origin: str = Form(""),
    radii_semantic: float = Form(1.5),
    radii_temporal: float = Form(2.0),
    radii_provenance: float = Form(2.0),
):
    from deltas.media import ingest

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    media_hash = await asyncio.to_thread(ingest, MEDIA_DIR, image_bytes)
    from deltas.media import resolve as resolve_media

    img_path = resolve_media(MEDIA_DIR, media_hash)

    result = await query_engine.search(
        origin=origin or None,
        origin_image=str(img_path) if img_path else None,
        radii=DimensionWeights(
            temporal=radii_temporal,
            semantic=radii_semantic,
            provenance=radii_provenance,
        ),
    )
    retrievals.fire_and_forget(len(result.results))
    return result


# ── Routes: Feed ─────────────────────────────────────────────────────────────
#
# Feed stories are written directly into the lake as `feed-story` deltas by
# the `feed0001` heartbeat routine. No server-side clustering or rendering
# — the read endpoint below just returns the deltas, newest first.


@app.get("/feed/stories")
async def feed_stories(
    layer: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Query individual feed-story deltas, newest first."""
    tags = ["feed-story"]
    if layer:
        tags.append(layer)
    results = await store.query(
        tags_include=tags,
        source="fathom-feed",
        limit=limit,
        offset=offset,
    )
    stories = []
    for r in results:
        try:
            data = json.loads(r["content"])
            data["id"] = r["id"]
            data["timestamp"] = r["timestamp"]
            stories.append(data)
        except (json.JSONDecodeError, TypeError):
            continue
    return {"stories": stories, "has_more": len(results) == limit}


# ── Routes: Drift ────────────────────────────────────────────────────────────


class DriftRequest(_BaseModel):
    text: str
    since: str


async def _compute_lake_centroid(tags_include: list[str] | None = None):
    """Return (embedded_list, centroid_unit_vec | None).

    Exponentially-decayed weighted mean of all embedded delta vectors,
    7-day half-life, L2-normalized. Shared by /drift (which also needs
    the raw embedded list for the new-delta count) and /centroid.

    `tags_include` scopes the centroid to a subset of deltas — used by
    the feed-orient crystal to anchor on engagement-tagged deltas only.
    Same semantics as store.query's tags_include (AND across tags).
    """
    import numpy as np
    from datetime import UTC, datetime

    if tags_include:
        all_deltas = await store.query(tags_include=tags_include, limit=5000)
    else:
        all_deltas = await store.query(limit=5000)
    embedded = [d for d in all_deltas if d.get("embedding")]
    if not embedded:
        return embedded, None

    now_ts = datetime.now(UTC).timestamp()
    half_life_sec = 7 * 24 * 3600
    decay = np.log(2) / half_life_sec

    embeddings = np.array([d["embedding"] for d in embedded], dtype=np.float32)
    timestamps = []
    for d in embedded:
        try:
            ts = d["timestamp"].replace("Z", "+00:00")
            timestamps.append(datetime.fromisoformat(ts).timestamp())
        except Exception:
            timestamps.append(now_ts)

    ages = np.array([now_ts - t for t in timestamps], dtype=np.float32)
    weights = np.exp(-decay * ages)
    weights = weights / weights.sum()

    centroid = (embeddings.T @ weights).astype(np.float32)
    norm = np.linalg.norm(centroid)
    if norm > 0:
        centroid = centroid / norm
    return embedded, centroid


@app.post("/drift")
async def drift(req: DriftRequest):
    import numpy as np

    from deltas.embedder import embed_text as _embed

    anchor_emb = await asyncio.to_thread(_embed, req.text)
    anchor_arr = np.array(anchor_emb, dtype=np.float32)
    anchor_norm = np.linalg.norm(anchor_arr)
    if anchor_norm > 0:
        anchor_arr = anchor_arr / anchor_norm

    embedded, centroid = await _compute_lake_centroid()
    if centroid is None:
        return {"drift": 0.0, "new_deltas": 0, "total_deltas": 0}

    new_count = sum(1 for d in embedded if d["timestamp"] > req.since)
    cos_dist = float(1.0 - np.dot(anchor_arr, centroid))

    return {
        "drift": round(cos_dist, 4),
        "new_deltas": new_count,
        "total_deltas": len(embedded),
    }


@app.get("/centroid")
async def centroid(tags_include: str | None = None):
    """Return the raw decayed lake centroid vector.

    Used by consumer-fathom to snapshot an "anchor" at crystal-write time —
    drift is then the distance this anchor has drifted from the current
    centroid, independent of the crystal's own text embedding.

    `tags_include` is a comma-separated tag list. When provided, the
    centroid is computed over only the deltas matching all those tags
    (the feed-orient crystal uses this to anchor on `feed-engagement`
    deltas only).
    """
    tag_filter = [t.strip() for t in (tags_include or "").split(",") if t.strip()] or None
    embedded, vec = await _compute_lake_centroid(tag_filter)
    if vec is None:
        return {"centroid": None, "dim": 0, "total_deltas": 0}
    return {
        "centroid": [float(x) for x in vec.tolist()],
        "dim": int(vec.shape[0]),
        "total_deltas": len(embedded),
    }


# ── Routes: Backup / Export / Import ─────────────────────────────────────────


@app.post("/backup")
async def backup_db():
    """Create a JSONL backup of the delta store."""
    import io

    buf = io.StringIO()
    count = 0
    async for delta in store.export_iter():
        buf.write(json.dumps(delta, ensure_ascii=False) + "\n")
        count += 1

    from datetime import UTC, datetime

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_path = MEDIA_DIR.parent / f"deltas-backup-{ts}.jsonl"
    backup_path.write_text(buf.getvalue())
    size_bytes = backup_path.stat().st_size

    return {
        "path": str(backup_path),
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 1),
        "count": count,
    }


@app.get("/admin/backup/state", response_model=BackupStateOut)
async def admin_backup_state():
    """Current backup writer state plus file inventory."""
    state = backup.load_state()
    inv = backup.inventory()
    return BackupStateOut(
        **state,
        rotation=[BackupFile(**f) for f in inv["rotation"]],
        quarantine=[BackupFile(**f) for f in inv["quarantine"]],
        daily=[BackupFile(**f) for f in inv["daily"]],
    )


@app.post("/admin/backup/ack", response_model=BackupAckResult)
async def admin_backup_ack(req: BackupAckRequest):
    """Clear lockdown — promote latest quarantined dump to rotation,
    re-anchor baseline at the current live delta count.
    """
    from deltas.db import _pool

    if _pool is None:
        raise HTTPException(status_code=503, detail="DB pool not ready")
    try:
        result = await backup.ack(_pool, discard_quarantine=req.discard_quarantine)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return BackupAckResult(**result)


@app.get("/export")
async def export_deltas(
    time_start: str | None = None,
    time_end: str | None = None,
    tags_include: list[str] | None = Query(None),  # noqa: B008
    source: str | None = None,
):
    async def generate():
        async for delta in store.export_iter(
            time_start=time_start,
            time_end=time_end,
            tags_include=tags_include,
            source=source,
        ):
            yield json.dumps(delta, ensure_ascii=False) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=deltas-export.jsonl"},
    )


class ImportResult(_BaseModel):
    written: int
    skipped: int
    errors: int


@app.post("/import", response_model=ImportResult)
async def import_deltas(
    file: UploadFile = File(...),  # noqa: B008
    skip_duplicates: bool = Form(True),
):
    content = await file.read()
    deltas = []
    for line in content.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            deltas.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not deltas:
        raise HTTPException(status_code=400, detail="No valid JSONL lines found")

    result = await store.import_batch(deltas, skip_duplicates=skip_duplicates)
    return result


# ── Routes: Meta ─────────────────────────────────────────────────────────────


@app.get("/sources")
async def list_sources():
    return await store.sources()


@app.get("/tags")
async def list_tags():
    return await store.tags()


@app.get("/stats")
async def stats():
    return await store.embedding_stats()


@app.get("/stats/retrievals/history")
async def retrievals_history(since_seconds: int = 7 * 24 * 3600, buckets: int = 60):
    """Bucketed count of deltas retrieved from the lake across the window."""
    items = await retrievals.history(since_seconds=since_seconds, buckets=buckets)
    return {"history": items}


class PressureHistoryIn(_BaseModel):
    since_seconds: int = 7 * 24 * 3600
    buckets: int = 60
    weights: dict[str, float]
    default_weight: float
    user_tag_boost: float
    half_life_seconds: int


@app.post("/stats/pressure/history")
async def pressure_history(req: PressureHistoryIn):
    """Bucketed weighted-decay pressure curve, computed entirely in SQL.

    The consumer-api owns the source-weight policy and passes it in;
    this endpoint applies those weights against every delta in the
    window — no row-limit truncation.
    """
    from datetime import UTC, datetime, timedelta

    if req.since_seconds <= 0 or req.buckets <= 0:
        return {"history": []}
    rows = await store.pressure_history(
        since_seconds=req.since_seconds,
        buckets=req.buckets,
        weights=req.weights,
        default_weight=req.default_weight,
        user_tag_boost=req.user_tag_boost,
        half_life_seconds=req.half_life_seconds,
    )
    by_bucket = {b: v for b, v in rows}
    now = datetime.now(UTC)
    start = now - timedelta(seconds=req.since_seconds)
    bucket_seconds = req.since_seconds / req.buckets
    out: list[dict] = []
    for i in range(req.buckets):
        tick = start + timedelta(seconds=bucket_seconds * (i + 0.5))
        out.append({"t": tick.isoformat(), "v": float(by_bucket.get(i, 0.0))})
    return {"history": out}


class PressureVolumeIn(_BaseModel):
    cutoff_ts: str | None = None
    window_seconds: int
    weights: dict[str, float]
    default_weight: float
    user_tag_boost: float
    half_life_seconds: int


@app.post("/stats/pressure/volume")
async def pressure_volume(req: PressureVolumeIn):
    """Single-value weighted-decay pressure since cutoff (or window)."""
    v = await store.pressure_volume(
        cutoff_ts=req.cutoff_ts,
        window_seconds=req.window_seconds,
        weights=req.weights,
        default_weight=req.default_weight,
        user_tag_boost=req.user_tag_boost,
        half_life_seconds=req.half_life_seconds,
    )
    return {"volume": v}


@app.get("/stats/usage/history")
async def usage_history(since_seconds: int = 24 * 3600, buckets: int = 60):
    """Bucketed count of deltas written into the lake across the window.

    Bucketing is done in SQL so the result is not subject to any row-limit
    truncation — every delta in the window is counted.
    """
    from datetime import UTC, datetime, timedelta

    if since_seconds <= 0 or buckets <= 0:
        return {"history": []}
    rows = await store.usage_history(since_seconds=since_seconds, buckets=buckets)
    by_bucket = {b: c for b, c in rows}
    now = datetime.now(UTC)
    start = now - timedelta(seconds=since_seconds)
    bucket_seconds = since_seconds / buckets
    out: list[dict] = []
    for i in range(buckets):
        tick = start + timedelta(seconds=bucket_seconds * (i + 0.5))
        out.append({"t": tick.isoformat(), "v": int(by_bucket.get(i, 0))})
    return {"history": out}


# ── Routes: Crystal facets ───────────────────────────────────────────────────


class FacetsIn(_BaseModel):
    facets: list[dict]


@app.post("/hooks/activation/facets")
async def set_crystal_facets(req: FacetsIn):
    global _facet_labels, _facet_texts, _facet_embeddings
    from deltas.embedder import embed_texts

    if not req.facets:
        _facet_labels, _facet_texts, _facet_embeddings = [], [], []
        return {"count": 0}

    labels = [f["label"] for f in req.facets]
    texts = [f["text"] for f in req.facets]
    embeddings = await asyncio.to_thread(embed_texts, texts)

    _facet_labels = labels
    _facet_texts = texts
    _facet_embeddings = embeddings

    log.info("Crystal facets embedded: %d facets", len(labels))
    return {"count": len(labels)}


@app.get("/hooks/activation/facets")
async def list_crystal_facets():
    return {
        "facets": [
            {"label": _facet_labels[i], "text": _facet_texts[i][:200]}
            for i in range(len(_facet_labels))
        ]
    }


class ResonanceSourcesIn(_BaseModel):
    allowed: list[str]


@app.get("/hooks/activation/sources")
async def get_resonance_sources():
    return {"allowed": sorted(_resonance_allowed)}


@app.post("/hooks/activation/sources")
async def set_resonance_sources(req: ResonanceSourcesIn):
    global _resonance_allowed
    _resonance_allowed = {str(s) for s in req.allowed}
    _save_resonance()
    log.info("Resonance allow-list updated: %d sources", len(_resonance_allowed))
    return {"allowed": sorted(_resonance_allowed)}


# ── Routes: Plan (new) ──────────────────────────────────────────────────────


@app.post("/plan", response_model=PlanResponse)
async def execute_plan(req: PlanRequest):
    """Execute a compositional query plan against the delta lake."""
    try:
        result = await plan_executor.execute(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    total = 0
    for step in (result.steps or {}).values():
        total += int(getattr(step, "count", 0) or 0)
    retrievals.fire_and_forget(total)
    return result


# ── Routes: Contacts & Handles ──────────────────────────────────────────────


@app.get("/contacts", response_model=list[ContactOut])
async def list_contacts():
    return await contacts_store.list_all()


@app.post("/contacts", response_model=ContactOut)
async def create_contact(req: ContactIn):
    """Register a slug. Soft fields (display_name, role, …) are written
    separately by the caller as a `profile + contact:<slug>` delta —
    the contacts registry just tracks that the slug exists."""
    try:
        return await contacts_store.create(slug=req.slug)
    except asyncpg.UniqueViolationError as e:
        raise HTTPException(status_code=409, detail=f"Contact '{req.slug}' already exists") from e


@app.get("/contacts/{slug}", response_model=ContactOut)
async def get_contact(slug: str, include_disabled: bool = False):
    c = await contacts_store.get(slug, include_disabled=include_disabled)
    if not c:
        raise HTTPException(status_code=404, detail="Contact not found")
    return c


@app.post("/contacts/{slug}/disable", response_model=ContactOut)
async def disable_contact(slug: str):
    """Soft-delete. Tombstone via disabled_at timestamp. Callers are
    expected to also write a `contact-deleted + contact:<slug>` delta
    for lake-side provenance."""
    ok = await contacts_store.disable(slug)
    if not ok:
        existing = await contacts_store.get(slug, include_disabled=True)
        if not existing:
            raise HTTPException(status_code=404, detail="Contact not found")
        return existing
    return await contacts_store.get(slug, include_disabled=True)


@app.post("/contacts/{slug}/reenable", response_model=ContactOut)
async def reenable_contact(slug: str):
    ok = await contacts_store.reenable(slug)
    if not ok:
        raise HTTPException(status_code=404, detail="Contact not found")
    return await contacts_store.get(slug)


@app.get("/contacts/{slug}/handles", response_model=list[HandleOut])
async def list_handles(slug: str):
    if not await contacts_store.get(slug):
        raise HTTPException(status_code=404, detail="Contact not found")
    return await contacts_store.list_handles(slug)


@app.post("/contacts/{slug}/handles", response_model=HandleOut)
async def add_handle(slug: str, req: HandleIn):
    if not await contacts_store.get(slug):
        raise HTTPException(status_code=404, detail="Contact not found")
    try:
        return await contacts_store.add_handle(slug, req.channel, req.identifier)
    except asyncpg.UniqueViolationError as e:
        raise HTTPException(
            status_code=409,
            detail=f"Handle ({req.channel}, {req.identifier}) already bound to another contact",
        ) from e


@app.delete("/contacts/{slug}/handles")
async def remove_handle(slug: str, req: HandleIn):
    ok = await contacts_store.remove_handle(slug, req.channel, req.identifier)
    if not ok:
        raise HTTPException(status_code=404, detail="Handle not found")
    return {"deleted": {"channel": req.channel, "identifier": req.identifier}}


@app.get("/handles/resolve", response_model=ResolvedHandle)
async def resolve_handle(channel: str, identifier: str):
    slug = await contacts_store.resolve_handle(channel, identifier)
    return {"contact_slug": slug}


class BackfillContactTagIn(_BaseModel):
    contact_slug: str
    filter_tags: list[str]


@app.post("/admin/backfill-contact-tag")
async def backfill_contact_tag(req: BackfillContactTagIn):
    """One-shot migration: add `contact:<slug>` to legacy per-user deltas
    that predate the contact registry (feed-engagement, feed-story,
    crystal:feed-orient, etc.). Skips deltas that already carry any
    `contact:` tag, so re-running is a no-op.
    """
    if not req.filter_tags:
        raise HTTPException(status_code=400, detail="filter_tags is required")
    if not await contacts_store.get(req.contact_slug):
        raise HTTPException(
            status_code=404, detail=f"Contact '{req.contact_slug}' not found"
        )
    return await contacts_store.backfill_contact_tag(
        req.contact_slug, req.filter_tags
    )


@app.get("/health")
async def health():
    count = await store.count()
    return {"status": "ok", "deltas": count}
