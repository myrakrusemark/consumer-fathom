"""Routine CRUD for the consumer API.

Routines live as spec deltas in the delta lake, tagged
`[spec, routine, routine-id:<id>]` with YAML frontmatter + prompt body.

Lifecycle:
  spec delta  →  (cron tick)  →  routine-fire delta
                                 (consumed by fathom-agent kitty plugin)
                                 →  claude runs prompt
                                 →  routine-summary delta linked via `fire-delta:<id>`

Every create/update/delete = write a new spec delta with the same `routine-id`
tag. Scheduler and dashboard take latest per id. Deletion = tombstone with
`deleted: true`. No deltas are ever actually removed from the lake.

See fathom2/docs/routine-spec.md for the canonical field reference.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from . import delta_client

_SPEC_KEYS_ORDER = [
    "id",
    "name",
    "schedule",
    "interval_minutes",
    "enabled",
    "workspace",
    "host",
    "permission_mode",
    "single_fire",
    "deleted",
]


# ── Frontmatter parse / render ───────────────────────────────────────────


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse `--- yaml ---` frontmatter + body."""
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end < 0:
        return {}, content
    header = content[3:end].strip()
    body = content[end + 4 :].lstrip("\n")
    meta: dict = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        val = raw.strip().strip('"').strip("'")
        if val.lower() in ("true", "false"):
            meta[key] = val.lower() == "true"
        elif val.isdigit():
            meta[key] = int(val)
        else:
            meta[key] = val
    return meta, body


def render_frontmatter(meta: dict, body: str) -> str:
    """Render meta + body back into the spec-delta content format."""
    lines = ["---"]
    for key in _SPEC_KEYS_ORDER:
        if key not in meta:
            continue
        val = meta[key]
        if isinstance(val, bool):
            rendered = "true" if val else "false"
        elif isinstance(val, int):
            rendered = str(val)
        elif val is None:
            continue
        else:
            s = str(val)
            if s.startswith("*") or ":" in s:
                rendered = f'"{s}"'
            else:
                rendered = s
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + "\n" + (body or "")


# ── Cron utilities ───────────────────────────────────────────────────────


def _parse_cron_next(cron_expr: str, now: datetime) -> float | None:
    """Return the next epoch time matching the cron expression.

    Hand-rolled subset: supports `*`, `*/N`, `A,B,C`, `A-B` in each field.
    Five fields: minute hour day-of-month month day-of-week.
    """
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return None
        minute, hour, dom, month, dow = parts

        def matches(field_val: str, value: int, _max: int) -> bool:
            if field_val == "*":
                return True
            if field_val.startswith("*/"):
                return value % int(field_val[2:]) == 0
            values = set()
            for part in field_val.split(","):
                if "-" in part:
                    lo, hi = part.split("-", 1)
                    values.update(range(int(lo), int(hi) + 1))
                else:
                    values.add(int(part))
            return value in values

        local_now = now.astimezone()
        candidate = local_now.replace(second=0, microsecond=0)
        for _ in range(8 * 24 * 60):
            ts = candidate.timestamp() + 60
            candidate = datetime.fromtimestamp(ts).astimezone()
            if (
                matches(minute, candidate.minute, 59)
                and matches(hour, candidate.hour, 23)
                and matches(dom, candidate.day, 31)
                and matches(month, candidate.month, 12)
                and matches(dow, (candidate.weekday() + 1) % 7, 6)
            ):
                return candidate.timestamp()
    except (ValueError, IndexError):
        return None
    return None


def next_fire_after(cron: str, after_epoch: float) -> float | None:
    pivot = datetime.fromtimestamp(after_epoch).astimezone()
    return _parse_cron_next(cron, pivot)


def validate_cron(schedule: str | None) -> bool:
    if not schedule:
        return True
    return next_fire_after(schedule, time.time()) is not None


def preview_fires(schedule: str, count: int = 5) -> list[str]:
    """Return the next N fire times as ISO strings."""
    if not schedule:
        return []
    fires: list[str] = []
    pivot = time.time()
    for _ in range(max(1, min(count, 20))):
        nxt = next_fire_after(schedule, pivot)
        if not nxt:
            break
        fires.append(datetime.fromtimestamp(nxt).astimezone().isoformat())
        pivot = nxt
    return fires


# ── Lake CRUD ────────────────────────────────────────────────────────────


def _routine_id_from_tags(tags: list[str]) -> str | None:
    return next((t.split(":", 1)[1] for t in tags if t.startswith("routine-id:")), None)


def _fire_delta_id_from_tags(tags: list[str]) -> str | None:
    return next((t.split(":", 1)[1] for t in tags if t.startswith("fire-delta:")), None)


