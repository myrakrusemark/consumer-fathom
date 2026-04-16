/**
 * Vault watcher — watches markdown directories, pushes changes as deltas.
 *
 * On file add/change: reads content, chunks if needed, pushes.
 * On file delete: pushes a tombstone delta.
 * Deduplicates by content hash — won't re-push unchanged files.
 */

import { watch } from "chokidar";
import { readFileSync, statSync } from "fs";
import { createHash } from "crypto";
import { basename, relative, extname } from "path";

const MAX_CHUNK = 3000; // chars per delta
const EXTENSIONS = new Set([".md", ".txt", ".org", ".rst"]);

export default {
  name: "Vault",
  icon: "📁",
  type: "watch", // continuous, not polled

  start(config, pusher) {
    const allPaths = config.paths || [];
    // Filter to paths that actually exist
    const paths = allPaths.filter((p) => {
      try { statSync(p); return true; } catch { return false; }
    });
    if (!paths.length) {
      const skipped = allPaths.length ? ` (${allPaths.length} paths not found)` : "";
      console.log(`  vault: no valid paths${skipped}`);
      return null;
    }
    if (paths.length < allPaths.length) {
      const missing = allPaths.filter((p) => !paths.includes(p));
      console.log(`  vault: skipping missing paths: ${missing.join(", ")}`);
    }

    const seen = new Map(); // path → content hash

    const watcher = watch(paths, {
      persistent: true,
      ignoreInitial: false, // process existing files on startup
      ignored: [
        /(^|[\/\\])\../, // dotfiles
        /node_modules/,
        /\.sync-conflict/,
      ],
      awaitWriteFinish: { stabilityThreshold: 500 },
    });

    const source = config.source || "vault";

    watcher.on("add", (filepath) => handleFile(filepath, "add"));
    watcher.on("change", (filepath) => handleFile(filepath, "change"));
    watcher.on("unlink", (filepath) => handleDelete(filepath));

    function handleFile(filepath, event) {
      if (!EXTENSIONS.has(extname(filepath).toLowerCase())) return;

      let content;
      try {
        content = readFileSync(filepath, "utf8");
      } catch {
        return;
      }

      const hash = createHash("md5").update(content).digest("hex");
      if (seen.get(filepath) === hash) return; // unchanged
      seen.set(filepath, hash);

      const name = basename(filepath, extname(filepath));
      const relPath = paths.reduce((best, p) => {
        const r = relative(p, filepath);
        return r.length < best.length ? r : best;
      }, filepath);

      const tags = ["vault-note", `doc:${relPath.replace(/\.[^.]+$/, "")}`];
      if (config.tags) tags.push(...config.tags);

      // Chunk large files
      const chunks = chunk(content, MAX_CHUNK);
      for (const [i, text] of chunks.entries()) {
        const chunkTag = chunks.length > 1 ? [`chunk:${i + 1}/${chunks.length}`] : [];
        pusher.push({
          content: text,
          tags: [...tags, ...chunkTag],
          source,
        });
      }
    }

    function handleDelete(filepath) {
      if (!EXTENSIONS.has(extname(filepath).toLowerCase())) return;
      seen.delete(filepath);

      const relPath = paths.reduce((best, p) => {
        const r = relative(p, filepath);
        return r.length < best.length ? r : best;
      }, filepath);

      pusher.push({
        content: `Vault note deleted: ${basename(filepath)}`,
        tags: ["vault-deletion", "deleted", `doc:${relPath.replace(/\.[^.]+$/, "")}`],
        source: config.source || "vault",
      });
    }

    console.log(`  vault: watching ${paths.join(", ")}`);
    return { stop: () => watcher.close() };
  },
};

function chunk(text, limit) {
  if (text.length <= limit) return [text];
  const chunks = [];
  let remaining = text;
  while (remaining.length > limit) {
    // Try to break at paragraph, then newline, then sentence
    let breakAt = remaining.lastIndexOf("\n\n", limit);
    if (breakAt < limit / 4) breakAt = remaining.lastIndexOf("\n", limit);
    if (breakAt < limit / 4) breakAt = remaining.lastIndexOf(". ", limit);
    if (breakAt < limit / 4) breakAt = limit;
    chunks.push(remaining.slice(0, breakAt + 1).trimEnd());
    remaining = remaining.slice(breakAt + 1).trimStart();
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}
