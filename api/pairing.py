"""Pair-code onboarding for new agents.

Flow:
  1. User clicks "Add machine" in the dashboard → POST /v1/pair
     mints a short-lived single-use code (format: `pair_<26 chars>`)
  2. User copies the generated install command (code baked in) and runs
     it on the target machine: `npx fathom-agent init --pair-code pair_...`
  3. Agent POSTs /v1/pair/redeem {code, host} → server validates,
     revokes any previous token for this host, mints a fresh API token,
     returns it to the agent
  4. Agent writes the token to agent.json, starts heartbeating normally

Pair codes live on disk next to tokens.json, time-bounded (10 min default)
and single-use. Re-running step 2 on the same host with a new code
rotates the key — no separate "rotate" endpoint needed; pairing serves
both first-install and key-rotation.
"""
from __future__ import annotations

import json
import secrets
import string
import time
from pathlib import Path

from .auth import ALL_SCOPES, _hash, _load as _load_tokens, _save as _save_tokens
from .settings import settings

ALPHABET = string.ascii_lowercase + string.digits
PAIR_PREFIX = "pair_"
PAIR_RAND_LEN = 26
DEFAULT_TTL_SECONDS = 600  # 10 min
# Default scopes granted to a paired agent. Agents don't need tokens:manage;
# they push deltas, read the lake, and chat. Keeping tokens:manage off
# prevents a compromised agent from minting replacement tokens for itself.
AGENT_SCOPES = ["lake:read", "lake:write", "chat"]


def _path() -> Path:
    return Path(settings.pair_codes_path)


def _load() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _save(codes: list[dict]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(codes, indent=2))


def _now() -> int:
    return int(time.time())


def _prune(codes: list[dict]) -> list[dict]:
    """Drop codes older than 24h regardless of used/unused — keeps the file
    from growing unbounded for long-running deployments."""
    cutoff = _now() - 86400
    return [c for c in codes if c.get("created_at", 0) >= cutoff]


def create_pair_code(ttl_seconds: int = DEFAULT_TTL_SECONDS, note: str = "") -> dict:
    """Mint a new single-use pair code with a short TTL.

    `note` is optional metadata (e.g. "for laptop, jordan") so the user can
    see context in the dashboard if they somehow end up with multiple open
    pair codes at once.
    """
    raw = PAIR_PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(PAIR_RAND_LEN))
    now = _now()
    record = {
        "code": raw,
        "created_at": now,
        "expires_at": now + max(60, ttl_seconds),
        "used_at": None,
        "note": note or "",
    }
    codes = _prune(_load())
    codes.append(record)
    _save(codes)
    return record


def redeem_pair_code(code: str, host: str = "") -> dict:
    """Validate and consume a pair code. Returns a freshly minted token.

    Raises ValueError with a short machine-readable reason on failure —
    callers (the HTTP endpoint) translate into 400/401/410 as appropriate.
    Revokes any existing tokens named `agent:<host>` before minting, so
    re-pairing effectively rotates the key for that host.
    """
    now = _now()
    codes = _prune(_load())

    target = None
    for c in codes:
        if c["code"] == code:
            target = c
            break

    if target is None:
        _save(codes)
        raise ValueError("unknown_code")
    if target["used_at"] is not None:
        raise ValueError("already_redeemed")
    if target["expires_at"] < now:
        raise ValueError("expired")

    target["used_at"] = now
    target["used_by_host"] = host or None
    _save(codes)

    # Revoke any previous tokens for this host so re-pairing rotates the
    # key rather than leaving a dangling old token valid. Token name is
    # the convention: `agent:<host>`. If the user named tokens differently
    # by hand, those aren't touched.
    if host:
        token_name = f"agent:{host}"
        tokens = _load_tokens()
        kept = [t for t in tokens if t.get("name") != token_name]
        if len(kept) != len(tokens):
            _save_tokens(kept)
        tokens = kept
    else:
        tokens = _load_tokens()

    # Mint the new token directly into tokens.json with the agent-default
    # scopes. auth.create_token uses the settings path, which is the same
    # file; importing through auth ensures the format matches.
    from .auth import TOKEN_PREFIX, TOKEN_RAND_LEN, ALPHABET as TOKEN_ALPHABET
    from datetime import datetime, timezone

    token_raw = TOKEN_PREFIX + "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_RAND_LEN))
    token_hash = _hash(token_raw)
    nowiso = datetime.now(timezone.utc).isoformat()
    record = {
        "id": token_hash[:12],
        "name": f"agent:{host}" if host else "agent:paired",
        "hash": token_hash,
        "prefix": token_raw[:8] + "…",
        "scopes": list(AGENT_SCOPES),
        "created_at": nowiso,
        "last_used_at": None,
    }
    tokens.append(record)
    _save_tokens(tokens)

    return {
        "token": token_raw,
        "scopes": list(AGENT_SCOPES),
        "host": host,
        "token_id": record["id"],
    }


def list_active_codes() -> list[dict]:
    """Codes still redeemable — not used, not expired. For the dashboard's
    'pending codes' display so the user can see outstanding admissions."""
    now = _now()
    codes = _prune(_load())
    out = []
    for c in codes:
        if c["used_at"] is not None:
            continue
        if c["expires_at"] < now:
            continue
        out.append({
            "code": c["code"],
            "expires_at": c["expires_at"],
            "seconds_remaining": max(0, c["expires_at"] - now),
            "note": c.get("note", ""),
        })
    return out
