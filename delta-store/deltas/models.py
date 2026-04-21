"""Pydantic models for the delta store API.

Preserves all v1 model shapes for backward compatibility.
Adds plan request/response models for compositional queries.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── Delta CRUD ──────────────────────────────────────────────────────────────


class DeltaIn(BaseModel):
    content: str
    modality: str = "text"
    tags: list[str] = Field(default_factory=list)
    timestamp: str | None = None
    id: str | None = None
    embedding: list[float] | None = None
    provenance_embedding: list[float] | None = None
    source: str = "unknown"
    media_hash: str | None = None
    expires_at: str | None = None


class DeltaOut(BaseModel):
    id: str
    timestamp: str
    modality: str
    content: str
    embedding: list[float]
    provenance_embedding: list[float]
    source: str
    tags: list[str]
    media_hash: str | None = None
    expires_at: str | None = None


class DeltaSlim(BaseModel):
    """Delta without embedding vectors."""

    id: str
    timestamp: str
    modality: str
    content: str
    source: str
    tags: list[str]
    media_hash: str | None = None
    expires_at: str | None = None


class BatchIn(BaseModel):
    deltas: list[DeltaIn]


class WriteResult(BaseModel):
    id: str
    media_hash: str | None = None


class BatchResult(BaseModel):
    count: int


# ── Backup ──────────────────────────────────────────────────────────────────


class BackupFile(BaseModel):
    path: str
    size: int
    mtime: str


class BackupStateOut(BaseModel):
    state: str
    last_attempt_at: str | None = None
    last_healthy_at: str | None = None
    last_good_path: str | None = None
    last_good_size: int | None = None
    last_good_delta_count: int | None = None
    last_reason: str | None = None
    rotation: list[BackupFile] = Field(default_factory=list)
    quarantine: list[BackupFile] = Field(default_factory=list)
    daily: list[BackupFile] = Field(default_factory=list)


class BackupAckRequest(BaseModel):
    discard_quarantine: bool = False


class BackupAckResult(BaseModel):
    state: str
    promoted_path: str | None = None
    discarded_count: int = 0


# ── Search ──────────────────────────────────────────────────────────────────


class DimensionWeights(BaseModel):
    temporal: float = 1.0
    semantic: float = 1.0
    provenance: float = 1.0


class SearchRequest(BaseModel):
    session_id: str | None = None
    origin: str | None = None
    origin_ids: list[str] | None = None
    origin_image: str | None = None
    radii: DimensionWeights = Field(default_factory=DimensionWeights)
    radius: float = 0.7
    tags_include: list[str] | None = None
    tags_exclude: list[str] | None = None
    modality: str | None = None
    create_subset: bool = False
    subset_id: str | None = None
    limit: int = 50


class ScoredDelta(BaseModel):
    delta: DeltaOut
    distance: float
    dimensions: DimensionWeights


class ScoredDeltaSlim(BaseModel):
    """Scored delta without embedding vectors."""

    delta: DeltaSlim
    distance: float
    dimensions: DimensionWeights


class SearchResult(BaseModel):
    session_id: str
    full: bool
    results: list[ScoredDeltaSlim]
    added: list[ScoredDeltaSlim]
    removed: list[str]
    total_relevant: int | None = None
    origin_radius: float | None = None
    subset_id: str | None = None
    subset_size: int | None = None


# ── Plan ─────────────────────────────────────────────────────────────────────


class PlanRadii(BaseModel):
    semantic: float = 1.0
    temporal_hours: float | None = None
    provenance: float = 1.0


class PlanStep(BaseModel):
    """A single step in a query plan.

    Exactly one of the action fields must be set:
    - search: semantic search by text
    - filter: structured filter (time/tags/source)
    - intersect / union / diff: set operations on two step IDs
    - bridge: find deltas close to two step centroids
    - aggregate: group by time bucket / tag / source
    - chain: search outward from a previous step's centroid
    """

    id: str
    # Actions (exactly one must be set)
    search: str | None = None
    filter: dict | None = None
    intersect: list[str] | None = None
    union: list[str] | None = None
    diff: list[str] | None = None
    bridge: list[str] | None = None
    aggregate: str | None = None
    chain: str | None = None
    # Parameters
    radii: PlanRadii | None = None
    tags_include: list[str] | None = None
    tags_exclude: list[str] | None = None
    modality: str | None = None
    source: str | None = None
    time_start: str | None = None
    time_end: str | None = None
    group_by: str | None = None  # "week", "day", "month", "tag", "source"
    metric: str | None = None  # "count", "centroid"
    limit: int = 100


class PlanRequest(BaseModel):
    steps: list[PlanStep]


class StepResultDeltas(BaseModel):
    """Result for a step that produces a set of deltas."""

    count: int
    deltas: list[DeltaSlim]


class AggBucket(BaseModel):
    """A single aggregation bucket."""

    bucket: str
    count: int
    delta_ids: list[str] = Field(default_factory=list)


class StepResultAggregate(BaseModel):
    """Result for an aggregation step."""

    buckets: list[AggBucket]


class PlanResponse(BaseModel):
    steps: dict[str, StepResultDeltas | StepResultAggregate]
    timing_ms: float
    warnings: list[str] = Field(default_factory=list)


# ── Contacts & Handles ─────────────────────────────────────────────────────
#
# The contacts table is a thin registry: slug + created_at + disabled_at.
# Everything soft (display_name, role, pronouns, bio, avatar, timezone,
# language, aliases) lives in a `profile + contact:<slug>` delta,
# latest-wins, and is merged on the consumer-api side. These models
# reflect only the registry shape.


class ContactIn(BaseModel):
    slug: str


class ContactOut(BaseModel):
    slug: str
    created_at: str
    disabled_at: str | None = None


class HandleIn(BaseModel):
    channel: str
    identifier: str


class HandleOut(BaseModel):
    contact_slug: str
    channel: str
    identifier: str
    created_at: str


class ResolvedHandle(BaseModel):
    contact_slug: str | None
