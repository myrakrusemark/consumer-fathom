/**
 * Local UI — tiny HTTP server on the machine itself.
 *
 * Rationale: some agent config is too awkward to live in a flat JSON file
 * (HA instance lists with many entities, per-machine settings) but too
 * sensitive to expose on the cloud dashboard (credentials, veto lists).
 * A localhost-only HTTP server splits the difference: the dashboard links
 * out to it, but only a browser on the same machine can actually open it.
 *
 * Scope (v1):
 *   GET  /                      → serves the local UI HTML (index.html sibling file)
 *   GET  /api/config            → plugins dict from agent.json, secrets scrubbed
 *   POST /api/plugin/:name/instance          → add/update an instance
 *   DEL  /api/plugin/:name/instance/:id      → remove an instance
 *   POST /api/plugin/:name/enabled           → { enabled: bool }
 *
 * What this server does NOT do in v1:
 *   - edit permission_mode / allowed_permission_modes (file-only — the
 *     concern raised during design was accidentally expanding veto lists
 *     from a browser; keep that decision deliberate).
 *   - trigger a live config reload. Writes land in agent.json; the UI
 *     tells the user to restart the agent (systemd restart or equivalent).
 *     A live-reload system is a later phase once we know the edit patterns.
 *
 * Heartbeat coordinates with this plugin: when local-ui is enabled and the
 * server is bound, the heartbeat payload includes agent_url pointing here.
 * The consumer dashboard reads that URL and renders the "configure ↗" link
 * on the matching agent block.
 */

import { createServer } from "http";
import { existsSync, readFileSync, readdirSync, writeFileSync } from "fs";
import { homedir, hostname } from "os";
import { dirname, join } from "path";
import { fileURLToPath, pathToFileURL } from "url";

const CONFIG_PATH = join(process.env.HOME || "", ".fathom", "agent.json");

export const CONFIG_SHAPE = {
  port: { type: "number", required: false, help: "HTTP port for the local UI. Default: 8202." },
  bind: { type: "string", required: false, help: "Bind address. Keep 127.0.0.1 for localhost-only. Default: 127.0.0.1." },
};
const BUILTIN_PLUGIN_DIR = dirname(fileURLToPath(import.meta.url));
const CUSTOM_PLUGIN_DIR = join(homedir(), ".fathom", "plugins");
const BUILTIN_UI_DIR = join(BUILTIN_PLUGIN_DIR, "..", "local-ui");
const SECRET_KEYS = /^(token|api[_-]?key|key|secret|password|auth|bearer)$/i;

// Walk plugin dirs once and index by declared plugin name (not filename)
// — matches the loader's keying exactly. Custom overrides built-in.
let _metaMap = null;
async function buildPluginMetaMap() {
  if (_metaMap) return _metaMap;
  const map = new Map();
  for (const dir of [BUILTIN_PLUGIN_DIR, CUSTOM_PLUGIN_DIR]) {
    if (!existsSync(dir)) continue;
    for (const file of readdirSync(dir)) {
      if (!file.endsWith(".js")) continue;
      const full = join(dir, file);
      try {
        const mod = await import(pathToFileURL(full).href);
        const name = mod.default && mod.default.name;
        if (!name) continue;
        map.set(name.toLowerCase(), {
          capabilities: mod.SOURCE_CAPABILITIES || null,
          category: (mod.default && mod.default.category) || null,
          description: (mod.default && mod.default.description) || null,
          config_shape: mod.CONFIG_SHAPE || null,
        });
      } catch (e) {
        console.error(`  local-ui: failed to read meta for ${file}: ${e.message}`);
      }
    }
  }
  _metaMap = map;
  return map;
}
async function readPluginMeta(name) {
  const map = await buildPluginMetaMap();
  return map.get(name) || { capabilities: null, category: null };
}

function readConfig() {
  try {
    return JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
  } catch {
    return { plugins: {} };
  }
}

function writeConfig(cfg) {
  writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2));
}

function scrubInstance(inst) {
  if (!inst || typeof inst !== "object") return inst;
  const out = {};
  for (const [k, v] of Object.entries(inst)) {
    if (SECRET_KEYS.test(k)) {
      out[`__${k}_set`] = v != null && v !== "";
    } else {
      out[k] = v;
    }
  }
  return out;
}

