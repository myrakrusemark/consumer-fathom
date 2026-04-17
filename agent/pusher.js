/**
 * Delta pusher — batches and POSTs deltas to the consumer API.
 * Logs every push with source, preview, and dedup status.
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

  async flush() {
    if (!this.queue.length) return;
    const batch = this.queue.splice(0);

    const headers = { "Content-Type": "application/json" };
    if (this.apiKey) headers["Authorization"] = `Bearer ${this.apiKey}`;

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
