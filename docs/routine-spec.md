# Routine Spec

A routine is a prompt + a schedule + a place to run. It lives in the delta lake as a single delta, tagged and structured so the scheduler can find it, fire it on cron, and the dashboard can display it.

## Anatomy

A routine is a **spec delta** with three things:

1. **Tags** — `spec`, `routine`, and `routine-id:<stable-id>`, plus an optional `workspace:<name>`.
2. **Content** — YAML frontmatter + a prompt body (the text injected into the claude session when the routine fires).
3. **Source** — `dashboard` (when created via the UI), `claude-code:<workspace>` (when hand-written), or `lake-scheduler` for internal writes.

Example:

```
Tags:   spec, routine, routine-id:gold-check, workspace:trader-agent
Source: dashboard
Content:
---
id: gold-check
name: Gold Price Pulse
schedule: "0 * * * *"
enabled: true
workspace: trader-agent
permission_mode: auto
deleted: false
---

Check the current gold spot price. Compare to the last 24h. Summarize in one sentence.
```

## Frontmatter fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | string | *required* | Stable identifier. Cannot be changed (the `routine-id:` tag carries it). |
| `name` | string | *required* | Human-readable label. Shown in the dashboard. |
| `schedule` | cron | — | 5-field cron string. Evaluated in the container's local TZ. |
| `interval_minutes` | int | — | Used only if `schedule` is absent. Not yet honored by LakeScheduler — prefer `schedule`. |
| `enabled` | bool | `true` | When false, scheduler skips; dashboard shows greyed-out. |
| `workspace` | string | `""` | Maps to a directory under `~/Dropbox/Work/`. The kitty plugin `cd`s there before launching claude. |
| `permission_mode` | `auto` \| `normal` | `auto` | `auto` = claude runs with `--permission-mode auto` (classifier). `normal` = claude prompts for each tool (you approve in the kitty window). |
| `single_fire` | bool | `false` | Planned: fire once then disable. Not yet honored by LakeScheduler. |
| `deleted` | bool | `false` | Tombstone — scheduler and dashboard skip. History stays in the lake. |

## Tag conventions

Three delta families compose a routine's lifecycle:

| Kind | Required tags | Optional tags | Source |
|---|---|---|---|
| **spec** | `spec`, `routine`, `routine-id:<id>` | `workspace:<name>` | `dashboard`, `claude-code:<ws>`, or manual |
| **fire** | `routine-fire`, `routine-id:<id>` | `workspace:<name>`, `permission-mode:<mode>` | `lake-scheduler`, `dashboard`, `manual` |
| **summary** | `routine-summary`, `routine-id:<id>` | `fire-delta:<fire-id>` | `claude-code:routine` (written by the running routine) |

The `fire-delta:<fire-id>` tag on a summary is what lets the dashboard pair a run with its result.

## Lifecycle

```
  spec delta              routine-fire delta         kitty window          routine-summary delta
  (edited by you)         (written by scheduler)     (spawned by           (written by claude
                                                      kitty plugin)         inside the routine)
  ────────────            ───────────────────        ──────────────        ─────────────────────
  [spec, routine,         [routine-fire,             (claude runs          [routine-summary,
   routine-id:X]    ───▶   routine-id:X,       ───▶   the prompt)  ───▶    routine-id:X,
                           workspace:Y]                                     fire-delta:<fire-id>]

  (cron tick)             (lake plugin polls)        (plugin injects       (dashboard pairs
                                                      via kitten @)         fire + summary)
```

One spec → zero or more fires over time → (usually) one summary per fire.

## CRUD

**Create** or **update**: write a new spec delta with the same `routine-id:<id>` tag. The scheduler and dashboard always take the latest-by-timestamp per id.

**Delete**: write a new spec delta with `deleted: true`. Don't literally remove deltas from the lake — history stays.

**Pause**: write a new spec delta with `enabled: false`. Resume = another spec delta with `enabled: true`.

The dashboard's Routines page does all of this through the `POST|PUT|DELETE /api/routines/from-lake` endpoints in `loop-api`. The `fathom delta write` heredoc path still works for scripting.

## Who reads what

- **`loop-api/lake_scheduler.py` (`LakeScheduler`)** — reads spec deltas every 30s, writes fire deltas on cron.
- **`consumer-fathom/agent/plugins/kitty.js` (kitty plugin)** — polls for fire deltas, spawns kitty + claude, writes fire-receipt deltas. Claude itself writes the summary delta from inside the routine.
- **`loop-api/server.py` (`list_routines_from_lake`)** — reads spec, fire, and summary deltas; pairs them; returns enriched list for the dashboard.
- **Dashboard `RoutinesPage`** — renders, and POSTs back to `loop-api` for CRUD.

## Gotchas

- **`routine-id` is immutable.** It's also the stable key across every delta in the lifecycle. Changing it means creating a different routine.
- **Soft-delete != gone from search.** Tombstones still match `fathom delta search`. Filter with `--not-tags deleted` if you want to hide them from queries. (Or filter client-side on `meta.deleted`.)
- **Schedule TZ.** Cron is evaluated in the recall-loop container's local TZ (currently `America/Chicago` per the compose config). If you're away from home, your routines still fire on CST.
- **`interval_minutes` is legacy.** Kept in the parser for back-compat with the older `routines.json` format but LakeScheduler ignores it. Use `schedule`.
- **`single_fire` is not yet honored.** Parser accepts it but the scheduler fires on every cron cycle. Tracked as a follow-up.
