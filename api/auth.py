"""Token-based auth with per-token scopes for the consumer API.

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

# ── Scopes ────────────────────────────────────────

ALL_SCOPES = {
    "lake:read": "Search, query, view deltas, stats, and tags",
    "lake:write": "Write new deltas to the lake",
    "sources:manage": "Add, remove, and configure sources",
    "tokens:manage": "Create and revoke API tokens",
    "chat": "Use chat completions",
}

DEFAULT_SCOPES = list(ALL_SCOPES.keys())  # new tokens get everything

# Map route patterns → required scope
ROUTE_SCOPES: list[tuple[str, str, str]] = [
    # (method, path_prefix, required_scope)
    ("POST", "/v1/search", "lake:read"),
    ("GET", "/v1/deltas", "lake:read"),
    ("GET", "/v1/tags", "lake:read"),
    ("GET", "/v1/stats", "lake:read"),
    ("POST", "/v1/plan", "lake:read"),
    ("POST", "/v1/deltas", "lake:write"),
    ("GET", "/v1/sources", "sources:manage"),
    ("POST", "/v1/sources", "sources:manage"),
    ("PUT", "/v1/sources", "sources:manage"),
    ("DELETE", "/v1/sources", "sources:manage"),
    ("GET", "/v1/tokens", "tokens:manage"),
    ("POST", "/v1/tokens", "tokens:manage"),
    ("DELETE", "/v1/tokens", "tokens:manage"),
    # Minting pair codes needs the same scope as minting tokens — it's an
    # admission-token flow that will mint a real token on redemption.
    ("POST", "/v1/pair", "tokens:manage"),
    ("GET", "/v1/pair", "tokens:manage"),
    ("POST", "/v1/chat", "chat"),
    ("GET", "/v1/sessions", "chat"),
    ("POST", "/v1/sessions", "chat"),
    ("GET", "/v1/feed", "lake:read"),
    ("GET", "/v1/usage", "lake:read"),
    ("GET", "/v1/media", "lake:read"),
    ("POST", "/v1/media", "lake:write"),
    ("POST", "/v1/crystal", "lake:write"),
    ("GET", "/v1/moods", "lake:read"),
    ("POST", "/v1/moods", "lake:write"),
    ("GET", "/v1/pressure", "lake:read"),
    ("GET", "/v1/drift", "lake:read"),
    ("GET", "/v1/crystal/events", "lake:read"),
    ("GET", "/v1/recall", "lake:read"),
    ("GET", "/v1/agents", "lake:read"),
    ("GET", "/v1/routines", "lake:read"),
    ("POST", "/v1/routines", "lake:write"),
    ("PUT", "/v1/routines", "lake:write"),
    ("DELETE", "/v1/routines", "lake:write"),
]

# Endpoints that don't require auth
PUBLIC_PATHS = frozenset({
    "/",
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/v1/models",
    "/v1/tools",
    "/v1/scopes",
    # Pair-code redemption is the agent's path to its first token, so it
    # can't require one. Mint (POST /v1/pair) is NOT public — that still
    # requires tokens:manage.
    "/v1/pair/redeem",
    # npm registry lookup; just public metadata. Dashboard calls this
    # before the user has a token.
    "/v1/agents/latest-version",
})

PUBLIC_PREFIXES = (
    "/docs",
    "/ui",
)


def _required_scope(method: str, path: str) -> str | None:
    """Find the required scope for a method + path, or None if unrestricted."""
    for route_method, route_prefix, scope in ROUTE_SCOPES:
        if method == route_method and path.startswith(route_prefix):
            return scope
    return None


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


def create_token(name: str = "", scopes: list[str] | None = None) -> dict:
    """Create a new token. Returns the full token (only time it's visible)."""
    raw = TOKEN_PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_RAND_LEN))
    token_hash = _hash(raw)
    now = datetime.now(timezone.utc).isoformat()

    # Validate and default scopes
    granted = scopes if scopes is not None else DEFAULT_SCOPES
    granted = [s for s in granted if s in ALL_SCOPES]
    if not granted:
        granted = DEFAULT_SCOPES

    record = {
        "id": token_hash[:12],
        "name": name or "Unnamed token",
        "hash": token_hash,
        "prefix": raw[:8] + "…",
        "scopes": granted,
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
            t["last_used_at"] = datetime.now(timezone.utc).isoformat()
            _save(tokens)
            return {k: v for k, v in t.items() if k != "hash"}
    return None


def get_scopes() -> dict[str, str]:
    """Return all available scopes with descriptions."""
    return ALL_SCOPES


# ── Middleware ─────────────────────────────────────


def auth_required() -> bool:
    """Check if auth is enabled (any tokens exist)."""
    return len(_load()) > 0


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Require Bearer token on all non-public endpoints.

    Auth is only enforced when at least one token exists. Before any
    tokens are created, the API is open (first-run experience).
    Checks per-token scopes against the requested endpoint.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method

        # Public endpoints always open
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # CORS preflight always open
        if method == "OPTIONS":
            return await call_next(request)

        # No tokens exist → everything open (first-run)
        if not auth_required():
            request.state.token = None
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

        # Check scope (tokens without scopes field = legacy, grant all)
        required = _required_scope(method, path)
        if required:
            token_scopes = token_info.get("scopes") or list(ALL_SCOPES.keys())
            if required not in token_scopes:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": f"Token missing required scope: {required}",
                        "required": required,
                        "granted": token_scopes,
                    },
                )

        request.state.token = token_info
        return await call_next(request)
