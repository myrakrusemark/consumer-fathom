/**
 * Vault watcher — watches markdown directories, pushes changes as deltas.
 *
 * On file add/change: reads content, chunks if needed, pushes.
 * On file delete: pushes a tombstone delta.
 * Images: detects ![[image]] and ![](path) references, uploads them.
 * Deduplicates by content hash — won't re-push unchanged files.
 */

import { watch } from "chokidar";
import { readFileSync, writeFileSync, mkdirSync, statSync, existsSync } from "fs";
import { createHash } from "crypto";
import { basename, relative, extname, dirname, join, resolve } from "path";
import { homedir } from "os";

const STATE_PATH = join(homedir(), ".fathom", "vault-state.json");

function loadState() {
  try { return JSON.parse(readFileSync(STATE_PATH, "utf8")); } catch { return {}; }
}

function saveState(state) {
  mkdirSync(dirname(STATE_PATH), { recursive: true });
  writeFileSync(STATE_PATH, JSON.stringify(state));
}

const MAX_CHUNK = 3000; // chars per delta
const MAX_IMAGE_SIZE = 10 * 1024 * 1024; // 10MB
const TEXT_EXTENSIONS = new Set([".md", ".txt", ".org", ".rst"]);
const IMAGE_EXTENSIONS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"]);

// Match ![[file.png]] (Obsidian wikilink) and ![alt](path) (standard markdown)
const WIKILINK_IMG = /!\[\[([^\]]+?)(?:\|[^\]]*?)?\]\]/g;
const MD_IMG = /!\[([^\]]*?)\]\(([^)]+?)\)/g;

export default {
  name: "Vault",
  icon: "📁",
  type: "watch",

  start(config, pusher) {
    const allPaths = config.paths || [];
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

    const diskState = loadState(); // { path: contentHash }
    const seen = new Map(Object.entries(diskState));
    const uploadedImages = new Set(Object.keys(diskState).filter(k => k.startsWith("img:")));
    let fileCount = 0;
    let imageCount = 0;
    let skippedCount = 0;
    let dirty = false;

    function persistState() {
      if (!dirty) return;
      saveState(Object.fromEntries(seen));
      dirty = false;
    }
    // Flush to disk every 10 seconds
    const saveTimer = setInterval(persistState, 10000);

    const watcher = watch(paths, {
      persistent: true,
      ignoreInitial: false,
      ignored: [
        /(^|[\/\\])\./,
        /node_modules/,
        /\.sync-conflict/,
        /\.trash/i,
      ],
      awaitWriteFinish: { stabilityThreshold: 500 },
    });

    const source = config.source || "vault";
    const apiUrl = pusher.apiUrl;
    const apiKey = pusher.apiKey;

    watcher.on("add", (filepath) => handleFile(filepath));
    watcher.on("change", (filepath) => handleFile(filepath));
    watcher.on("unlink", (filepath) => handleDelete(filepath));
    watcher.on("ready", () => {
      console.log(`  vault: initial scan complete — ${fileCount} new, ${imageCount} images, ${skippedCount} unchanged`);
      console.log(`  vault: watching for changes...`);
      persistState();
    });

    function handleFile(filepath) {
      const ext = extname(filepath).toLowerCase();
      if (!TEXT_EXTENSIONS.has(ext)) return;

      let content;
      try {
        content = readFileSync(filepath, "utf8");
      } catch {
        return;
      }

      const hash = createHash("md5").update(content).digest("hex");
      if (seen.get(filepath) === hash) { skippedCount++; return; }
      seen.set(filepath, hash);
      dirty = true;
      fileCount++;

      const relPath = bestRelative(paths, filepath);
      console.log(`  + ${relPath}`);
      const tags = ["vault-note", `doc:${relPath.replace(/\.[^.]+$/, "")}`];
      if (config.tags) tags.push(...config.tags);

      // Extract and upload images
      const images = extractImageRefs(content);
      for (const img of images) {
        const absPath = resolveImagePath(img.src, filepath, paths);
        if (!absPath) continue;
        const imgKey = "img:" + absPath;
        if (uploadedImages.has(imgKey)) continue;
        console.log(`    📷 ${basename(absPath)}`);
        uploadImage(absPath, img.alt || basename(absPath), [...tags, "vault-image", "image"], source, apiUrl, apiKey);
        uploadedImages.add(imgKey);
        seen.set(imgKey, "uploaded");
        dirty = true;
        imageCount++;
      }

      // Chunk and push text
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
      const ext = extname(filepath).toLowerCase();
      if (!TEXT_EXTENSIONS.has(ext)) return;
      seen.delete(filepath);
      dirty = true;

      const relPath = bestRelative(paths, filepath);
      console.log(`  ✗ ${relPath} (deleted)`);
      pusher.push({
        content: `Vault note deleted: ${basename(filepath)}`,
        tags: ["vault-deletion", "deleted", `doc:${relPath.replace(/\.[^.]+$/, "")}`],
        source,
      });
    }

    console.log(`  vault: watching ${paths.join(", ")}`);
    return { stop: () => { persistState(); clearInterval(saveTimer); watcher.close(); } };
  },
};

