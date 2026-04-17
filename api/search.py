"""Canonical natural-language recall over the delta lake.

One entry point — ``search(text, depth, ...)`` — returns a hierarchical
structured result. Used by the consumer chat internally (pre-recall layer),
the ``POST /v1/search`` endpoint (CLI, MCP, claude-code recall hook), and
anywhere else NL search happens. Every surface shares the same plan, the
same DAG rendering, and the same voice.

Deep mode generates a compositional plan via the planner LLM, executes it
against the delta store, walks the DAG, and emits an associative trail.
Shallow mode runs a single semantic search wrapped in the same shape so
callers don't branch.

Result shape::

    {
      "plan": {...},                    # plan executed (shallow = synthetic)
      "tree": [                         # topo-ordered DAG nodes
        {"id": "a", "relation": "first came to mind",
         "parents": [], "action": "search", "query": "...",
         "delta_ids": ["...", ...]},
        {"id": "b", "relation": "which pulled on",
         "parents": ["a"], "action": "chain", "query": "a",
         "delta_ids": [...]},
      ],
      "deltas_by_step": {"a": [...], "b": [...]},
      "total_count": int,
      "media_hashes": [...],            # up to 5 for UI thumbnails
      "as_prompt": str,                 # pre-rendered hierarchical text
    }
"""

from __future__ import annotations

import json

from . import delta_client
from .prompt import SEARCH_PLANNER_PROMPT
from .providers import llm
from .settings import settings

_ACTION_KEYS = (
    "search",
    "filter",
    "chain",
    "bridge",
    "intersect",
    "union",
    "diff",
    "aggregate",
)

_DEFAULT_RELATION_BY_ACTION = {
    "search": "surfaced",
    "filter": "from around that time",
    "chain": "and that reminded me of",
    "bridge": "bridging those to",
    "intersect": "and the overlap",
    "union": "taken together",
    "diff": "but not",
    "aggregate": "grouped",
}

_MAX_CONTENT_CHARS = 1200
_MAX_MEDIA_HASHES = 5


# ── Planner (deep mode) ─────────────────────────


