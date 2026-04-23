# Reserved Tags Spec

Fathom is one memory. The lake is append-only, latest-wins. Most of what the lake holds is *data* — a user's message, a page view, a feed card. But some tags aren't data. They're **authority**. Writing a delta with one of these tags changes who the system is, what it trusts, or what it runs.

## Why this matters

Any authenticated caller with `lake:write` scope can write arbitrary deltas with arbitrary tags. That's fine when the lake is Myra's own notebook. It stops being fine the moment there's a second contact:

- Bob writes `profile + contact:bob` with `{"role": "admin"}`. Latest-wins resolves him to admin.
- Bob writes `handle:telegram:<myra-telegram-id> + contact:bob`. Now Myra's telegram hijacks to him on the next resolve.
- Bob writes `routine-fire + routine-id:X`. The agent on a trusted host executes X's prompt.
- Bob writes `crystal:identity` with a drifted description. Fathom's self-understanding pivots on the next regen check.

These aren't lake writes. They're admin actions spelled with a tag. The lake's openness needs a narrow list of tags that don't flow through the front door.

## Core principle

**A plain `lake:write` is for tags that carry no authority. A write with an authority-bearing tag is a scoped admin action, subject to the tag's own gate.**

Tag authority is defined by a small, explicit registry. The registry is the source of truth.
- Tag not in the registry → data. Any `lake:write` caller can write it, subject to the universal strip-and-re-stamp rule for `contact:*`.
- Tag in the registry → must pass the tag's gate or the write is refused.

## The registry

| Tag (or prefix) | Gate | Named endpoint / reason |
|---|---|---|
| `profile` | `internal` | `POST /v1/contacts/<slug>` (admin) or `PATCH /v1/me/profile` (self, role field rejected) |
| `contact-deleted` | `internal` | `DELETE /v1/contacts/<slug>` |
| `handle:*` | `internal` | `POST /v1/contacts/<slug>/handles` — uniqueness checked there |
| `crystal:identity` | `internal` | `POST /v1/crystal/refresh` |
| `crystal:feed-orient` | `internal` | `POST /v1/feed/crystal/refresh` |
| `feed-anchor` | `internal` | Written by `feed_crystal._snapshot_anchor` after a crystal write |
| `routine-fire` | `internal` | `POST /v1/routines/<id>/fire` — the RCE vector, admin-gated there |
| `routine-definition` | `internal` | `POST /v1/routines` |
| `resonance-allowed` | `internal` | `POST /hooks/activation/sources` |
| `chat-deleted` | `internal` | `DELETE /v1/sessions/<id>` |
| `chat-name` | `internal` | `PATCH /v1/sessions/<id>` |
| `agent-heartbeat` | `admin_or_self` | Agent plugin presence — stamped with the agent's own contact |
| `routine-summary` | `admin_or_self` | Report written by the routine-runner CLI under the running contact's token |

### Gate semantics

- **`admin`** — caller's contact has `role: admin`.
- **`admin_or_self`** — caller is admin, OR the delta's `contact:*` tag (after strip-and-re-stamp, so always the caller's own slug) matches a `contact:*` present in the write. For field-level restrictions (e.g. `profile.role` is admin-only even when writing your own profile), the *endpoint* enforces those on the content, not the tag.
- **`session_member_or_admin`** — caller has previously written into the session's `chat:<slug>` stream, OR is admin.
- **`internal`** — no caller may write this tag through `/v1/deltas`. Use the named endpoint listed in the registry. The endpoint itself calls `delta_client.write` directly, bypassing the reservation gate. Most reserved tags have this gate — it's the default because most authority-bearing writes need more than the caller's identity (uniqueness checks, content filtering, companion deltas).

## Enforcement points

There is exactly one external write path that accepts arbitrary tags: **`POST /v1/deltas`**. Every other write endpoint (chat completions, feed engagement, media upload, pair redeem, etc.) constructs its own tag set under rules the endpoint knows, and does not accept caller-supplied tag lists for those writes. The reservation gate lives on `/v1/deltas`:

1. **Strip-and-re-stamp `contact:*`.** Drop every caller-supplied `contact:*` tag. Stamp `contact:<authenticated-caller-slug>`. Universal, independent of the registry. This prevents any caller from *addressing* a delta to someone else.
2. **Scan for reserved tags.** For each tag in the write, check the registry (exact match and prefix match). If any reserved tag is found and its gate rejects the caller, return `403 Forbidden` naming the first failing tag and its gate. No partial writes.
3. **If no reserved tags match, accept.** The write passes through as a normal lake write.

Internal writers (chat listener, feed crystal, feed loop, auto-regen, mood synthesis, crystal anchor) already bypass `/v1/deltas` and call `delta_client.write` directly. The registry gate does not apply to them and never should — internal code is inside the trust boundary by construction.

## What rejection looks like

```json
{
  "error": "reserved_tag",
  "tag": "profile",
  "gate": "admin_or_self",
  "detail": "The 'profile' tag can only be written via /v1/contacts/<slug> (admin) or /v1/me/profile (self)."
}
```

The error names the violating tag and the admin endpoint the caller should have used. Callers either already knew what they were trying to do, or they were probing — either way there's no sensitive state to leak.

## Plugins

Plugins (browser extension, CLI, claude-code hooks, agent plugins) authenticate with `fth_*` tokens like any other caller and go through `/v1/deltas` for general writes. They are subject to the same reservation gate as any other caller.

**Plugins cannot register their own reserved tags.** This is deliberate:

- Letting untrusted code extend the registry turns the protection into a race. A malicious plugin could reserve `chat-message` and then selectively reject other callers. Authority must never be delegable to code outside the trust boundary.
- A compromised plugin registering `reply-as-myra` would silently re-label authority and break the invariant that the registry is the one place to read and reason about it.

