"""Memory operations as function-calling tools."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import httpx

from . import delta_client, routines as routines_mod
from .chat_listener import write_chat_event
from .settings import settings

# How long a routine-proposal event survives in the lake before the
# delta-store reaps it. Longer than the default chat-event TTL because
# the user may wander off for a while before confirming the form.
ROUTINE_PROPOSAL_TTL_SECONDS = 6 * 3600

# A heartbeat is considered "fresh" (agent connected) if it was emitted
# within this window. Heartbeats fire every ~60s, so 90s tolerates a
# single missed beat without flipping the UI to disconnected. Heartbeat
# deltas themselves live for 24h so the dashboard can still show a
# disconnected card after the connected window elapses.
HEARTBEAT_STALE_SECONDS = 90


def heartbeat_age_seconds(delta: dict) -> float | None:
    """Seconds since the given heartbeat delta was emitted, or None if unparseable."""
    ts = delta.get("timestamp", "")
    if not ts:
        return None
    try:
        hb = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    return (datetime.now(timezone.utc) - hb).total_seconds()


def heartbeat_is_fresh(delta: dict) -> bool:
    age = heartbeat_age_seconds(delta)
    return age is not None and age < HEARTBEAT_STALE_SECONDS

# ── Tool definitions (OpenAI format) ────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Search your memories. Returns moments ranked by relevance, "
                "recency, and provenance. Use this when you need to recall "
                "something — remember before answering."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you're trying to remember",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20, max 50)",
                        "default": 20,
                    },
                    "radii": {
                        "type": "object",
                        "description": "Dimension weights for ranking",
                        "properties": {
                            "temporal": {"type": "number", "default": 1.0},
                            "semantic": {"type": "number", "default": 1.0},
                            "provenance": {"type": "number", "default": 1.0},
                        },
                    },
                    "tags_include": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Only include moments with ALL of these tags",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": (
                "Persist a thought, observation, or discovery. "
                "Everything you write becomes part of you — "
                "a future self will find it when they need it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "What to persist",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for categorization (2-4 recommended)",
                    },
                    "source": {
                        "type": "string",
                        "description": "Provenance label (default: consumer-api)",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": (
                "Examine your memories by time, tags, or source. "
                "For structured retrieval when you know what you're looking for."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tags_include": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "source": {"type": "string"},
                    "time_start": {
                        "type": "string",
                        "description": "ISO-8601 timestamp",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deep_recall",
            "description": (
                "Connect threads across your memories with a multi-step plan. "
                "Primitives: search, filter, intersect, union, diff, bridge, "
                "aggregate, chain. Use when you need to trace connections."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Ordered list of plan steps. Each has 'id' (str) and "
                            "exactly one action key (search, filter, intersect, "
                            "union, diff, bridge, aggregate, chain) plus optional "
                            "radii, tags_include, limit, group_by, metric."
                        ),
                    },
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mind_tags",
            "description": "See what tags exist in your memory, with counts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mind_stats",
            "description": "Check the state of your memory: total moments, coverage, pending.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "see_image",
            "description": (
                "View an image from your memory by its media_hash. "
                "Call this when you remember a moment that includes an image "
                "and you want to see it. Returns the image for visual inspection."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "media_hash": {
                        "type": "string",
                        "description": "The media_hash from a memory (hex string)",
                    },
                },
                "required": ["media_hash"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routines",
            "description": (
                "Manage scheduled routines — prompts that fire into a local "
                "claude session on a cron schedule. Everything goes through "
                "this one tool via the `action` field. "
                "Start with action='help' to see the routine spec, or "
                "action='list' to see existing ones. "
                "If no local agent is connected the mutation actions "
                "(create/update/delete/fire) will return installation "
                "instructions — tell the user to visit the main page of the "
                "app to set one up. "
                "For action='create': the default flow is PROPOSE, NOT COMMIT. "
                "Call create with whatever fields you've composed (name, "
                "schedule, prompt at minimum — id/workspace/host may be blank) "
                "and the tool returns {status:'needs_confirmation'} while "
                "simultaneously painting a review form in the user's chat. "
                "The user edits and saves that form — you do NOT re-prompt the "
                "user for the fields in prose; just say something short like "
                "'Here's the routine — review and save.' Pass confirm=true "
                "only when the user has explicitly told you to skip the review "
                "step (e.g. 'just make it', 'don't ask, create it'). "
                "Outside a chat session (session_id absent), the tool commits "
                "directly and returns the result."
            ),
            "parameters": {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "help", "list", "get",
                            "create", "update", "delete",
                            "fire", "preview_schedule",
                        ],
                        "description": (
                            "help: spec reference and action catalogue. "
                            "list: all current routines. "
                            "get: single routine by id. "
                            "create: new routine (requires id, name; schedule/prompt strongly recommended). "
                            "update: modify fields by id. "
                            "delete: soft-delete (tombstone) by id. "
                            "fire: trigger a routine to run now. "
                            "preview_schedule: show next N fire times for a cron string."
                        ),
                    },
                    "id": {
                        "type": "string",
                        "description": "routine-id (required for get/update/delete/fire)",
                    },
                    "name": {"type": "string", "description": "human-readable label"},
                    "schedule": {
                        "type": "string",
                        "description": "5-field cron (e.g. '0 * * * *' for hourly, '*/5 * * * *' every 5 min)",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "what claude should do when this routine fires",
                    },
                    "permission_mode": {
                        "type": "string",
                        "enum": ["auto", "normal"],
                        "description": (
                            "auto: classifier auto-approves safe actions. "
                            "normal: claude prompts for each tool (user approves)."
                        ),
                    },
                    "workspace": {
                        "type": "string",
                        "description": "directory under ~/Dropbox/Work/ where the kitty session opens",
                    },
                    "host": {
                        "type": "string",
                        "description": (
                            "which machine runs this routine — must match a connected "
                            "agent's hostname (e.g. 'fedora'). Empty = fleet-wide (every "
                            "connected agent will execute the fire). When unsure, call "
                            "action='help' to see the list of connected machines, or leave "
                            "blank and the tool will ask."
                        ),
                    },
                    "enabled": {"type": "boolean", "description": "paused if false"},
                    "single_fire": {
                        "type": "boolean",
                        "description": "documented but not yet honored by scheduler",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "create-only bypass. Default behavior proposes the "
                            "routine in a chat form for the user to review. "
                            "Set true only when the user explicitly asked you "
                            "to skip the review step."
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "description": "for preview_schedule: number of upcoming fires to return (default 5)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_contact",
            "description": (
                "Notice that a person exists who isn't in the contacts "
                "registry yet, and write a proposal for an admin to "
                "review. Use this when: (1) someone mentioned in "
                "conversation clearly refers to a real person you don't "
                "have on file — partner, coworker, frequent correspondent; "
                "(2) an unknown handle shows up in a channel you were "
                "listening on. You never create contacts yourself — this "
                "tool writes a `contact-proposal` delta that surfaces in "
                "the admin's Contacts UI with Accept/Reject buttons. "
                "Search proposals first to avoid duplicates. Keep the "
                "rationale short and concrete: who they seem to be, why "
                "they matter, what evidence led you to propose them."
            ),
            "parameters": {
                "type": "object",
                "required": ["display_name", "rationale"],
                "properties": {
                    "display_name": {
                        "type": "string",
                        "description": "How people refer to this person. Required.",
                    },
                    "candidate_slug": {
                        "type": "string",
                        "description": (
                            "URL-safe identifier you'd suggest (e.g. 'nova', "
                            "'bob'). Lowercase, no spaces. Admin can override "
                            "on accept. Leave blank if you're unsure."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": (
                            "1-3 sentences: who they seem to be, what evidence "
                            "supports that, why they should be a contact."
                        ),
                    },
                    "source_context": {
                        "type": "object",
                        "description": (
                            "Optional hints for the admin: "
                            "{chat_session, delta_ids, channel, handle, …}. "
                            "Whatever helps the admin verify."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "engage",
            "description": (
                "React to a delta in the lake. Use this to mark what you "
                "just recalled — a sediment you think is wrong, a memory "
                "that resonated, a moment you're replying to. Your "
                "engagement becomes its own delta and shapes how the "
                "target surfaces in future recalls. Use `refutes` when "
                "you've read a synthesis that's wrong and want to prevent "
                "the mind from re-deriving it — your reasoning travels "
                "inline with the target on the next recall. Use `affirms` "
                "when something keeps proving useful and should rise. "
                "Use `reply-to` for neutral conversational linkage."
            ),
            "parameters": {
                "type": "object",
                "required": ["target_id", "kind"],
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "id of the delta you're engaging with",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["refutes", "affirms", "reply-to"],
                        "description": (
                            "refutes: disagree, mark as wrong — lowers its surfacing. "
                            "affirms: useful, right — raises its surfacing. "
                            "reply-to: conversational pointer, no valence."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Your reasoning in prose. For refutes this is "
                            "what future recalls see under the delta — why "
                            "you rejected it. Keep it concrete."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_session",
            "description": (
                "Rename the current chat session. The name you pass becomes "
                "the title shown in the sidebar. Use this in two cases: "
                "(1) the session is still showing its raw slug (e.g. "
                "'cross-bold-goldfinch') — pick a short descriptive title; "
                "(2) the user explicitly asks to name or rename the "
                "conversation (\"name this X\", \"rename to X\", \"call "
                "this X\") — use their requested string verbatim, even if "
                "it's silly. Never refuse a rename request by saying you "
                "can't; this tool is how you do it."
            ),
            "parameters": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "The new title, 1-6 words, lowercase, no "
                            "slug-style hyphens. For explicit user requests, "
                            "pass their requested string as-is."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain",
            "description": (
                "Explain a part of the Fathom dashboard to the user. Call this "
                "whenever the user asks what something is, how it works, or how "
                "to set it up — covers sources, feed, stats, and agent. The tool "
                "returns a spec-style description blended with the user's live "
                "state (e.g. how many sources they have configured right now), "
                "so your answer can be concrete rather than generic. Prefer this "
                "over answering from general knowledge — the dashboard is "
                "opinionated and the tool is authoritative."
            ),
            "parameters": {
                "type": "object",
                "required": ["topic"],
                "properties": {
                    "topic": {
                        "type": "string",
                        "enum": ["sources", "feed", "stats", "agent"],
                        "description": (
                            "sources: pollers that write deltas into the lake (RSS, "
                            "Mastodon, HN, custom). "
                            "feed: the 'What I noticed' surface on the dashboard — "
                            "synthesized stories from recent lake activity. "
                            "stats: the time-series dashboard showing deltas-in, "
                            "recall, mood pressure, drift. "
                            "agent: the local fathom-agent runtime — what it runs "
                            "(routines, passive senses) and how to install it."
                        ),
                    },
                },
            },
        },
    },
]


# ── Tool execution ──────────────────────────────

def _slim_search_results(raw: dict) -> dict:
    """Strip embeddings, cap content length for context window."""
    hits = raw.get("results", [])
    slim = []
    for h in hits:
        d = h.get("delta", {})
        entry = {
            "id": d.get("id"),
            "content": d.get("content", "")[:1500],
            "tags": d.get("tags", []),
            "source": d.get("source"),
            "timestamp": d.get("timestamp"),
            "distance": round(h.get("distance", 0), 3),
        }
        if d.get("media_hash"):
            entry["media_hash"] = d["media_hash"]
        slim.append(entry)
    return {"count": len(slim), "results": slim}


def _slim_query_results(raw: list) -> dict:
    """Same slimming for query results."""
    slim = []
    for d in raw:
        entry = {
            "id": d.get("id"),
            "content": d.get("content", "")[:1500],
            "tags": d.get("tags", []),
            "source": d.get("source"),
            "timestamp": d.get("timestamp"),
        }
        if d.get("media_hash"):
            entry["media_hash"] = d["media_hash"]
        slim.append(entry)
    return {"count": len(slim), "results": slim}


async def execute(name: str, arguments: dict, session_id: str | None = None) -> str:
    """Execute a tool call, return result as JSON string.

    `session_id` is injected from the API — the caller knows the current
    chat session and passes it in so tools that need it (route_to_agent)
    don't have to ask the model to pass it back as a parameter. The model
    wouldn't know anyway, and asking the user is always wrong.
    """
    try:
        if name == "remember":
            raw = await delta_client.search(
                query=arguments["query"],
                limit=arguments.get("limit", 20),
                radii=arguments.get("radii"),
                tags_include=arguments.get("tags_include"),
            )
            return json.dumps(_slim_search_results(raw))

        if name == "write":
            result = await delta_client.write(
                content=arguments["content"],
                tags=arguments.get("tags", []),
                source=arguments.get("source", "consumer-api"),
            )
            return json.dumps(result)

        if name == "recall":
            raw = await delta_client.query(
                limit=arguments.get("limit", 50),
                tags_include=arguments.get("tags_include"),
                source=arguments.get("source"),
                time_start=arguments.get("time_start"),
            )
            return json.dumps(_slim_query_results(raw))

        if name == "deep_recall":
            result = await delta_client.plan(arguments["steps"])
            return json.dumps(result)

        if name == "mind_tags":
            result = await delta_client.tags()
            return json.dumps(result)

        if name == "mind_stats":
            result = await delta_client.stats()
            return json.dumps(result)

        if name == "see_image":
            return await _fetch_image_as_tool_result(arguments.get("media_hash", ""))

        if name == "routines":
            return await _execute_routines(arguments, session_id=session_id)

        if name == "propose_contact":
            from . import contacts as contacts_mod
            written = await contacts_mod.propose(
                candidate_slug=(arguments.get("candidate_slug") or "").strip() or None,
                display_name=arguments["display_name"],
                rationale=arguments["rationale"],
                source_context=arguments.get("source_context") or {},
                # In the chat tool path, Fathom writes the proposal as
                # Fathom (no contact: tag) — the admin just needs to
                # know it's a proposal, not who proposed it.
                proposer_slug=None,
            )
            return json.dumps({
                "ok": True,
                "proposal_id": written.get("id"),
                "candidate_slug": written.get("candidate_slug"),
                "display_name": written.get("display_name"),
                "note": (
                    "Proposal written. Admin will see it in Settings → "
                    "Contacts and can Accept (creates the contact) or "
                    "Reject (keeps the proposal as sediment)."
                ),
            })

        if name == "engage":
            kind = (arguments.get("kind") or "").lower()
            if kind not in ("refutes", "affirms", "reply-to"):
                return json.dumps({"error": f"unknown engagement kind: {kind!r}"})
            target_id = (arguments.get("target_id") or "").strip()
            if not target_id:
                return json.dumps({"error": "target_id required"})
            reason = (arguments.get("reason") or "").strip()
            written = await delta_client.write(
                content=reason,
                tags=[f"{kind}:{target_id}"],
                source="fathom-engagement",
            )
            return json.dumps({
                "ok": True,
                "id": written.get("id"),
                "kind": kind,
                "target_id": target_id,
            })

        if name == "rename_session":
            if not session_id:
                return json.dumps({
                    "error": "rename_session can only be called inside a chat session",
                })
            new_name = (arguments.get("name") or "").strip()
            if not new_name:
                return json.dumps({"error": "name is required"})
            await delta_client.write(
                content=new_name,
                tags=["fathom-chat", f"chat:{session_id}", "chat-name"],
                source="consumer-api",
            )
            return json.dumps({"ok": True, "session_id": session_id, "name": new_name})

        if name == "explain":
            return await _execute_explain(arguments)

        return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# Sentinel prefix for multimodal image results — the tool loop
# in server.py detects this and converts to a content block.
IMAGE_RESULT_PREFIX = "__IMAGE__:"


async def _fetch_image_as_tool_result(media_hash: str) -> str:
    """Fetch image from delta store, return as a sentinel string.

    The tool loop in server.py detects the IMAGE_RESULT_PREFIX and
    converts this into a multimodal content block (image_url with
    base64 data URI) so the LLM actually sees the pixels.
    """
    if not media_hash:
        return json.dumps({"error": "No media_hash provided"})
    try:
        c = await delta_client._get()
        r = await c.get(f"/media/{media_hash}", timeout=15)
        r.raise_for_status()
        img_bytes = r.content
        b64 = base64.b64encode(img_bytes).decode("ascii")
        # Return sentinel so the tool loop can build a multimodal message
        return f"{IMAGE_RESULT_PREFIX}data:image/webp;base64,{b64}"
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch image: {e}"})


# ── Routines tool — action-dispatched CRUD ──────────────────────────────


ROUTINE_SPEC_HELP_STATIC = """ROUTINE SPEC — quick reference

