/**
 * Heartbeat — agent presence signal.
 *
 * Writes an `[agent-heartbeat]` delta every N seconds with a short-lived
 * `expires_at` (2x interval). The dashboard polls for unexpired heartbeats
 * to decide whether to show the Routines section — no live agent, no point
 * creating routines whose fires would pile up with nothing to consume them.
 *
 * The heartbeat also surfaces per-plugin state (enabled + key config) so the
 * dashboard can show badges like "routine's permission_mode is not allowed
 * on this agent" without a round-trip.
 *
 * Content format is a JSON blob in the delta's content field, parsed by the
 * consumer API. Tags carry the invariants the dashboard filters on.
 */

import { existsSync, readFileSync, readdirSync } from "fs";
import { homedir, hostname } from "os";
import { dirname, join } from "path";
import { fileURLToPath, pathToFileURL } from "url";

const CONFIG_PATH = join(homedir(), ".fathom", "agent.json");
const VERSION = "0.10.0"; // bumped when heartbeat shape changes

export const CONFIG_SHAPE = {
  interval_ms: { type: "number", required: false, help: "How often to emit a heartbeat (milliseconds). Default: 60000 (1 min)." },
  expiry_ms: { type: "number", required: false, help: "Time before a heartbeat expires. Default: 2x interval_ms." },
};

// Plugin directories to scan for capability declarations. Matches how the
// agent loader resolves plugins: built-ins ship next to this file, customs
// live in ~/.fathom/plugins/. If the loader ever changes its search path,
// this needs to follow.
const BUILTIN_PLUGIN_DIR = dirname(fileURLToPath(import.meta.url));
const CUSTOM_PLUGIN_DIR = join(homedir(), ".fathom", "plugins");

// Secrets scrubbed from instance objects before they go into heartbeat
// payloads. Heartbeats live in the lake and are visible to any reader with
// a lake:read token; credentials stay in agent.json only.
const SECRET_KEYS = /^(token|api[_-]?key|key|secret|password|auth|bearer)$/i;

function scrub(obj) {
  if (!obj || typeof obj !== "object") return obj;
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    if (SECRET_KEYS.test(k)) continue;
    out[k] = v;
  }
  return out;
}

function readPluginConfig() {
  try {
    return JSON.parse(readFileSync(CONFIG_PATH, "utf8")).plugins || {};
  } catch {
    return {};
  }
}

// Walk the plugin dirs once, import each .js, and index its metadata by
// the plugin's own declared name (lowercased) — NOT the filename. The
// loader in agent/index.js does the same keying, but its map is private;
// duplicating the walk here is cheaper than plumbing the registry into
// every plugin. Custom overrides built-in (same precedence as loader).
let _metaMap = null;
async function buildPluginMetaMap() {
  if (_metaMap) return _metaMap;
  const map = new Map();
  const dirs = [
    { dir: BUILTIN_PLUGIN_DIR, source: "built-in" },
    { dir: CUSTOM_PLUGIN_DIR, source: "custom" },  // loaded last so it wins
  ];
  for (const { dir } of dirs) {
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
        });
      } catch (e) {
        console.error(`  heartbeat: failed to read meta for ${file}: ${e.message}`);
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

async function summarizePlugins() {
  const plugins = readPluginConfig();
  const out = {};
  for (const [name, pc] of Object.entries(plugins)) {
    if (!pc || typeof pc !== "object") continue;
    if (name.startsWith("_")) continue;
    const slim = { enabled: !!pc.enabled };

    // Surface the fields the dashboard cares about. Anything secret (tokens,
    // keys) stays in the config file — heartbeat is a capability summary.
    if ("allowed_permission_modes" in pc) slim.allowed_permission_modes = pc.allowed_permission_modes;
    if ("paths" in pc) slim.path_count = Array.isArray(pc.paths) ? pc.paths.length : 0;
    if ("interval" in pc) slim.interval = pc.interval;

    // Multi-instance plugins (e.g. homeassistant) surface their scrubbed
    // instances array so the dashboard can render one chip per instance
    // without hitting the machine for details.
    if (Array.isArray(pc.instances)) {
      slim.instances = pc.instances.map(scrub);
    }

    // Capability descriptor + category. Plugins self-register their shape
    // and their role via default export fields; heartbeat just reports
    // what they say. Dashboard + local UI group by category so "the agent
    // itself" (system) is visually separated from user-facing sources and
    // runtime executors.
    const meta = await readPluginMeta(name);
    if (meta.capabilities) slim.capabilities = meta.capabilities;
    if (meta.category) slim.category = meta.category;

    out[name] = slim;
  }
  return out;
}

async function emitHeartbeat(config, pusher, startedAt) {
  const intervalMs = config.interval_ms || 60000;
  const expiryMs = config.expiry_ms || 2 * intervalMs;
  const host = config.host || hostname();

  const allPluginConfigs = readPluginConfig();
  const localUi = allPluginConfigs.localui;
  // When the local-ui plugin is enabled, advertise its URL so the consumer
  // dashboard can deep-link to it. The URL only resolves from the machine
  // itself (bind defaults to 127.0.0.1), which is the point — it's a local
  // management surface, not a remote one.
  const agent_url = localUi && localUi.enabled
    ? `http://${localUi.bind || "127.0.0.1"}:${localUi.port || 8202}`
    : null;

  const payload = {
    host,
    version: VERSION,
    plugins: await summarizePlugins(),
    uptime_s: Math.round((Date.now() - startedAt) / 1000),
    ...(agent_url ? { agent_url } : {}),
  };

  // Tags: one per enabled plugin so queries like
  //   tags_include=[agent-heartbeat, plugin:kitty]
  // can find agents that currently have kitty on.
  const tags = ["agent-heartbeat", "fathom-agent", `host:${host}`, `version:${VERSION}`];
  for (const [name, p] of Object.entries(payload.plugins)) {
    if (p.enabled) tags.push(`plugin:${name}`);
  }

  const expires_at = new Date(Date.now() + expiryMs).toISOString();

  pusher?.push?.({
    content: JSON.stringify(payload),
    tags,
    source: "fathom-agent",
    expires_at,
  });
}

export default {
  name: "Heartbeat",
  // Core infrastructure — not really a "plugin" in the user sense. If this
  // isn't running, the agent is invisible to the dashboard. Categorized
  // system so UIs hide it behind a fold alongside other plumbing.
  category: "system",
  icon: "💓",
  description: "Periodic agent-alive signal (short expiry).",
  defaults: {
    enabled: true,
    interval_ms: 60000,
    // expiry_ms defaults to 2x interval_ms
  },

  start(config, pusher) {
    const startedAt = Date.now();
    const intervalMs = config.interval_ms || 60000;
    console.log(`  heartbeat: emitting every ${Math.round(intervalMs / 1000)}s`);

    // Emit immediately so the dashboard gets a fast signal on agent startup
    emitHeartbeat(config, pusher, startedAt).catch(() => {});

    const timer = setInterval(
      () => emitHeartbeat(config, pusher, startedAt).catch((e) => console.error("heartbeat failed:", e.message)),
      intervalMs,
    );

    return { stop: () => clearInterval(timer) };
  },
};
