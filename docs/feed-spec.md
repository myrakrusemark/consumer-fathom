# Feed Spec

The feed is what Fathom shows Myra on the dashboard — short cards distilled from the lake, the web, and Fathom's own work on her behalf. It is not a recommender. It is a **provenance generator**: Fathom goes out, finds what Myra cares about, writes it as sediment, and renders the most recent layer.

The feed orients on a `crystal:feed-orient` delta — a task-shaped distillation of "what Myra wants to see right now," regenerated from her engagement over time. The crystal IS the directive; Myra never writes one.

## Why this shape

Three observations drive the design:

1. **Routines require an agent that's not installed by default.** The feed must work out-of-the-box. The fire path lives inside `consumer-api` (Python, in-process), not in a routine on the agent host.
2. **Click is not engagement.** A click opens a chat. The signal is whatever Myra *says* in that chat, plus explicit `more`/`less` reactions. No viewport time, no scroll depth, no implicit scoring.
3. **The directive is not for Myra to write.** Asking Myra to maintain a list of interests is asking her to do Fathom's job. Fathom distills it from her engagement and her conversation. If the model is wrong, Myra corrects it the way she corrects anything else — by talking.

## Anatomy

Three delta families compose the feed lifecycle:

| Kind | Required tags | Optional tags | Source | Lifetime |
|---|---|---|---|---|
| **engagement** | `feed-engagement`, `engagement:<kind>` | `engages:<id>`, `topic:<slug>`, `chat-from:<session>` | `consumer-api` | durable |
| **crystal** | `crystal:feed-orient` | `confidence:<float>` | `consumer-api` | durable, latest-wins |
| **card** | `feed-card` | `topic:<slug>`, `directive-line:<id>` | `fathom-feed` | durable |

The `engages:<id>` tag on an engagement delta points at the card that provoked it — the same pointer primitive used everywhere else in the lake (sediment cites sources via `from:<id>`, rejections via `refutes:<id>`, etc.). Confidence scoring today reads topic directly off the engagement payload, so the card join isn't actually needed — but the pointer is there for any future path that wants it.

## Engagement deltas

The only signals captured:

| Kind | Trigger | Strength |
|---|---|---|
| `engagement:more` | Myra hits the `+` button on a card | strong positive |
| `engagement:less` | Myra hits the `−` button on a card | strong negative |
| `engagement:chat` | Myra sends a message in a chat session opened from a card | positive (sentiment-graded by content) |

Both `+` and `−` ship together. One alone is a dial; both make a *shape* — Myra can disagree as legibly as she can agree.

Engagement delta content is JSON:

```json
{
  "kind": "more",
  "card_id": "feed-card-2026-04-20T1734-weather",
  "topic": "weather",
  "card_excerpt": "first 200 chars of the card's narrative — context for retrieval"
}
```

Chat-engagement deltas are written by the chat listener when a chat session was opened from a feed card. The session-from-card pairing is carried by an existing `seed_card_id` on the session metadata; the listener stamps the `feed-engagement` tag onto the user's first message in that session.

## The crystal

A `crystal:feed-orient` delta is Fathom's current model of "what to put in Myra's feed." Latest wins.

Content is structured JSON so the confidence scorer has something to check:

```json
{
  "version": 1,
  "narrative": "Myra wants weather (rainy/stormy preferred), local-STL things to do with Nova on weekends, AI/tech news with a skeptical lens, home-assistant signals worth surfacing, occasional wardrobe finds. Skip routine-completion noise and anything Fathom said yesterday.",
  "directive_lines": [
    {"id": "weather", "topic": "weather", "freshness_hours": 12, "weight": 0.9, "skip_if": "no precipitation"},
    {"id": "local-nova", "topic": "stl-events", "freshness_hours": 48, "weight": 0.8, "skip_if": "weekday-only events"},
    {"id": "tech-news", "topic": "ai-tech", "freshness_hours": 8, "weight": 0.7, "skip_if": "model launch hype"}
  ],
  "topic_weights": {"weather": 0.9, "stl-events": 0.8, "ai-tech": 0.7, "home-assistant": 0.5, "wardrobe": 0.3, "routine-completion": -1.0},
  "skip_rules": ["routine-completion noise", "normal-range readings", "anything Fathom already said yesterday"]
}
```

The `narrative` is for the LLM to read on every feed-loop fire. The `directive_lines`, `topic_weights`, and `skip_rules` are for the confidence scorer to match against.

## The feed loop

Fires on dashboard page-view, debounced (10 minutes by default). Runs in `consumer-api` (Python, in-process). One LLM session per fire, structured roughly as:

