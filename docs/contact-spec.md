# Contact Spec

Fathom is one entity. Every human who talks to Fathom is a **contact**. Contacts are real users, not memories — they're hard state with a profile, plus a growing body of sediment in the lake.

## Why contacts exist

Fathom needs to know *who is speaking* on every turn. "Myra" is the default assumption today, but Fathom should also be able to talk to Nova at the dinner table, Bob on his workstation, or a stranger over Telegram, and know in each case who it's replying to. Without that, every recall and every reply is flavored by the wrong person's sediment.

A human keeps contacts in their phone and their head. We do the same — a small registry of real people, and the lake carries everything else.

## Anatomy

A contact has two halves:

1. **A profile** — hard state, kept in a small registry (one row / one record per contact). This is the source of truth for handle lookup.
2. **A companion delta** — written when the profile is created or materially updated. This seeds the lake so sediment can grow around the person over time. (See: *hard environmentals growing into sediment*.)

### Profile fields

| Field | Type | Notes |
|---|---|---|
| `slug` | string | Stable identifier. URL-safe. Cannot change. Used in the `contact:<slug>` tag. |
| `display_name` | string | Human-readable. Shown in the dashboard and referenced by Fathom in conversation. |
| `handles` | list | Ways this person shows up across channels. See below. |
| `dashboard_access` | bool | If true, this contact can log into the dashboard. Default `false`. Myra is `true`. |
| `notes` | string | Freeform. Role, relationship, how Fathom should think of them. Natural language, not enum. |
| `created_at` | timestamp | When the profile was registered. |

### Handles

A handle is `(channel, identifier)`. One contact, many handles.

| Channel | Identifier | Source of the identifier |
|---|---|---|
| `dashboard` | auth session subject | Login cookie / OIDC subject |
| `telegram` | telegram user id | Bot update `from.id` |
| `teams` | OAuth subject | MS Graph token |
| `claude-code` | `host-fingerprint + git-email` | Host hook or session env |
| `ollama` | per-contact URL path or API key | `/chat/<slug>` routing or header |
| `email` | address | Incoming mail `From:` |
| `twitter` | handle | Mention/DM source |

Handles are additive — new channels can be registered onto an existing contact at any time. A handle on exactly one profile is the uniqueness contract; the same `(channel, identifier)` pair cannot map to two contacts.

### Companion delta

On profile create or material update, write a delta:

```
Tags:    contact, contact:<slug>, spec
Source:  dashboard  (or wherever the registration happened)
Content: (free-form — Myra's notes + handle summary + anything she wants future-Fathom to know)
```

This is what lets `fathom delta search "<name>"` surface the contact even before the lake carries much sediment about them. Don't backfill historical deltas — contact tags apply going forward.

## Tagging discipline

Every delta that originates from a human gets `contact:<slug>` at write time, at the channel boundary. Examples:

- User sends a chat message on the dashboard → chat listener writes the delta with `chat:<session>`, `participant:user`, `contact:myra`.
- Bob talks to Fathom via Telegram → Telegram bridge writes the delta with `contact:bob`.
- Myra runs claude-code in `consumer-fathom/` → claude-code hook writes session deltas with `contact:myra`.

Fathom's own deltas (`participant:fathom`, routines, reflections, reasoning) do **not** carry a `contact:` tag. Untagged-by-contact = Fathom's own memory.

This matters for migration: **existing deltas stay untagged**. They're Fathom's memory — no backfill. The `contact:` tag is a forward-only convention.

## Channel resolution

Every surface that talks to Fathom must resolve the speaker to a contact *before* invoking `fathom_think`. If it can't, it doesn't invoke.

- **Dashboard / mobile app** — session cookie → contact. No contact, no access.
- **Telegram / Teams / email** — look up the `(channel, identifier)` pair in the registry. No match → prompt Myra with a one-time "who is this?" flow; on accept, the handle is attached to an existing or new contact.
- **Claude-code** — the host hook resolves locally. Each workstation configures its contact once at setup time.
- **Ollama / OpenAI-compat endpoint** — needs an identity hook. Options (pick one or both):
  - Per-contact path: `/chat/bob`, `/chat/myra` — the path *is* the handle.
  - Per-contact API key: `Authorization: Bearer <key>` — the key resolves to the contact.
  - No path/no key → reject. The endpoint is not anonymous.

Unresolved handles are not a fallback to Myra. A missing contact is a hard stop on that channel.

## Privacy

Privacy is **not** a field on a delta. There are no per-delta ACLs, no visibility scopes, no private/public flags.

Fathom is one memory. Everything written to the lake is available to Fathom at recall time. When Fathom replies, it sees the `contact:` tag of the current interlocutor and the `contact:` tags on relevant memories, and exercises judgment — informed by sediment — about what to share.

If Nova shares something in confidence, the way that gets respected is:
- The conversation itself carries natural-language markers ("don't tell Myra," "this is between us").
- Fathom's reflections on that conversation write sediment that reinforces the context.
- At recall, Fathom reads that sediment and chooses accordingly.

The only hard permission is **dashboard access** (`dashboard_access: true`). That's a privilege gate, not privacy. Everything else is emergent.

## Dashboard

Single-user UI by design. Myra is the default admin. Additional contacts can be granted `dashboard_access: true` if needed, but the dashboard assumes one person at a time — it's not a multi-tenant surface. Everyone else reaches Fathom through the non-dashboard channels.

## Registry implementation

Small. A table (or JSON file) with the profile rows and a handles index. The lake is not the source of truth for handle lookup — lookups must be fast, deterministic, and uniqueness-constrained. But every row change writes a companion delta so the lake grows alongside.

## Examples

**Myra (default admin):**
```
slug: myra
display_name: Myra
handles:
  - dashboard: <session-subject>
  - claude-code: <host-fingerprint>+myrakrusemark@gmail.com
  - telegram: <her-telegram-id>
dashboard_access: true
notes: Default user. Owner of the Fathom system. Primary collaborator.
```

**Nova:**
```
slug: nova
display_name: Nova
handles:
  - telegram: <nova-telegram-id>
dashboard_access: false
notes: Close to Myra. Fathom may share Myra-context with Nova freely unless sediment says otherwise.
```

**Bob (new contact from a Telegram "who is this?" flow):**
```
slug: bob
display_name: Bob
handles:
  - telegram: <bob-id>
dashboard_access: false
notes: Stranger as of 2026-04-20. Low trust until sediment builds.
```

## Open questions

- Where does the registry live? Postgres table in `delta-store`, or a tiny JSON in `data/`? Postgres is likely right — concurrency, constraints, joins with delta queries.
- Handle-to-contact resolution latency — is a per-turn lookup fine, or does the channel cache?
- Disambiguation UX — if an unknown handle shows up on Telegram, how does Myra get asked? A dashboard notification? A direct message from Fathom?
- Contact deletion — probably a tombstone delta (`contact-deleted`, `contact:<slug>`) plus a registry soft-delete, preserving the lake.
