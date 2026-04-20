# consumer-fathom — conventions

Project-specific conventions and lake tag contracts. Read when working on
chat, routines, or agent plumbing.

## Chat sessions

A chat session is a **tag**, not a table. Anyone can write into a session by
including its tag on a delta. The session IS the timestream of all such
deltas. There is no session roster, no message schema, and no central
coordinator — just the lake and these conventions.

Chat is a memory-query interface. The consumer-api's loop-LLM re-orients
each turn by searching the lake, then answers. For body work (running
commands, editing files, web access), open claude-code directly in a
kitty terminal. Chat does not route to local agents — that path was
removed because the cross-talk polluted the lake with process-lifecycle
sediment (signoffs, silence-acks, brain-switches) that aren't memories.

### Tags

| Tag | Meaning |
|---|---|
| `chat:<slug>` | This delta belongs to that session. |
| `participant:user` | Writer is the human. |
| `participant:fathom` | Writer is the Fathom consumer-api LLM. |
| `chat-event` | Ephemeral UI signal (tool use, silence-ack). Short-lived; reaped by `expires_at`. |
| `event:<kind>` | Companion to `chat-event` — names what happened (remember, recall, silence, see_image, …). |
| `chat-name` | A session-rename delta. Latest wins. |
| `chat-deleted` | Tombstone for a session. |

### Membership

Implicit. You're a member of a session if you've ever written a delta
into it. Once in, always in. No tombstones on membership.

## Polling

The consumer-api's chat listener polls the lake every ~3 seconds for new
user deltas in any chat session. For each session with fresh user
activity, it fires one inference turn through `fathom_think` — the
unified reasoning loop that searches the lake, composes, and writes a
`participant:fathom` reply delta. Silence is an option: if the model
returns `<...>`, the listener writes a short-lived silence-ack event
instead of a durable reply.

Postgres `LISTEN/NOTIFY` + SSE is a future perf optimization, not a
structural requirement.

## Routines (separate from chat)

Routines are scheduled prompts that fire on a local machine via the
agent's `kitty` plugin. They're independent of chat sessions — a routine
lands in the lake as a `routine-fire` delta that the agent picks up and
executes by spawning claude-code in a kitty window. The model running in
that kitty window is free to write deltas back to the lake (tagged with
whatever the routine's prompt instructs), and the dashboard pairs the
fire to its summary delta by routine-id.

Routines do NOT write into chat sessions. If a user wants to see what a
routine produced, they look at the routines page or search the lake by
`routine-id:<id>`.