1. Load latest `crystal:feed-orient`. If none, run cold-start path (see below).
2. For each `directive_line`:
   - Check freshness: is the most recent `feed-card` for this `directive-line:<id>` newer than `freshness_hours`? If yes, skip.
   - Else: invoke `fathom_think` with the line's directive + budget. Goal: one card with answer + image + link.
   - Write a `feed-card` delta.
3. Return.

Each line carries a budget: max tool calls (default 8), max wall-clock seconds (default 90). The loop stops on (card complete) OR (budget exhausted) OR (genuinely no answer). "Until satisfied" is bounded; the loop never runs free.

### Cold start

Fresh install, no crystal yet, no engagement deltas. The feed-loop runs with a curiosity-default directive ("what's worth knowing today, broad strokes") and writes cards without `directive-line:<id>` tags. Once ≥10 engagement deltas accumulate, the first crystal regen fires and subsequent loops use the crystal path.

## Crystal regen

Borrows the **mood pattern**, not the identity-crystal pattern: wake-gated, in-process, one focused synthesis call. Not a background poller.

### Trigger predicate

On dashboard wake (the same page-view event that fires the feed loop, but checked first):

```
should_regen = (drift > drift_threshold) OR (confidence < confidence_floor)
                AND (time_since_last_regen > min_cooldown)
                AND (engagement_deltas_since_last_regen >= min_signal)
```

| Variable | Meaning | Default |
|---|---|---|
| `drift` | Cosine distance from engagement-centroid anchor (snapshotted at last accepted regen) | — |
| `drift_threshold` | When drift alone forces a regen | `0.35` |
| `confidence` | Recent-prediction accuracy of current crystal (see below) | — |
| `confidence_floor` | When low confidence forces a regen | `0.55` |
| `min_cooldown` | Don't regen more than once per | `6 hours` |
| `min_signal` | Don't regen on too little signal (cold-start guard) | `10 engagement deltas` |

The `min_signal` guard is the lesson from the 2026-04-19 identity-crystal runaway: missing data fails *open* (skip), not *closed* (fire).

### Synthesis

Reads:
- All `feed-engagement` deltas since last regen
- Last 50 chat-from-card sessions (user-side messages only)
- Recent feed-card deltas (what was already shown)
- The previous crystal (anchor for the synthesis)

LLM call with `FEED_CRYSTAL_DIRECTIVE`. Output is the structured JSON shape above. Writes a `crystal:feed-orient` delta. Snapshots the engagement-centroid as the new drift anchor.

### Confidence scorer

After each crystal regen, Fathom is making a prediction: *the cards we generate from this crystal will get more positive engagement than negative.*

For each engagement delta after the regen:
- Read `topic` and `kind` directly off the engagement delta's JSON payload.
- The crystal predicted this topic was a fit — so `engagement:more` or `engagement:chat` on a positively-weighted topic is a hit, `engagement:less` is a miss.
- Confidence = `(hits + 1) / (hits + misses + 2)`, Laplace-smoothed. Recency decay via pressure-system half-life.

Confidence is recomputed cheaply on every wake. Stored on the crystal delta as `confidence:<float>` for the stats graph.

## Indicator (main page)

When the feed loop is running, the main-page feed section shows a subtle activity indicator next to the "What I noticed" header — a breathing dot or pulse, ~600ms cycle, low-contrast. Disappears the moment the loop returns.

State is read from a single endpoint: `GET /v1/feed/status` returns `{generating: bool, started_at: iso, lines_total: int, lines_done: int}`. UI polls every 1s while the page is visible.

## Stats graph (Stats / ECG)

A new ECG card sits beside the existing pressure-mood and drift cards:

| Layer | Visual | Source |
|---|---|---|
| Engagement-centroid drift | Line graph (like pressure) | `/v1/feed/drift/history` |
| Confidence | Dotted line, 0-1 axis | `/v1/feed/confidence/history` (derived from crystal events) |
| Crystal regen events | Vertical ticks, colored by confidence band (red/amber/green) | `/v1/feed/crystal/events` |
| Engagement events | Tiny + / − marks at the bottom rule | `/v1/feed/engagement/history` |