A routine is a prompt + a cron schedule + a workspace, pinned to a specific
machine. When its cron fires, the named machine's local `fathom-agent` picks
it up, spawns a kitty window with claude in the named workspace, and injects
the prompt. Claude runs, writes a summary delta, and the dashboard pairs it
back to the fire.

Fields (used with action=create or action=update):
  id              (required, immutable)   stable identifier, e.g. "gold-check"
  name            (required)              human label, e.g. "Gold Price Pulse"
  host            (which machine)         hostname of the connected agent that runs this;
                                          empty = fleet-wide (every connected agent fires)
  schedule        (cron, 5 fields)        "0 * * * *" hourly · "*/5 * * * *" every 5 min
  prompt          (the work)              what claude should do when fired
  permission_mode auto | normal           auto = classifier guardrails · normal = user approves each tool
  workspace                                directory under ~/Dropbox/Work/ (e.g. "fathom"). Leave
                                           blank — the target agent advertises a default_workspace
                                           in its heartbeat (set during `fathom-agent init`) that
                                           fills in automatically. Only ask the user when no
                                           default exists and you can't infer one from context.
  enabled         bool (default true)
  single_fire     bool (default false, not yet honored by scheduler)

Actions (via this single `routines` tool):
  help             ← you just called this
  list             all current routines + last-run summaries
  get id=X         single routine spec
  create ...       new routine (id + name required, schedule strongly recommended)
  update id=X ...  modify fields; omitted fields inherit from existing
  delete id=X      soft-delete (writes a tombstone delta; history stays in the lake)
  fire id=X        trigger the routine to run now
  preview_schedule schedule="..." count=N    next N fire times for a cron

