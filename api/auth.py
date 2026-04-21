"""Token-based auth with per-token scopes for the consumer API.

Tokens are stored as SHA-256 hashes in a JSON file on disk. The raw token
is only ever visible at creation time. Lookup is O(n) over the token list,
which is fine for the expected scale (< 100 tokens per user).

Token format: fth_<40 chars of base62>

Each token is bound to a `contact_slug`. On successful auth the middleware
resolves that slug to the contact record from delta-store and stamps it on
`request.state.contact`, so every downstream handler can tag writes and
gate admin-only routes without re-resolving.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import string
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
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
    ("POST", "/v1/feed", "lake:write"),
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

# Paths that do NOT require auth but still get middleware-stamped contact
# when a valid token is present — used by the login UI to probe current
# identity without tripping a 401 before the user has entered a key.
AUTH_OPTIONAL_PATHS = frozenset({
    "/v1/auth/me",
})

PUBLIC_PREFIXES = (
    "/docs",
    "/ui",
    # Favicon is a browser-initiated request with no Authorization header.
    # Letting it through keeps the console clean without weakening any
    # token-gated path. Delta-store serves 404 if no file is configured.
    "/favicon",
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


def create_token(
    name: str = "",
    scopes: list[str] | None = None,
    contact_slug: str = "myra",
) -> dict:
    """Create a new token bound to a contact. Raw token only visible here."""
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
        "contact_slug": contact_slug,
        "created_at": now,
        "last_used_at": None,
    }
    tokens = _load()
    tokens.append(record)
    _save(tokens)
    return {"token": raw, **{k: v for k, v in record.items() if k != "hash"}}


def migrate_legacy_tokens(default_slug: str = "myra") -> int:
    """Bind any token missing `contact_slug` to `default_slug`.

    Legacy tokens were minted before contact-awareness. They all belong
    to the admin who first set the instance up — Myra in the default
    case, or whichever slug owns the first contact row.
    """
    tokens = _load()
    changed = 0
    for t in tokens:
        if not t.get("contact_slug"):
            t["contact_slug"] = default_slug
            changed += 1
    if changed:
        _save(tokens)
    return changed


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


# ── Contacts cache ──────────────────────────────────
# Role + display_name live in delta-store; fetching on every request is a
# cross-service round trip we don't need. Cache contact records for a
# short TTL keyed by slug. Admin mutations invalidate the entry.

_CONTACT_CACHE: dict[str, tuple[float, dict]] = {}
_CONTACT_CACHE_TTL = 60.0


async def resolve_contact(slug: str) -> dict | None:
    """Return a contact record (dict) for a slug, using a short-lived cache."""
    if not slug:
        return None
    now = time.time()
    entry = _CONTACT_CACHE.get(slug)
    if entry and (now - entry[0]) < _CONTACT_CACHE_TTL:
        return entry[1]

    from . import contacts as contacts_mod

    try:
        contact = await contacts_mod.get(slug)
    except Exception:
        contact = None
    if contact is not None:
        _CONTACT_CACHE[slug] = (now, contact)
    return contact


def invalidate_contact_cache(slug: str | None = None) -> None:
    if slug is None:
        _CONTACT_CACHE.clear()
    else:
        _CONTACT_CACHE.pop(slug, None)


def require_admin(request: Request) -> dict:
    """Raise 403 unless the authed caller is an admin. Returns the contact."""
    contact = getattr(request.state, "contact", None)
    if not contact:
        raise HTTPException(status_code=401, detail="Authentication required")
    if contact.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin role required for this action",
        )
    return contact


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

        # No tokens exist → everything open (first-run). Default the
        # caller to the seeded admin so any writes during first-run still
        # carry a contact tag; once a real token is minted this branch
        # stops firing.
        if not auth_required():
            request.state.token = None
            request.state.contact = await resolve_contact("myra")
            return await call_next(request)

        # Check Authorization header — preferred path for all clients.
        # Fallback for GET on /v1/media/* only: accept ?token=… query
        # param. <img src="…"> tags can't pass headers, so without this
        # fallback every in-lake media_hash image returns 401 in the
        # browser. Scope-narrow on purpose: only GET on the media route,
        # never write endpoints (a leaked URL with token in a referrer
        # header would otherwise grant write access).
        auth_header = request.headers.get("authorization", "")
        raw_token = ""
        if auth_header.startswith("Bearer "):
            raw_token = auth_header[7:]
        elif method == "GET" and path.startswith("/v1/media/"):
            raw_token = request.query_params.get("token", "")

        auth_optional = path in AUTH_OPTIONAL_PATHS
        if not raw_token:
            if auth_optional:
                request.state.token = None
                request.state.contact = None
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={"error": "Missing or invalid Authorization header"},
            )
        token_info = validate(raw_token)
        if not token_info:
            if auth_optional:
                request.state.token = None
                request.state.contact = None
                return await call_next(request)
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
        request.state.contact = await resolve_contact(
            token_info.get("contact_slug", "")
        )
        return await call_next(request)