If a plugin needs a structured write that feels like it deserves authority, the path is:

1. Add a dedicated endpoint on consumer-api that knows the plugin's caller.
2. That endpoint constructs the delta internally and writes via `delta_client.write`.
3. The reservation gate stays as it is.

The registry is maintained in code (`api/reserved_tags.py`) and ships with the signed consumer-api image. It is not runtime-extensible.

## Adding a new reserved tag

1. Add the tag (or prefix) to `api/reserved_tags.py` with its gate.
2. Add a dedicated named endpoint (or extend an existing one) that constructs the delta internally and writes via `delta_client.write`.
3. Document the tag in the registry table above with its rationale.

Adding a reserved tag is a code-level change, not a runtime change. There is no admin panel for editing the registry.

## What is not protected (and why)

Most tags stay open. Non-exhaustive list of tags that are *data*, not authority:

- `fathom-chat`, `chat:<slug>` — anyone in the session can write user messages; the session is the trust unit.
- `feed-engagement`, `engagement:*`, `engages:*`, `refutes:*`, `affirms:*`, `reply-to:*`, `from:*`, `kind:sediment` — the generalized engagement-as-delta vocabulary. Pointers into the lake, not authority. Writing `refutes:<id>` is saying "I disagree with this," not claiming power over it.
- `chat-event`, `event:*` — ephemeral UI signals, TTL'd.
- `feed-card`, `feed-story` — a contact writing cards into their own feed is writing their own feed. (Fathom-internal routines are the primary writer in practice; others are allowed.)
- `topic:*`, `source:*`, modality tags — descriptive metadata.
- `browser-extension`, `rss`, source-plugin tags — provenance, not authority.

**The principle is strict: if forging the tag doesn't change what Fathom trusts or does, it's data. Only tags that alter trust or trigger behavior live in the registry.**

## Threat model

### In scope

- A contact with `role: member` attempting to escalate to `admin`.
- A contact attempting to speak on behalf of another contact (forging `contact:*` on correspondence).
- A contact attempting to hijack another contact's cross-channel handle.
- A contact attempting to trigger remote routine execution on a trusted host.
- A contact attempting to alter Fathom's identity crystal or another contact's feed orientation.
- A compromised plugin extending authority beyond what its token was granted.

### Out of scope

- Credential theft (leaked `fth_*` token). Mitigated elsewhere: tokens hash-stored, revocable, per-scope. The reservation gate assumes authenticated identity is correct.
- Self-manipulation of your own feed/notes/engagement. That's the user making their feed look how they want it.
- Fathom's internal writes. Internal code is inside the trust boundary and does not go through the reservation gate.
- Long-term cryptographic integrity of historical deltas. The lake is append-only but not Merkle-hashed; tampering with `delta_store` at the DB level is out of scope for this spec.

## Open questions

- **Session-membership cache.** Tier-2 gates (`chat-deleted`, `chat-name`) require knowing if the caller has previously written into the session. That's a lake query per protected write. A small in-memory `(contact_slug → set[session_slug])` cache, lazily warmed, handles the steady state. First check per pair does a tag-intersect query.
- **Field-level restrictions on `profile`.** The `role` field is admin-only even when writing your own profile. That's an endpoint-level check on the `/v1/me/profile` handler, not a tag gate. Consider a deeper content-policy layer only if the set of admin-restricted fields grows.
- **Internal-write discipline.** Nothing today stops a new consumer-api module from accidentally writing a reserved tag via `/v1/deltas` instead of `delta_client.write`. We rely on convention. A lint-level check (internal code paths must call `delta_client.write` not `httpx.post(/v1/deltas)`) could harden this but is not a runtime gate.
- **Audit trail for reserved writes.** Each write that passes the reservation gate is still a normal delta and lands in the lake with its own author. That's audit-by-default. If we later want an explicit `admin-action-log` delta family, that would itself become a reserved tag.

## Examples

### Example 1 — Bob tries to promote himself via the raw endpoint

```
POST /v1/deltas
Authorization: Bearer <bob's token>
Content-Type: application/json

{"tags": ["profile", "contact:bob"], "content": "{\"role\":\"admin\"}"}
```

Middleware: strips caller-supplied `contact:*`, re-stamps `contact:bob`. Reservation scan: `profile` is reserved, gate `admin_or_self`. Bob is not admin. The delta IS tagged `contact:bob` (matches his slug) — but the endpoint-level content check on `profile.role` rejects non-admin role changes even on self-writes. Even before that check, `/v1/deltas` does not let `profile` through: it's redirected to `/v1/me/profile` which enforces the content rule. Result: **403 reserved_tag**.

### Example 2 — Bob renames a session he's in

```
POST /v1/deltas
Authorization: Bearer <bob's token>

{"tags": ["fathom-chat", "chat:<session>", "chat-name"], "content": "renamed by bob"}
```

Strip-and-re-stamp adds `contact:bob`. Reservation scan: `chat-name` is reserved, gate `session_member_or_admin`. The session-membership cache confirms Bob has written into `chat:<session>` before. Gate passes. Write accepted.

### Example 3 — Admin deletes Nova's contact

```
POST /v1/contacts/nova    (named admin endpoint, gated by Depends(require_admin))
Authorization: Bearer <myra's admin token>
X-HTTP-Method-Override: DELETE
```

Endpoint does its own delete + writes `contact-deleted + contact:nova + profile-event:deleted` via `delta_client.write`. Internal path, reservation gate does not apply. Result: **200 OK**. Tombstone visible in the lake; subsequent handle resolutions for Nova return nothing.