def _workspace_from_tags(tags: list[str], fallback: str = "") -> str:
    return next(
        (t.split(":", 1)[1] for t in tags if t.startswith("workspace:")),
        fallback,
    )


def _ts_to_epoch(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


async def _spec_deltas() -> list[dict]:
    return await delta_client.query(limit=500, tags_include=["spec", "routine"])


async def _fire_deltas() -> list[dict]:
    return await delta_client.query(limit=500, tags_include=["routine-fire"])


async def _summary_deltas() -> list[dict]:
    return await delta_client.query(limit=500, tags_include=["routine-summary"])


async def list_routines() -> list[dict]:
    """Return enriched routine list: spec + last_fire + last_summary per id."""
    specs, fires, summaries = (
        await _spec_deltas(),
        await _fire_deltas(),
        await _summary_deltas(),
    )

    latest_spec: dict[str, dict] = {}
    for d in specs:
        rid = _routine_id_from_tags(d.get("tags") or [])
        if not rid:
            continue
        prev = latest_spec.get(rid)
        if prev is None or d.get("timestamp", "") > prev.get("timestamp", ""):
            latest_spec[rid] = d

    latest_fire: dict[str, dict] = {}
    for d in fires:
        rid = _routine_id_from_tags(d.get("tags") or [])
        if not rid:
            continue
        prev = latest_fire.get(rid)
        if prev is None or d.get("timestamp", "") > prev.get("timestamp", ""):
            latest_fire[rid] = d

    latest_summary: dict[str, dict] = {}
    summary_by_fire: dict[str, dict] = {}
    for d in summaries:
        tags = d.get("tags") or []
        rid = _routine_id_from_tags(tags)
        if rid:
            prev = latest_summary.get(rid)
            if prev is None or d.get("timestamp", "") > prev.get("timestamp", ""):
                latest_summary[rid] = d
        fid = _fire_delta_id_from_tags(tags)
        if fid:
            summary_by_fire[fid] = d

    routines = []
    for rid, d in latest_spec.items():
        meta, body = parse_frontmatter(d.get("content", ""))
        if meta.get("deleted"):
            continue
        workspace = _workspace_from_tags(d.get("tags") or [], meta.get("workspace", ""))
        last_fire_d = latest_fire.get(rid)
        # Prefer the summary tied to the most recent fire
        last_summary_d = (
            summary_by_fire.get(last_fire_d["id"]) if last_fire_d else None
        ) or latest_summary.get(rid)

        routines.append({
            "id": meta.get("id", rid),
            "name": meta.get("name", rid),
            "enabled": bool(meta.get("enabled", True)),
            "schedule": meta.get("schedule", ""),
            "interval_minutes": meta.get("interval_minutes", 0),
            "permission_mode": str(meta.get("permission_mode") or "auto"),
            "single_fire": bool(meta.get("single_fire", False)),
            "workspace": workspace,
            # `host` pins the routine to a specific agent. Empty/missing =
            # fleet-wide (any live agent will pick up the fire delta).
            "host": str(meta.get("host") or ""),
            "delta_id": d.get("id"),
            "prompt": body,
            "last_fire_at": _ts_to_epoch(last_fire_d.get("timestamp")) if last_fire_d else 0,
            "next_fire_at": (
                next_fire_after(meta["schedule"], time.time())
                if meta.get("enabled") and meta.get("schedule")
                else None
            ),
            "last_fire": (
                {
                    "id": last_fire_d.get("id"),
                    "timestamp": last_fire_d.get("timestamp"),
                    "content": last_fire_d.get("content", "")[:300],
                }
                if last_fire_d
                else None
            ),
            "last_summary": (
                {
                    "id": last_summary_d.get("id"),
                    "timestamp": last_summary_d.get("timestamp"),
                    "content": last_summary_d.get("content", "")[:500],
                }
                if last_summary_d
                else None
            ),
        })

    routines.sort(key=lambda r: (r.get("workspace") or "", r["id"]))
    return routines


async def get_latest_spec(routine_id: str) -> dict | None:
    """Return {delta, meta, body, workspace} for the latest spec with this id."""
    deltas = await delta_client.query(
        limit=50,
        tags_include=["spec", "routine", f"routine-id:{routine_id}"],
    )
    if not deltas:
        return None
    latest = max(deltas, key=lambda d: d.get("timestamp", ""))
    meta, body = parse_frontmatter(latest.get("content", ""))
    workspace = _workspace_from_tags(latest.get("tags") or [], meta.get("workspace", ""))
    return {"delta": latest, "meta": meta, "body": body, "workspace": workspace}


async def _write_spec(meta: dict, body: str, workspace: str) -> dict:
    rid = meta.get("id", "")
    content = render_frontmatter(meta, body)
    tags = ["spec", "routine", f"routine-id:{rid}"]
    if workspace:
        tags.append(f"workspace:{workspace}")
    return await delta_client.write(content=content, tags=tags, source="consumer-dashboard")


def _merge_meta(body: dict, existing: dict | None = None) -> tuple[dict, str, str]:
    meta = dict(existing or {})
    for key in _SPEC_KEYS_ORDER:
        if key in body:
            meta[key] = body[key]
    if "enabled" in meta:
        meta["enabled"] = bool(meta["enabled"])
    if "single_fire" in meta:
        meta["single_fire"] = bool(meta["single_fire"])
    if "deleted" in meta:
        meta["deleted"] = bool(meta["deleted"])
    if meta.get("interval_minutes") is not None:
        try:
            meta["interval_minutes"] = int(meta["interval_minutes"])
        except (ValueError, TypeError):
            meta.pop("interval_minutes", None)
    return meta, body.get("prompt", ""), meta.get("workspace", "")


async def create(body: dict) -> dict:
    rid = (body.get("id") or "").strip()
    name = (body.get("name") or "").strip()
    if not rid or not name:
        raise ValueError("id and name are required")
    existing = await get_latest_spec(rid)
    if existing and not existing["meta"].get("deleted"):
        raise FileExistsError(f"Routine {rid} already exists")
    if not validate_cron(body.get("schedule")):
        raise ValueError("Invalid cron schedule")
    meta, prompt, workspace = _merge_meta(body)
    meta.setdefault("enabled", True)
    meta.setdefault("permission_mode", "auto")
    meta.setdefault("deleted", False)
    written = await _write_spec(meta, prompt, workspace)
    return {"created": True, "routine_id": rid, "delta_id": written.get("id")}


async def update(routine_id: str, body: dict) -> dict:
    existing = await get_latest_spec(routine_id)
    if not existing or existing["meta"].get("deleted"):
        raise FileNotFoundError(f"Routine {routine_id} not found")
    if "schedule" in body and not validate_cron(body["schedule"]):
        raise ValueError("Invalid cron schedule")
    merged_body = {**body, "id": routine_id}
    meta, prompt, workspace = _merge_meta(merged_body, existing["meta"])
    if "prompt" not in body:
        prompt = existing["body"]
    if not workspace:
        workspace = existing["workspace"]
    written = await _write_spec(meta, prompt, workspace)
    return {"updated": True, "routine_id": routine_id, "delta_id": written.get("id")}


async def soft_delete(routine_id: str) -> dict:
    existing = await get_latest_spec(routine_id)
    if not existing or existing["meta"].get("deleted"):
        raise FileNotFoundError(f"Routine {routine_id} not found")
    meta = dict(existing["meta"])
    meta["deleted"] = True
    meta.setdefault("id", routine_id)
    written = await _write_spec(meta, "", existing["workspace"])
    return {"deleted": True, "routine_id": routine_id, "delta_id": written.get("id")}


async def fire(routine_id: str, prompt_override: str | None = None) -> dict:
    """Write a routine-fire delta. The kitty plugin (or other consumer) picks it up.

    Stamps a `fired-at:<iso>` tag so repeat fires of the same routine bypass
    the lake's sequential dedup (which skips writes when source + tags +
    content all match the previous delta). Without this tag, clicking "Fire
    now" twice in a row for the same routine would be a no-op.
    """
    existing = await get_latest_spec(routine_id)
    if not existing or existing["meta"].get("deleted"):
        raise FileNotFoundError(f"Routine {routine_id} not found")
    meta = existing["meta"]
    body = prompt_override if prompt_override is not None else existing["body"]
    fired_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    tags = ["routine-fire", f"routine-id:{routine_id}", f"fired-at:{fired_at}"]
    if existing["workspace"]:
        tags.append(f"workspace:{existing['workspace']}")
    # Host-pin the fire if the spec has a host; kitty plugins on other
    # machines will veto fires whose host: tag doesn't match them.
    host_target = str(meta.get("host") or "").strip()
    if host_target:
        tags.append(f"host:{host_target}")
    mode = str(meta.get("permission_mode") or "auto").strip()
    if mode:
        tags.append(f"permission-mode:{mode}")
    written = await delta_client.write(content=body.strip(), tags=tags, source="consumer-dashboard")
    return {"fired": True, "routine_id": routine_id, "fire_delta_id": written.get("id")}