When mutation actions are called without a connected local agent, the tool
returns installation instructions instead. Tell the user to visit the main
dashboard and pick a platform under "Local Agent".
"""


async def _routine_help_text() -> str:
    """Static help + a live dump of currently-connected machines.

    The live section matters because `host` is a required-ish field and the
    LLM has to pick from the connected set. Including it in help means the
    LLM rarely has to make a second round trip just to see the machine list.
    """
    alive, agents = await _agent_alive()
    if not alive:
        live = "\nCONNECTED MACHINES — none right now. Mutation actions will fail until an agent connects."
    else:
        names = ", ".join(a["host"] for a in agents)
        live = (
            f"\nCONNECTED MACHINES — {names}\n"
            "When creating a routine: if only one machine is connected, use it as the "
            "default host without asking. If multiple are connected, ask the user which "
            "machine the routine should run on. The user may also name a machine that "
            "isn't currently connected — accept it; the routine sits until that machine "
            "comes back online."
        )
    return ROUTINE_SPEC_HELP_STATIC + live


# Backwards-compat alias so anything that references the old name still works.
ROUTINE_SPEC_HELP = ROUTINE_SPEC_HELP_STATIC


async def _agent_alive() -> tuple[bool, list[dict]]:
    """Return (alive, agent_summaries) for hosts with a fresh heartbeat.

    "Fresh" means the most recent heartbeat delta for that host was emitted
    within HEARTBEAT_STALE_SECONDS. Stale heartbeats are ignored — callers
    use this to decide whether mutation actions (routine dispatch, body
    routing) can reach a live agent, which stale heartbeats can't.
    """
    # Bound the query to the freshness window on the server side. Heartbeat
    # deltas linger for 24h so the dashboard can show disconnected cards —
    # without time_start we'd pull every heartbeat from every host.
    time_start = (datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_STALE_SECONDS)).isoformat()
    try:
        deltas = await delta_client.query(
            limit=50,
            tags_include=["agent-heartbeat"],
            time_start=time_start,
        )
    except Exception:
        return False, []
    agents = []
    seen_hosts = set()
    for d in deltas:
        tags = d.get("tags") or []
        host = next((t.split(":", 1)[1] for t in tags if t.startswith("host:")), "unknown")
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        if not heartbeat_is_fresh(d):
            continue
        try:
            payload = json.loads(d.get("content", "{}"))
        except Exception:
            payload = {}
        agents.append({"host": host, "plugins": payload.get("plugins") or {}})
    return len(agents) > 0, agents


def _no_agent_response(action: str) -> str:
    return json.dumps({
        "action": action,
        "error": "no_agent_connected",
        "message": (
            "No local fathom-agent is currently registered. Mutation actions "
            "(create/update/delete/fire) require a local agent to execute the "
            "resulting routine-fire deltas. Tell the user to visit the main "
            "Fathom dashboard and install a local agent from the \"Local Agent\" "
            "section (Linux / Mac / Windows), then try again."
        ),
        "dashboard_hint": "the main page of the Fathom app has the Local Agent install cards",
    })


async def _known_workspaces() -> list[str]:
    """Scan existing spec deltas for the set of workspaces currently in use.

    Used by the clarification loop so the LLM can offer the user a menu
    instead of inventing a workspace name.
    """
    try:
        specs = await delta_client.query(limit=500, tags_include=["spec", "routine"])
    except Exception:
        return []
    seen: set[str] = set()
    for d in specs:
        tags = d.get("tags") or []
        ws = next((t.split(":", 1)[1] for t in tags if t.startswith("workspace:")), "")
        if ws:
            seen.add(ws)
    return sorted(seen)


async def _host_default_workspace(host: str) -> str:
    """Look up an agent's configured default_workspace from heartbeat.

    The kitty plugin surfaces this in its heartbeat summary when the user
    set one during `fathom-agent init`. Returns empty string when unknown
    or when the host hasn't configured one.
    """
    if not host:
        return ""
    _alive, agents = await _agent_alive()
    for a in agents:
        if a.get("host") != host:
            continue
        kitty = (a.get("plugins") or {}).get("kitty") or {}
        return (kitty.get("default_workspace") or "").strip()
    return ""


async def _gather_create_gaps(args: dict) -> dict:
    """Return {missing: [...], hint: '...'} describing what's incomplete.

    Missing list is empty when everything needed is present. Hint is always
    a single human-readable sentence the LLM can use to ask the user.
    """
    missing: list[str] = []
    hints: list[str] = []

    if not (args.get("id") or "").strip():
        missing.append("id")
        hints.append("No routine id. Ask the user for a stable short identifier (e.g. 'gold-check', 'daily-heartbeat').")

    if not (args.get("name") or "").strip():
        missing.append("name")
        hints.append("No name. Ask the user for a human-readable label.")

    if not (args.get("schedule") or "").strip():
        missing.append("schedule")
        hints.append(
            "No schedule. Ask the user when the routine should fire "
            "(e.g. 'every hour' → '0 * * * *', 'every 5 minutes' → '*/5 * * * *', "
            "'daily at 9am' → '0 9 * * *'). Offer to preview with action=preview_schedule."
        )

    if not (args.get("prompt") or "").strip():
        missing.append("prompt")
        hints.append("No prompt. Ask the user what claude should do when this routine fires.")

    if not (args.get("workspace") or "").strip():
        # Before declaring a workspace gap, see if the target host has one
        # configured via `fathom-agent init`. That default travels with the
        # agent's heartbeat, so the LLM shouldn't have to ask if it's set.
        host_default = await _host_default_workspace((args.get("host") or "").strip())
        if host_default:
            args["workspace"] = host_default
        else:
            missing.append("workspace")
            known = await _known_workspaces()
            if known:
                hints.append(
                    f"No workspace. Known workspaces from existing routines: {', '.join(known)}. "
                    "Ask the user which directory under ~/Dropbox/Work/ the routine should run in."
                )
            else:
                hints.append(
                    "No workspace. Ask the user which directory under ~/Dropbox/Work/ "
                    "the routine should run in (e.g. 'fathom', 'applications')."
                )

    # `host` is only a "gap" when there are 2+ live machines — the user has
    # to pick. With exactly one, it's silently defaulted further down. With
    # zero live machines, other gates have already rejected the call. An
    # explicit host the user named (even if offline) is accepted as-is.
    if "host" not in args:
        _alive, agents = await _agent_alive()
        if len(agents) > 1:
            missing.append("host")
            names = ", ".join(a["host"] for a in agents)
            hints.append(
                f"No machine. Live machines right now: {names}. "
                "Ask the user which machine should run this routine. "
                "They can also name a machine that isn't currently connected; "
                "the routine will sit until that machine comes back."
            )

    return {"missing": missing, "hint": " ".join(hints) if hints else ""}


async def _execute_routines(args: dict, session_id: str | None = None) -> str:
    action = (args.get("action") or "help").strip().lower()

    # Informational actions — always work, even without an agent.
    if action == "help":
        alive, agents = await _agent_alive()
        return json.dumps({
            "action": "help",
            "agent_connected": alive,
            "agents": agents,
            "spec": await _routine_help_text(),
        })

    if action == "list":
        alive, agents = await _agent_alive()
        routines = await routines_mod.list_routines()
        # Slim each to keep context lean
        slim = [
            {
                "id": r["id"], "name": r["name"], "enabled": r["enabled"],
                "schedule": r.get("schedule"), "workspace": r.get("workspace"),
                "permission_mode": r.get("permission_mode"),
                "last_fire_at": r.get("last_fire_at"),
                "last_summary": (r.get("last_summary") or {}).get("content"),
            }
            for r in routines
        ]
        return json.dumps({
            "action": "list",
            "agent_connected": alive,
            "count": len(slim),
            "routines": slim,
        })

    if action == "get":
        rid = (args.get("id") or "").strip()
        if not rid:
            return json.dumps({"action": "get", "error": "id is required"})
        spec = await routines_mod.get_latest_spec(rid)
        if not spec or spec["meta"].get("deleted"):
            return json.dumps({"action": "get", "error": f"routine {rid} not found"})
        return json.dumps({
            "action": "get",
            "routine": {
                "id": spec["meta"].get("id"),
                "meta": spec["meta"],
                "body": spec["body"],
                "workspace": spec["workspace"],
            },
        })

    if action == "preview_schedule":
        sched = (args.get("schedule") or "").strip()
        if not sched:
            return json.dumps({"action": "preview_schedule", "error": "schedule is required"})
        fires = routines_mod.preview_fires(sched, count=int(args.get("count") or 5))
        return json.dumps({
            "action": "preview_schedule",
            "schedule": sched,
            "fires": fires,
            "error": None if fires else "invalid cron",
        })

    # Mutation actions — require an agent.
    if action in ("create", "update", "delete", "fire"):
        alive, _ = await _agent_alive()
        if not alive:
            return _no_agent_response(action)

    if action == "create":
        # Single-machine default: if exactly one agent is connected and the
        # caller didn't set `host`, silently pin to that machine. With two or
        # more live agents, the user picks from the form's host dropdown.
        # With zero live agents, the earlier _agent_alive gate already
        # rejected the call.
        if "host" not in args:
            _alive, agents = await _agent_alive()
            if len(agents) == 1:
                args = {**args, "host": agents[0]["host"]}

        confirm = bool(args.get("confirm"))

        # Proposal flow: inside a chat, paint the routine form in the stream
        # and let the human review/edit/save. Skipped when confirm=true
        # (user said "just make it") or outside chat (no session_id).
        if session_id and not confirm:
            proposal = {k: args[k] for k in args if k not in ("action", "confirm")}
            try:
                await write_chat_event(
                    session_id,
                    "routine-proposal",
                    {"proposal": proposal},
                    ttl_seconds=ROUTINE_PROPOSAL_TTL_SECONDS,
                )
            except Exception as e:
                return json.dumps({
                    "action": "create",
                    "status": "proposal_failed",
                    "message": f"couldn't paint review form: {e}",
                })
            return json.dumps({
                "action": "create",
                "status": "needs_confirmation",
                "hint": (
                    "The routine form is now in the chat for the user to "
                    "review and save. Reply briefly — do NOT restate the "
                    "fields in prose."
                ),
                "proposal": proposal,
            })

        # Clarification loop: inspect args, return `needs_info` when gaps exist
        # so the LLM can go back to the user and ask before committing.
        gaps = await _gather_create_gaps(args)
        if gaps["missing"]:
            return json.dumps({
                "action": "create",
                "status": "needs_info",
                "missing": gaps["missing"],
                "hint": gaps["hint"],
                "partial": {k: args[k] for k in args if k not in ("action", "confirm")},
            })
        try:
            body = {k: args[k] for k in args if k not in ("action", "confirm")}
            result = await routines_mod.create(body)
            return json.dumps({"action": "create", **result})
        except FileExistsError:
            # Upgrade dup-collision from hard error to conversational clarification
            rid = args.get("id", "")
            existing = await routines_mod.get_latest_spec(rid)
            existing_name = (existing or {}).get("meta", {}).get("name", "") if existing else ""
            return json.dumps({
                "action": "create",
                "status": "needs_info",
                "missing": ["id_or_intent"],
                "hint": (
                    f"A routine with id '{rid}' already exists"
                    + (f" (name: '{existing_name}')" if existing_name else "")
                    + ". Ask the user: do they want to update the existing one "
                    + "(use action=update), replace it (delete first, then create), "
                    + "or pick a different id?"
                ),
                "partial": {k: args[k] for k in args if k not in ("action", "confirm")},
            })
        except ValueError as e:
            return json.dumps({"action": "create", "error": "invalid", "message": str(e)})

    if action == "update":
        rid = (args.get("id") or "").strip()
        if not rid:
            return json.dumps({"action": "update", "error": "id is required"})
        try:
            body = {k: args[k] for k in args if k not in ("action", "id")}
            result = await routines_mod.update(rid, body)
            return json.dumps({"action": "update", **result})
        except FileNotFoundError as e:
            return json.dumps({"action": "update", "error": "not_found", "message": str(e)})
        except ValueError as e:
            return json.dumps({"action": "update", "error": "invalid", "message": str(e)})

    if action == "delete":
        rid = (args.get("id") or "").strip()
        if not rid:
            return json.dumps({"action": "delete", "error": "id is required"})
        try:
            result = await routines_mod.soft_delete(rid)
            return json.dumps({"action": "delete", **result})
        except FileNotFoundError as e:
            return json.dumps({"action": "delete", "error": "not_found", "message": str(e)})

    if action == "fire":
        rid = (args.get("id") or "").strip()
        if not rid:
            return json.dumps({"action": "fire", "error": "id is required"})
        try:
            result = await routines_mod.fire(rid, prompt_override=args.get("prompt"))
            return json.dumps({"action": "fire", **result})
        except FileNotFoundError as e:
            return json.dumps({"action": "fire", "error": "not_found", "message": str(e)})

    return json.dumps({"action": action, "error": f"unknown action: {action}"})


# ── Explain tool ────────────────────────────────────────────────────────────
#
# Static doc + live state, per dashboard concept. Each topic builder returns
# a dict; the tool serializes it. Live calls stay best-effort — if the
# source-runner is down we still return the static doc so the LLM has
# something useful to relay.


_EXPLAIN_SOURCES = (
    "SOURCES — pollers that write deltas into the lake on a schedule.\n"
    "Each source is a plugin running inside the source-runner container:\n"
    "  rss           — fetch feed items, one delta per entry\n"
    "  mastodon      — a user's timeline, one delta per toot\n"
    "  hacker-news   — front-page stories above a karma threshold\n"
    "  custom        — user-defined, wraps any HTTP endpoint\n\n"
    "Sources show up as chips under the 'Sources' section on the dashboard. "
    "Click + Add source to configure one. Configured sources can be paused, "
    "resumed, or manually polled from their detail view. Deltas a source "
    "writes get tagged with the source type + instance id, so you can filter "
    "for them later (e.g. tags_include=['source:rss', 'rss:hn-front'])."
)


_EXPLAIN_FEED = (
    "FEED ('What I noticed') — the dashboard's top surface, right below the "
    "opener. Each card is a synthesized *story* composed from a cluster of "
    "recent deltas: the feed worker groups related lake activity and writes "
    "a narrative delta with a title, body, and optional images.\n\n"
    "Stories are generated lazily from lake content — on a fresh install "
    "with no deltas there's nothing to synthesize, which is why new users "
    "see an empty-state prompt until their first sources or chats land.\n\n"
    "Tapping a card opens it as a new chat session with the story as the "
    "opening turn, so the user can dig into any thread Fathom noticed."
)


_EXPLAIN_STATS = (
    "STATS — a multi-track time-series of Fathom's internal state, rendered "
    "like an ECG at the bottom of the dashboard. Each track is a different "
    "signal sampled over the last N hours:\n"
    "  deltas       — writes per time bucket (ingest rate)\n"
    "  recall       — how many deltas got pulled back out via search\n"
    "  mood         — carrier-wave pressure (how 'loud' things feel)\n"
    "  drift        — semantic drift between identity crystal and current state\n"
    "  usage        — LLM token spend per bucket\n\n"
    "Stats is the 'am I alive and well?' view — a glance shows whether "
    "sources are flowing, whether Fathom is recalling, whether pressure is "
    "building toward a mood synthesis. The drift track in particular "
    "triggers auto-regeneration of the identity crystal when it crosses "
    "threshold × red_ratio (see settings.crystal_*)."
)


_EXPLAIN_AGENT = (
    "AGENT — the local Node process that runs on each connected machine. "
    "It emits passive observations into the lake on a schedule (sysinfo, "
    "vault watchers, homeassistant feeds) and executes routines when their "
    "cron schedules fire. Routines are the way you reach a machine: a "
    "prompt + cron + workspace, and the local agent spawns a claude-code "
    "subprocess in a kitty window to do the work.\n\n"
    "Without a body connected, you still have your mind (chat, memory, "
    "feed), but routines can't execute — they need a body to run on.\n\n"
    "Install a new body: main dashboard → Agent section → pick Linux / "
    "Mac / Windows → run the one-liner. Each body writes an "
    "agent-heartbeat delta every ~60s tagged host:<hostname>, so you "
    "know which ones are alive."
)


async def _live_sources_summary() -> dict:
    """Best-effort: fetch configured sources from source-runner."""
    try:
        async with httpx.AsyncClient(
            base_url=settings.source_runner_url.rstrip("/"), timeout=5
        ) as c:
            r = await c.get("/api/sources")
            r.raise_for_status()
            data = r.json() or {}
    except Exception:
        return {"configured": None, "note": "source-runner unreachable"}

    items = data.get("sources") or data if isinstance(data, list) else data.get("sources", [])
    if not isinstance(items, list):
        items = []
    configured = [s for s in items if s.get("status") != "available"]
    by_status: dict[str, int] = {}
    for s in configured:
        st = s.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
    return {
        "configured": len(configured),
        "by_status": by_status,
        "types": sorted({s.get("source_type") for s in configured if s.get("source_type")}),
    }


async def _live_feed_summary() -> dict:
    try:
        data = await delta_client.feed_stories(limit=50, offset=0)
    except Exception:
        return {"stories": None, "note": "feed endpoint unreachable"}
    return {"stories": len(data.get("stories") or [])}


async def _live_stats_summary() -> dict:
    try:
        s = await delta_client.stats()
    except Exception:
        return {"note": "stats endpoint unreachable"}
    return {
        "total_deltas": s.get("total_deltas") or s.get("count"),
        "embedded": s.get("embedded") or s.get("embedded_count"),
        "embedding_coverage": s.get("embedding_coverage"),
    }


async def _live_agent_summary() -> dict:
    alive, agents = await _agent_alive()
    return {
        "connected": alive,
        "count": len(agents),
        "hosts": [a["host"] for a in agents],
    }


async def _execute_explain(args: dict) -> str:
    topic = (args.get("topic") or "").strip().lower()
    if topic == "sources":
        return json.dumps({
            "topic": topic,
            "doc": _EXPLAIN_SOURCES,
            "live": await _live_sources_summary(),
        })
    if topic == "feed":
        return json.dumps({
            "topic": topic,
            "doc": _EXPLAIN_FEED,
            "live": await _live_feed_summary(),
        })
    if topic == "stats":
        return json.dumps({
            "topic": topic,
            "doc": _EXPLAIN_STATS,
            "live": await _live_stats_summary(),
        })
    if topic == "agent":
        return json.dumps({
            "topic": topic,
            "doc": _EXPLAIN_AGENT,
            "live": await _live_agent_summary(),
        })
    return json.dumps({
        "topic": topic,
        "error": "unknown_topic",
        "known": ["sources", "feed", "stats", "agent"],
    })