Same window selector as existing ECG. Same render conventions (`ecg-line-*` classes, SVG namespace).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/feed/engagement` | Write a `feed-engagement` delta. Body: `{kind, card_id, topic?, card_excerpt?}`. |
| `GET` | `/v1/feed/status` | Current loop state for the indicator. |
| `POST` | `/v1/feed/refresh` | Existing — manual kick. Now also fired by the page-view debouncer. |
| `GET` | `/v1/feed/crystal` | Latest `crystal:feed-orient` (for inspection). |
| `GET` | `/v1/feed/crystal/events` | History of crystal regens with confidence. |
| `GET` | `/v1/feed/drift` | Sample current engagement-drift now. |
| `GET` | `/v1/feed/drift/history` | Drift history for the ECG. |
| `GET` | `/v1/feed/confidence/history` | Confidence history for the ECG. |
| `GET` | `/v1/feed/engagement/history` | Engagement marks for the ECG bottom rule. |

## Who reads what

- **`api/feed_crystal.py`** (new) — crystal load/write, regen synthesis, drift sampling, confidence scoring.
- **`api/feed_loop.py`** (new) — page-view debouncer, per-line `fathom_think` orchestration, budget enforcement.
- **`api/server.py`** — endpoints listed above; lifespan-starts the loop module's debouncer.
- **`ui/index.html`** — engagement buttons on each card, chat-from-card pairing, status indicator, new ECG card.
- **`api/chat_listener.py`** — when a chat session was seeded by a feed card (`seed_card_id` on session metadata), tag the user's first message as `feed-engagement` + `engagement:chat`.

## Phases

1. **Engagement plumbing** — `+`/`−` buttons on cards, `POST /v1/feed/engagement`, chat-from-card → `engagement:chat` tagging in the chat listener.
2. **Crystal scaffolding** — `crystal:feed-orient` shape, `feed_crystal.py` with load/write, FEED_CRYSTAL_DIRECTIVE prompt, synthesis call. No regen trigger yet — fires only on `POST /v1/feed/crystal/refresh`.
3. **Engagement-drift + anchor** — engagement-centroid computation, anchor snapshot at regen acceptance, `/v1/feed/drift` + history.
4. **Feed loop** — page-view debouncer, per-line `fathom_think` with budget, freshness checks, cold-start path. `POST /v1/feed/refresh` becomes the in-process entry.
5. **Confidence scorer** — card-to-engagement traceback, hit/miss scoring, confidence written onto crystal deltas.
6. **Wake-gated regen trigger** — predicate (drift OR confidence) + cooldown + min-signal guard; wired into the page-view path so a stale crystal regens before the feed loop reads it.
7. **Indicator + stats graph** — `/v1/feed/status` endpoint, breathing-dot indicator on the main page, new ECG card with the four layers above.

Each phase ends with: works in isolation, can be merged independently, doesn't break the prior phases.

## Gotchas

- **Engagement deltas use `chat-from:<slug>`, not `chat:<slug>`.** The chat listener treats any delta tagged `chat:<slug>` as a session message and fires inference on it. Tagging an engagement delta that way would cause Fathom to respond to the engagement JSON. Use `chat-from:` for the back-link instead — same retrieval ergonomics, no listener collision.
- **`crystal:feed-orient` is latest-wins, not append-only-history-with-tombstones.** Like the identity crystal, the way to "delete" a bad crystal is to regen, not to tombstone.
- **`min_signal` is the cold-start guard.** The identity-crystal runaway happened because "no crystal" + transport error fired infinite regens. Here, "no signal yet" must fail open. The cold-start path runs the loop without a crystal until enough engagement accumulates.
- **The crystal carries its own confidence.** Don't compute confidence from a separate sidecar — read it off the crystal delta itself. Same single-source-of-truth discipline as the identity crystal.
- **Click ≠ engagement.** The chat session itself is the engagement; the user's message in that session is what gets tagged. Don't wire click handlers to write engagement deltas.
- **Drift is anchor-based, not crystal-text-based.** Same lesson as the identity crystal — measure drift from the *lake state at acceptance*, not from the crystal's own embedding, or a crappy crystal can self-trigger.
- **Loop budget is non-negotiable.** "Until satisfied" without a budget is a runaway-cost grenade. Budget per directive line: max 8 tool calls, max 90s wall-clock.
- **Page-view debounce is per-tab-session, not global.** Otherwise opening the dashboard in a second tab during a long synthesis will skip the trigger entirely.

## Open questions

- **Engagement decay.** How fast should an `engagement:more` from 30 days ago lose weight against one from yesterday? Probably the same half-life as the existing pressure system, but worth measuring before fixing.
- **Sentiment grading on `engagement:chat`.** Is "this card is wrong, here's why" a positive (Myra cared enough to correct) or negative (the card was bad)? Probably a small classifier turn at engagement-write time, but v1 can treat all chat-engagement as positive and refine later.
- **Topic taxonomy.** `topic:weather`, `topic:stl-events` — who maintains this? Free-form (whatever the LLM emits per card, drifting over time) or constrained (a fixed enum)? Free-form is more Fathom-shaped; constrained is more measurable. Lean free-form.
- **Multi-contact futures.** When Nova or Bob gets dashboard access (per `contact-spec.md`), each contact gets their own crystal. The `crystal:feed-orient` tag becomes `crystal:feed-orient` + `contact:<slug>`. Out of scope for v1 (Myra-only) but the tag shape leaves room.