function scrubPluginConfig(pc) {
  if (!pc || typeof pc !== "object") return pc;
  const out = {};
  for (const [k, v] of Object.entries(pc)) {
    if (SECRET_KEYS.test(k)) {
      out[`__${k}_set`] = v != null && v !== "";
    } else if (k === "instances" && Array.isArray(v)) {
      out[k] = v.map(scrubInstance);
    } else {
      out[k] = v;
    }
  }
  return out;
}

async function scrubFullConfig(cfg) {
  const plugins = {};
  for (const [name, pc] of Object.entries(cfg.plugins || {})) {
    const slim = scrubPluginConfig(pc);
    const meta = await readPluginMeta(name);
    if (meta.capabilities) slim.capabilities = meta.capabilities;
    if (meta.category) slim.category = meta.category;
    if (meta.description) slim.description = meta.description;
    if (meta.config_shape) slim.config_shape = meta.config_shape;
    // Legacy-vault migration, read-only for the UI. If a plugin declares
    // instance_shape and has no instances but does have a legacy `paths`
    // array, synthesize one instance per path so users see their existing
    // config in the UI. First save from the UI persists as real instances.
    if (meta.capabilities?.instance_shape
        && (!Array.isArray(slim.instances) || !slim.instances.length)
        && Array.isArray(slim.paths) && slim.paths.length) {
      slim.instances = slim.paths.map((p, i) => ({
        id: `vault-${i}`,
        name: (p.split("/").filter(Boolean).pop()) || `Vault ${i + 1}`,
        path: p,
        tags: Array.isArray(slim.tags) ? slim.tags : [],
      }));
    }
    plugins[name] = slim;
  }
  return { plugins, api_url: cfg.api_url || "" };
}

// Merge incoming instance against existing — preserve secret fields that
// the caller didn't re-send (UI didn't display them, so it can't re-submit
// them; absence means "keep the old value").
function mergeInstance(existing, incoming) {
  const out = { ...(existing || {}), ...(incoming || {}) };
  for (const [k, v] of Object.entries(incoming || {})) {
    if (SECRET_KEYS.test(k) && (v == null || v === "")) {
      // Caller explicitly cleared the secret? Treat empty string as keep,
      // not clear. Use an explicit `{__clear: true}` marker to actually
      // remove a secret (out of scope for v1; leave existing in place).
      if (existing && k in existing) out[k] = existing[k];
      else delete out[k];
    }
  }
  return out;
}

async function readBody(req) {
  const chunks = [];
  for await (const ch of req) chunks.push(ch);
  if (!chunks.length) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return {};
  }
}

function send(res, status, bodyObj, extraHeaders = {}) {
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    ...extraHeaders,
  });
  res.end(JSON.stringify(bodyObj));
}

