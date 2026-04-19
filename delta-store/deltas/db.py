"""Connection pool management and schema initialization.

Owns the asyncpg pool lifecycle. Registers pgvector codecs on every
new connection so embedding columns round-trip as Python lists.
"""

from __future__ import annotations

import os

import asyncpg
from pgvector.asyncpg import register_vector

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://fathom:fathom@localhost:5432/deltas")

VECTOR_DIM = 512

DDL_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS deltas (
    id                   TEXT PRIMARY KEY,
    timestamp            TIMESTAMPTZ NOT NULL,
    modality             TEXT NOT NULL,
    content              TEXT NOT NULL,
    embedding            vector({VECTOR_DIM}),
    provenance_embedding vector({VECTOR_DIM}),
    source               TEXT NOT NULL DEFAULT 'unknown',
    tags                 TEXT[] NOT NULL DEFAULT '{{}}',
    media_hash           TEXT,
    expires_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_deltas_timestamp ON deltas (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_deltas_source ON deltas (source);
CREATE INDEX IF NOT EXISTS idx_deltas_tags ON deltas USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_deltas_expires ON deltas (expires_at)
    WHERE expires_at IS NOT NULL;
"""

# HNSW indexes are expensive to create and can't use IF NOT EXISTS before pg17.
# We create them separately and swallow "already exists" errors.
HNSW_INDEXES = [
    (
        "idx_deltas_embedding",
        "CREATE INDEX idx_deltas_embedding ON deltas "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)",
    ),
    (
        "idx_deltas_prov_embedding",
        "CREATE INDEX idx_deltas_prov_embedding ON deltas "
        "USING hnsw (provenance_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)",
    ),
]

SCHEMA_VERSION = "1"

_pool: asyncpg.Pool | None = None


async def _setup_connection(conn: asyncpg.Connection) -> None:
    """Called on every new connection in the pool."""
    await register_vector(conn)


async def init_pool(dsn: str | None = None) -> asyncpg.Pool:
    """Create the connection pool and ensure the schema exists."""
    global _pool
    url = dsn or DATABASE_URL

    # The pool's setup hook calls register_vector, which introspects the
    # `vector` type. That type only exists after CREATE EXTENSION runs, so
    # bootstrap the extension on a one-off connection before opening the pool.
    bootstrap = await asyncpg.connect(url)
    try:
        await bootstrap.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await bootstrap.close()

    _pool = await asyncpg.create_pool(url, min_size=2, max_size=10, setup=_setup_connection)

    async with _pool.acquire() as conn:
        await conn.execute(DDL_SQL)

        # Create HNSW indexes (swallow "already exists")
        for _name, ddl in HNSW_INDEXES:
            try:
                await conn.execute(ddl)
            except (asyncpg.DuplicateObjectError, asyncpg.DuplicateTableError):
                pass

        # Schema version tracking
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', $1) "
            "ON CONFLICT (key) DO NOTHING",
            SCHEMA_VERSION,
        )

    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    assert _pool is not None, "Pool not initialized — call init_pool() first"
    return _pool
