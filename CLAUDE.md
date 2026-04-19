# consumer-fathom — conventions

Project-specific conventions and lake tag contracts. Read when working on chat,
routing, or agent plumbing.

## Chat sessions

A chat session is a **tag**, not a table. Anyone can write into a session by
including its tag on a delta. The session IS the timestream of all such
deltas. There is no session roster, no message schema, and no central
coordinator — just the lake and these conventions.

### Tags

| Tag | Meaning |
|---|---|
| `chat:<slug>` | This delta belongs to that session. |
| `to:agent:<host>` | Routing — invites/addresses the named agent. Multiple `to:` tags are allowed on one delta for broadcast invites. |
| `participant:user` | Writer is the human. |
| `participant:fathom` | Writer is the Fathom consumer-api LLM. |
| `participant:agent:<host>` | Writer is a local agent (or claude-code subprocess spawned by it). |
| `workspace:<name>` | Optional. On a routing delta, selects the workspace directory the agent should spawn claude-code in. |
| `signoff` | Agent's final delta in an engagement — paired with `chat:<slug>`. UI renders it as "agent <host> signed off." |

### Membership

Implicit. You're a member of a session if you've ever (a) appeared in a `to:`
of a delta in it, or (b) written a delta to it. Once in, always in. No
tombstones yet.

### Routing (`to:agent:<host>`)

The agent's `chat-router` plugin polls the lake for deltas where its own host
appears in a `to:` tag. On each such delta:

1. Extract the `chat:<slug>` tag. If absent, skip (non-chat routing is future work).
2. If no engagement exists for that slug, spawn a claude-code subprocess
   (via the kitty plugin) with an **orient prompt** that tells it: the
   session slug, how to search the lake to catch up, how to tag outgoing
   deltas, and how to signoff.
3. If an engagement already exists, inject the new delta into the running
   subprocess as a framed user message (`Message from <participant> in
   chat:<slug>: …`) via `kitty @ send-text`.

The engagement subprocess is transient — when claude-code exits, the router
forgets the session. A future `to:agent:<host>` in the same session spawns
fresh; the lake provides continuity, not the process.

### Output convention (for the agent's claude-code)

Every delta the agent writes during a chat engagement **must** include the
session tag(s) so it surfaces in chat. Tool outputs, observations, and the
final signoff delta all piggyback on the normal delta write path — the `chat:`
tag is what makes them chat messages.

### Liveness and signoff

- Claude-code exits naturally when its task is done.
- Its final delta should include both `chat:<slug>` and `signoff`. The
  dashboard renders that as a soft "signed off" line.
- If claude-code crashes without writing a signoff, the dashboard still has
  the last delta from that participant — the session just continues without
  a clean goodbye. Acceptable for V1.

### One host, many sessions

A single agent host can be engaged with N sessions simultaneously — one
claude-code subprocess per session, one kitty window per subprocess. The
router keys everything by session slug. Session IS the address.

## Polling

Chat-router polls every 2 seconds. Dashboard chat already polls on a similar
cadence. Postgres `LISTEN/NOTIFY` + SSE is a future perf optimization, not a
structural requirement.