async def _generate_plan(
    text: str,
    conv_context: str = "",
    session_slug: str | None = None,
) -> dict | None:
    """Fast LLM call that composes a multi-step plan annotated with relations."""
    prompt = text
    if conv_context:
        prompt = f"Conversation so far:\n{conv_context}\n\nLatest message: {text}"

    try:
        resp = await llm.chat.completions.create(
            model=settings.resolved_model,
            messages=[
                {"role": "system", "content": SEARCH_PLANNER_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        plan = json.loads(raw)
    except Exception:
        return None

    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        return None
    if not plan["steps"]:
        return None

    if session_slug:
        _inject_session_step(plan, session_slug)

    return plan


def _inject_session_step(plan: dict, session_slug: str) -> None:
    """Add a session-scoped filter step (medium-term memory) to the plan."""
    session_step = {
        "id": "_session",
        "relation": "and from this conversation",
        "filter": {
            "tags_include": ["fathom-chat", f"chat:{session_slug}"],
        },
        "tags_exclude": ["chat-name", "chat-deleted"],
        "limit": 30,
    }
    last = plan["steps"][-1]
    if isinstance(last.get("union"), list):
        last["union"].append("_session")
        plan["steps"].insert(-1, session_step)
        return

    ids = [s["id"] for s in plan["steps"]]
    plan["steps"].append(session_step)
    plan["steps"].append(
        {
            "id": "_combined",
            "union": [ids[0], "_session"],
            "relation": "taken together",
        }
    )


# ── DAG inspection ──────────────────────────────


def _action_of(step: dict) -> tuple[str, object]:
    for k in _ACTION_KEYS:
        if k in step:
            return k, step[k]
    return "unknown", None


def _parents_of(step: dict) -> list[str]:
    action, val = _action_of(step)
    if action in ("chain", "aggregate"):
        return [val] if isinstance(val, str) else []
    if action in ("bridge", "intersect", "union", "diff"):
        return [v for v in val if isinstance(v, str)] if isinstance(val, list) else []
    return []


# ── Rendering ───────────────────────────────────


def _delta_line(d: dict) -> str:
    src = d.get("source", "unknown")
    ts = (d.get("timestamp") or "")[:16]
    tags = ", ".join((d.get("tags") or [])[:4])
    media = (
        f"\n[has image: media_hash={d['media_hash']}]" if d.get("media_hash") else ""
    )
    content = (d.get("content") or "")[:_MAX_CONTENT_CHARS]
    return f"[{src} · {ts} · {tags}]{media}\n{content}"


def _render_tree(tree: list[dict], deltas_by_step: dict[str, list[dict]]) -> str:
    """Walk tree in order, emit 'relation — header:' blocks of deltas.

    Each delta surfaces only once, in the first step that contains it,
    so later union/chain steps don't rehash memories already shown.
    """
    blocks: list[str] = []
    seen: set[str] = set()

    for node in tree:
        deltas = deltas_by_step.get(node["id"], [])
        unique = []
        for d in deltas:
            did = d.get("id")
            if did and did in seen:
                continue
            unique.append(d)
            if did:
                seen.add(did)
        if not unique:
            continue

        relation = node.get("relation") or _DEFAULT_RELATION_BY_ACTION.get(
            node.get("action", ""), "surfaced"
        )
        header_parts = [relation]
        q = node.get("query")
        if isinstance(q, str) and q:
            header_parts.append(f'"{q}"')
        elif isinstance(q, list) and q:
            header_parts.append(f"from {' + '.join(str(x) for x in q)}")
        header = " — ".join(header_parts) + ":"

        body = "\n\n".join(_delta_line(d) for d in unique)
        blocks.append(f"{header}\n\n{body}")

    return "\n\n---\n\n".join(blocks)


# ── Main entry point ────────────────────────────


async def search(
    text: str,
    depth: str = "deep",
    session_slug: str | None = None,
    conv_context: str = "",
    limit: int = 50,
    threshold: float | None = None,
) -> dict:
    """Canonical NL recall.

    ``depth="deep"``    — planner LLM composes a multi-step plan, DAG preserved.
    ``depth="shallow"`` — single semantic search, one-node tree.

    ``threshold`` (shallow only) drops results whose distance > threshold.

    Logs a recall event so the Stats Activity card can plot retrievals
    alongside captures (sibling to write-side usage).
    """
    if not text or not text.strip():
        return _empty_result()

    # Lazy import to keep the dep one-way (recall doesn't import search).
    try:
        from . import recall as _recall
        _recall.fire_and_forget()
    except Exception:
        pass

    if depth == "shallow":
        return await _shallow(text, limit=limit, threshold=threshold)
    return await _deep(
        text,
        conv_context=conv_context,
        session_slug=session_slug,
        limit=limit,
    )


async def _shallow(text: str, *, limit: int, threshold: float | None) -> dict:
    try:
        data = await delta_client.search(text, limit=limit)
    except Exception:
        return _empty_result()
    raw = data.get("results", []) or []
    if threshold is not None:
        raw = [r for r in raw if r.get("distance", 1.0) <= threshold]

    deltas: list[dict] = []
    media_hashes: list[str] = []
    for r in raw:
        d = dict(r.get("delta") or r)
        if "id" not in d and "delta_id" in d:
            d["id"] = d["delta_id"]
        deltas.append(d)
        if d.get("media_hash"):
            media_hashes.append(d["media_hash"])

    node = {
        "id": "root",
        "relation": "what came to mind",
        "parents": [],
        "action": "search",
        "query": text,
        "delta_ids": [d.get("id") for d in deltas if d.get("id")],
    }
    tree = [node] if deltas else []
    deltas_by_step = {"root": deltas} if deltas else {}

    return {
        "plan": {"steps": [{"id": "root", "search": text, "limit": limit}]},
        "tree": tree,
        "deltas_by_step": deltas_by_step,
        "total_count": len(deltas),
        "media_hashes": media_hashes[:_MAX_MEDIA_HASHES],
        "as_prompt": _render_tree(tree, deltas_by_step),
    }


async def _deep(
    text: str,
    *,
    conv_context: str,
    session_slug: str | None,
    limit: int,
) -> dict:
    plan = await _generate_plan(
        text, conv_context=conv_context, session_slug=session_slug
    )
    if not plan:
        return _empty_result()

    try:
        result = await delta_client.plan(plan["steps"])
    except Exception:
        return _empty_result(plan=plan)

    steps_data = result.get("steps", {}) or {}
    tree: list[dict] = []
    deltas_by_step: dict[str, list[dict]] = {}
    media_hashes: list[str] = []
    total = 0

    for step in plan["steps"]:
        sid = step["id"]
        action, val = _action_of(step)
        raw_deltas = (steps_data.get(sid, {}) or {}).get("deltas", []) or []

        cleaned: list[dict] = []
        for d in raw_deltas:
            tags = d.get("tags") or []
            if "assistant" in tags and (
                "fathom-chat" in tags or d.get("source") == "fathom-chat"
            ):
                continue
            cleaned.append(d)
            if d.get("media_hash"):
                media_hashes.append(d["media_hash"])

        total += len(cleaned)

        relation = step.get("relation") or _DEFAULT_RELATION_BY_ACTION.get(
            action, "surfaced"
        )
        query = (
            val if action == "search" else val if isinstance(val, (str, list)) else None
        )

        tree.append(
            {
                "id": sid,
                "relation": relation,
                "parents": _parents_of(step),
                "action": action,
                "query": query,
                "delta_ids": [d.get("id") for d in cleaned if d.get("id")],
            }
        )
        deltas_by_step[sid] = cleaned

    return {
        "plan": plan,
        "tree": tree,
        "deltas_by_step": deltas_by_step,
        "total_count": total,
        "media_hashes": media_hashes[:_MAX_MEDIA_HASHES],
        "as_prompt": _render_tree(tree, deltas_by_step),
    }


def _empty_result(plan: dict | None = None) -> dict:
    return {
        "plan": plan or {"steps": []},
        "tree": [],
        "deltas_by_step": {},
        "total_count": 0,
        "media_hashes": [],
        "as_prompt": "",
    }
