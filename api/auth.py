"""Token-based auth for the consumer API.

Tokens are stored as SHA-256 hashes in a JSON file on disk. The raw token
is only ever visible at creation time. Lookup is O(n) over the token list,
which is fine for the expected scale (< 100 tokens per user).

Token format: fth_<40 chars of base62>
"""
from __future__ import annotations

import hashlib
import json
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .settings import settings

ALPHABET = string.ascii_letters + string.digits
TOKEN_PREFIX = "fth_"
TOKEN_RAND_LEN = 40

# Endpoints that don't require auth
PUBLIC_PATHS = frozenset({
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
})

# Path prefixes that are always public
PUBLIC_PREFIXES = (
    "/docs",
)


def _tokens_path() -> Path:
    return Path(settings.tokens_path)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _load() -> list[dict]:
    p = _tokens_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _save(tokens: list[dict]) -> None:
    p = _tokens_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tokens, indent=2))


# ── CRUD ──────────────────────────────────────────


def create_token(name: str = "") -> dict:
    """Create a new token. Returns the full token (only time it's visible)."""
    raw = TOKEN_PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_RAND_LEN))
    token_hash = _hash(raw)
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "id": token_hash[:12],
        "name": name or "Unnamed token",
        "hash": token_hash,
        "prefix": raw[:8] + "…",
        "created_at": now,
        "last_used_at": None,
    }
    tokens = _load()
    tokens.append(record)
    _save(tokens)
    return {"token": raw, **{k: v for k, v in record.items() if k != "hash"}}


def list_tokens() -> list[dict]:
    """List all tokens (without hashes)."""
    return [
        {k: v for k, v in t.items() if k != "hash"}
        for t in _load()
    ]


def delete_token(token_id: str) -> bool:
    """Revoke a token by its short ID."""
    tokens = _load()
    before = len(tokens)
    tokens = [t for t in tokens if t["id"] != token_id]
    if len(tokens) == before:
        return False
    _save(tokens)
    return True


def validate(raw: str) -> dict | None:
    """Validate a raw token. Returns the record (without hash) or None."""
    token_hash = _hash(raw)
    tokens = _load()
    for t in tokens:
        if t["hash"] == token_hash:
            # Update last_used_at
            t["last_used_at"] = datetime.now(timezone.utc).isoformat()
            _save(tokens)
            return {k: v for k, v in t.items() if k != "hash"}
    return None


# ── Middleware ─────────────────────────────────────


def auth_required() -> bool:
    """Check if auth is enabled (any tokens exist)."""
    return len(_load()) > 0


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Require Bearer token on all non-public endpoints.

    Auth is only enforced when at least one token exists. Before any
    tokens are created, the API is open (first-run experience).
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Public endpoints are always open
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # OPTIONS (CORS preflight) is always open
        if request.method == "OPTIONS":
            return await call_next(request)

        # If no tokens exist yet, everything is open (first-run)
        if not auth_required():
            return await call_next(request)

        # Check Authorization header
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": "Missing or invalid Authorization header"},
            )

        raw_token = auth_header[7:]
        token_info = validate(raw_token)
        if not token_info:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid token"},
            )

        # Attach token info to request state
        request.state.token = token_info
        return await call_next(request)