function sendHtml(res, html) {
  res.writeHead(200, {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(html);
}

// Stored when start() runs so handle() can trigger plugin reloads after
// config writes. Null while the plugin hasn't been started through the
// agent (e.g. during unit tests); in that case we gracefully fall back
// to the restart-banner path.
let _ctx = null;

async function reloadIfPossible(name) {
  if (!_ctx) return false;
  // Self-reload would restart this very HTTP server mid-response; avoid.
  if (name === "localui") return false;
  try {
    await _ctx.reloadPlugin(name);
    return true;
  } catch (e) {
    console.error(`  local-ui: reload(${name}) failed: ${e.message}`);
    return false;
  }
}

async function handle(req, res, config) {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const path = url.pathname;
  const method = req.method;

  if (method === "GET" && path === "/") {
    try {
      const html = readFileSync(join(BUILTIN_UI_DIR, "index.html"), "utf8");
      const cfg = readConfig();
      // Prefer the friendly host name captured during pairing; fall back
      // to the OS hostname so the UI always has something to show.
      const displayName = (cfg && cfg.host) ? cfg.host : hostname();
      // Agent version from its own package.json — cached once per process
      // since the file doesn't change at runtime.
      let version = "";
      try {
        const pkg = JSON.parse(
          readFileSync(join(BUILTIN_PLUGIN_DIR, "..", "package.json"), "utf8")
        );
        version = pkg.version || "";
      } catch {}
      const rendered = html
        .replaceAll("__FATHOM_HOST__", displayName)
        .replaceAll("__FATHOM_VERSION__", version);
      sendHtml(res, rendered);
    } catch (e) {
      send(res, 500, { error: "ui_html_missing", message: e.message });
    }
    return;
  }

  if (method === "GET" && path === "/api/config") {
    send(res, 200, await scrubFullConfig(readConfig()));
    return;
  }

  // /api/plugin/:name/enabled
  const enabledMatch = path.match(/^\/api\/plugin\/([^/]+)\/enabled$/);
  if (enabledMatch && method === "POST") {
    const name = enabledMatch[1];
    const body = await readBody(req);
    const cfg = readConfig();
    if (!cfg.plugins[name]) return send(res, 404, { error: "not_found" });
    cfg.plugins[name].enabled = !!body.enabled;
    writeConfig(cfg);
    const reloaded = await reloadIfPossible(name);
    return send(res, 200, { ok: true, reloaded, restart_required: !reloaded });
  }

  // /api/plugin/:name/config — singleton plugin config (non-instance fields)
  const cfgMatch = path.match(/^\/api\/plugin\/([^/]+)\/config$/);
  if (cfgMatch && method === "POST") {
    const name = cfgMatch[1];
    const body = await readBody(req);
    const cfg = readConfig();
    if (!cfg.plugins[name]) return send(res, 404, { error: "not_found" });
    // Merge only top-level keys; never touch 'instances' (use the instance
    // endpoint for that) and never let 'enabled' sneak in from this path.
    const existing = cfg.plugins[name];
    const next = { ...existing };
    for (const [k, v] of Object.entries(body || {})) {
      if (k === "instances" || k === "enabled") continue;
      // Secret passthrough: empty string = keep current value, don't clear.
      if (SECRET_KEYS.test(k) && (v == null || v === "")) continue;
      next[k] = v;
    }
    cfg.plugins[name] = next;
    writeConfig(cfg);
    const reloaded = await reloadIfPossible(name);
    return send(res, 200, { ok: true, reloaded, restart_required: !reloaded });
  }

  // /api/plugin/:name/instance
  const addInstMatch = path.match(/^\/api\/plugin\/([^/]+)\/instance$/);
  if (addInstMatch && method === "POST") {
    const name = addInstMatch[1];
    const body = await readBody(req);
    if (!body.id) return send(res, 400, { error: "id_required" });
    const cfg = readConfig();
    if (!cfg.plugins[name]) return send(res, 404, { error: "not_found" });
    cfg.plugins[name].instances = cfg.plugins[name].instances || [];
    const existing = cfg.plugins[name].instances.find((i) => i.id === body.id);
    const merged = mergeInstance(existing, body);
    if (existing) {
      cfg.plugins[name].instances = cfg.plugins[name].instances.map((i) =>
        i.id === body.id ? merged : i,
      );
    } else {
      cfg.plugins[name].instances.push(merged);
    }
    writeConfig(cfg);
    const reloaded = await reloadIfPossible(name);
    return send(res, 200, { ok: true, reloaded, restart_required: !reloaded, instance_id: body.id });
  }

  // /api/plugin/:name/instance/:id
  const delInstMatch = path.match(/^\/api\/plugin\/([^/]+)\/instance\/([^/]+)$/);
  if (delInstMatch && method === "DELETE") {
    const [, name, id] = delInstMatch;
    const cfg = readConfig();
    if (!cfg.plugins[name] || !Array.isArray(cfg.plugins[name].instances)) {
      return send(res, 404, { error: "not_found" });
    }
    const before = cfg.plugins[name].instances.length;
    cfg.plugins[name].instances = cfg.plugins[name].instances.filter((i) => i.id !== id);
    if (cfg.plugins[name].instances.length === before) {
      return send(res, 404, { error: "not_found" });
    }
    writeConfig(cfg);
    const reloaded = await reloadIfPossible(name);
    return send(res, 200, { ok: true, reloaded, restart_required: !reloaded });
  }

  send(res, 404, { error: "not_found" });
}

export default {
  name: "LocalUI",
  category: "system",
  icon: "⚙",
  description: "Serve a localhost-only HTTP server for on-machine agent configuration.",
  defaults: {
    enabled: true,
    port: 8202,
    bind: "127.0.0.1",
  },

  start(config, pusher, context) {
    _ctx = context || null;
    const port = config.port || 8202;
    const bind = config.bind || "127.0.0.1";

    const server = createServer((req, res) => {
      handle(req, res, config).catch((e) => {
        console.error(`  local-ui: handler error: ${e.message}`);
        try {
          send(res, 500, { error: "server_error", message: e.message });
        } catch {}
      });
    });

    server.listen(port, bind, () => {
      console.log(`  local-ui: serving on http://${bind}:${port}`);
    });
    server.on("error", (e) => {
      console.error(`  local-ui: listen failed on ${bind}:${port}: ${e.message}`);
    });

    return {
      stop() {
        server.close();
      },
    };
  },
};
