/**
 * Delta pusher — batches and POSTs deltas to the consumer API.
 */

const BATCH_INTERVAL = 2000; // ms between flushes

export class Pusher {
  constructor(apiUrl, apiKey) {
    this.apiUrl = apiUrl.replace(/\/$/, "");
    this.apiKey = apiKey;
    this.queue = [];
    this.timer = null;
    this.stats = { pushed: 0, failed: 0 };
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
      try {
        const r = await fetch(`${this.apiUrl}/v1/deltas`, {
          method: "POST",
          headers,
          body: JSON.stringify(delta),
        });
        if (r.ok) {
          this.stats.pushed++;
        } else {
          this.stats.failed++;
          console.error(`Push failed (${r.status}): ${delta.content?.slice(0, 60)}`);
        }
      } catch (e) {
        this.stats.failed++;
        console.error(`Push error: ${e.message}`);
        // Re-queue on network error
        this.queue.push(delta);
      }
    }
  }
}
