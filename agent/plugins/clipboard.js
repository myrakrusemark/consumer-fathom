/**
 * Clipboard watcher — polls clipboard for new text, pushes as deltas.
 *
 * Only captures text that's meaningfully different from the last capture.
 * Ignores very short clips (< 10 chars) and exact duplicates.
 */

import clipboard from "clipboardy";

const MIN_LENGTH = 10;

export default {
  name: "Clipboard",
  icon: "📋",
  type: "poll",
  interval: 3000, // ms

  start(config, pusher) {
    let lastHash = "";
    let lastContent = "";
    const source = config.source || "clipboard";
    const tags = ["clipboard", ...(config.tags || [])];

    const timer = setInterval(async () => {
      let text;
      try {
        text = await clipboard.read();
      } catch {
        return; // clipboard not available (headless, etc.)
      }

      if (!text || text.length < MIN_LENGTH) return;
      if (text === lastContent) return;

      // Only push if content actually changed (not just whitespace)
      const trimmed = text.trim();
      if (trimmed === lastContent?.trim()) return;

      lastContent = text;

      const preview = trimmed.slice(0, 60).replace(/\n/g, " ");
      console.log(`  📋 ${preview}${trimmed.length > 60 ? "…" : ""}`);

      pusher.push({
        content: trimmed.slice(0, 4000), // cap at 4k chars
        tags,
        source,
      });
    }, config.interval || 3000);

    console.log("  clipboard: watching");
    return { stop: () => clearInterval(timer) };
  },
};