// ── Helpers ──────────────────────────────────────

function bestRelative(bases, filepath) {
  return bases.reduce((best, p) => {
    const r = relative(p, filepath);
    return r.length < best.length ? r : best;
  }, filepath);
}

function extractImageRefs(markdown) {
  const images = [];

  // Obsidian wikilinks: ![[image.png]] or ![[image.png|alt]]
  let m;
  WIKILINK_IMG.lastIndex = 0;
  while ((m = WIKILINK_IMG.exec(markdown))) {
    images.push({ src: m[1].trim(), alt: "" });
  }

  // Standard markdown: ![alt](path)
  MD_IMG.lastIndex = 0;
  while ((m = MD_IMG.exec(markdown))) {
    const src = m[2].trim();
    // Skip URLs
    if (src.startsWith("http://") || src.startsWith("https://")) continue;
    images.push({ src, alt: m[1] });
  }

  return images;
}

function resolveImagePath(src, mdFile, vaultPaths) {
  // Try relative to the markdown file first
  const fromMd = resolve(dirname(mdFile), src);
  if (existsSync(fromMd) && isImage(fromMd)) return fromMd;

  // Try relative to each vault root (Obsidian stores attachments anywhere)
  for (const vaultRoot of vaultPaths) {
    const fromRoot = join(vaultRoot, src);
    if (existsSync(fromRoot) && isImage(fromRoot)) return fromRoot;

    // Obsidian wikilinks often omit the folder — search recursively
    // Just check common attachment folders
    for (const sub of ["", "attachments", "assets", "images", "media", "files"]) {
      const candidate = join(vaultRoot, sub, src);
      if (existsSync(candidate) && isImage(candidate)) return candidate;
    }
  }

  return null;
}

function isImage(filepath) {
  return IMAGE_EXTENSIONS.has(extname(filepath).toLowerCase());
}

async function uploadImage(absPath, alt, tags, source, apiUrl, apiKey) {
  try {
    const stat = statSync(absPath);
    if (stat.size > MAX_IMAGE_SIZE) return;
  } catch {
    return;
  }

  const headers = {};
  if (apiKey) headers["Authorization"] = `Bearer ${apiKey}`;

  try {
    const file = readFileSync(absPath);
    const form = new FormData();
    form.append("file", new Blob([file]), basename(absPath));
    form.append("content", alt);
    form.append("tags", tags.join(","));
    form.append("source", source);

    await fetch(`${apiUrl}/v1/media/upload`, {
      method: "POST",
      headers,
      body: form,
    });
  } catch {
    // Silent fail — image upload is best-effort
  }
}

function chunk(text, limit) {
  if (text.length <= limit) return [text];
  const chunks = [];
  let remaining = text;
  while (remaining.length > limit) {
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
