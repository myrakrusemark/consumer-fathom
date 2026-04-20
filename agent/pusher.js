/**
 * Lake client — batches and POSTs deltas to the consumer API, and reads
 * them back through the same authenticated surface.
 *
 * Named `Pusher` for backwards-compat; it's really the agent's single
 * point of contact with the lake. Plugins that need to read (kitty) call
 * `.query()`; plugins that write (everything else) call `.push()`. Both
 * paths use the same apiUrl + apiKey so there's one auth model and one
 * endpoint the agent has to reach.
 */

const BATCH_INTERVAL = 2000; // ms between flushes

export class Pusher {
  constructor(apiUrl, apiKey) {
    this.apiUrl = apiUrl.replace(/\/$/, "");
    this.apiKey = apiKey;
    this.queue = [];
    this.timer = null;
    this.stats = { pushed: 0, deduped: 0, failed: 0 };
  }

  start() {
    this.timer = setInterval(() => this.flush(), BATCH_INTERVAL);
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    this.flush(); // final flush
  }

  push(delta) {
    this.queue.push(delta);
  }

  _authHeaders() {
    const h = { "Content-Type": "application/json" };
    if (this.apiKey) h["Authorization"] = `Bearer ${this.apiKey}`;
    return h;
  }

  // GET /v1/deltas — for plugins that poll the lake (kitty).
  // Mirrors the delta-store's query shape but routes through the API so auth
  // and any future filtering/caching applies. Throws on non-2xx so callers
  // can distinguish transient failures from empty results.
  async query({ tags_include, source, time_start, limit = 50, timeoutMs = 5000 } = {}) {
    const url = new URL(`${this.apiUrl}/v1/deltas`);
    if (tags_include) url.searchParams.set("tags_include", tags_include);
    if (source) url.searchParams.set("source", source);
    if (time_start) url.searchParams.set("time_start", time_start);
    url.searchParams.set("limit", String(limit));
    const r = await fetch(url, {
      headers: this._authHeaders(),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!r.ok) throw new Error(`${r.status}`);
    return await r.json();
  }

  async flush() {
    if (!this.queue.length) return;
    const batch = this.queue.splice(0);

    const headers = this._authHeaders();

    for (const delta of batch) {
      const preview = (delta.content || "").slice(0, 50).replace(/\n/g, " ");
      const src = delta.source || "?";
      try {
        const r = await fetch(`${this.apiUrl}/v1/deltas`, {
          method: "POST",
          headers,
          body: JSON.stringify(delta),
        });
        if (r.ok) {
          const data = await r.json();
          if (data.deduped) {
            this.stats.deduped++;
          } else {
            this.stats.pushed++;
            console.log(`  ↑ [${src}] ${preview}${delta.content?.length > 50 ? "…" : ""}`);
          }
        } else {
          this.stats.failed++;
          console.error(`  ✗ [${src}] push failed (${r.status}): ${preview}`);
        }
      } catch (e) {
        this.stats.failed++;
        console.error(`  ✗ [${src}] push error: ${e.message}`);
        this.queue.push(delta);
      }
    }
  }
}
