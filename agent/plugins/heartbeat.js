/**
 * Heartbeat — agent presence signal.
 *
 * Writes an `[agent-heartbeat]` delta every N seconds with a long-lived
 * `expires_at` (24h by default). Freshness — i.e. "is this agent actually
 * up right now?" — is computed on the consumer side from the heartbeat's
 * timestamp, not from delta expiry. A stale but still-unexpired delta lets
 * the dashboard render a "disconnected" card for a host that's been seen
 * before but isn't responding, instead of silently dropping it.
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
const SCHEMA_VERSION = "0.10.0"; // bumped when heartbeat payload shape changes

// Agent package version, read from the shipped package.json. Emitted in the
// heartbeat as `agent_version` so the dashboard can compare against the
// registry's "latest" tag and render an update chip when the installed
// agent is behind.
const AGENT_VERSION = (() => {
  try {
    const pkgPath = join(dirname(fileURLToPath(import.meta.url)), "..", "package.json");
    return JSON.parse(readFileSync(pkgPath, "utf8")).version || "0.0.0";
  } catch {
    return "0.0.0";
  }
})();

export const CONFIG_SHAPE = {
  interval_ms: { type: "number", required: false, help: "How often to emit a heartbeat (milliseconds). Default: 60000 (1 min)." },
  expiry_ms: { type: "number", required: false, help: "Time before a heartbeat delta expires (milliseconds). Default: 24h — freshness is computed from the timestamp, not expiry, so long TTL keeps disconnected hosts visible on the dashboard." },
};

// Default TTL: 24h. Long enough for the consumer dashboard to show a
// "disconnected" card for a host that stopped reporting without the delta
// being reaped first.
const DEFAULT_EXPIRY_MS = 24 * 60 * 60 * 1000;

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
    // Agent-level default workspace (kitty plugin). The dashboard reads
    // this to prefill the routine form so the LLM doesn't have to ask
    // "which directory?" — the answer travels with the agent.
    if ("default_workspace" in pc) slim.default_workspace = pc.default_workspace;

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
  const expiryMs = config.expiry_ms || DEFAULT_EXPIRY_MS;
  const host = config.host || hostname();

  const allPluginConfigs = readPluginConfig();
  const localUi = allPluginConfigs.localui;
  // When the local-ui plugin is enabled, advertise a URL the dashboard can
  // probe. Three layers:
  //   1. explicit advertise_url in agent.json — operator override for LAN,
  //      mDNS, reverse-proxy, or tunneled setups.
  //   2. bind + port — works when the viewer's browser is on this same
  //      machine (default bind=127.0.0.1) or on the same LAN (bind=0.0.0.0
  //      will still resolve through loopback for same-machine viewers; for
  //      cross-machine, set advertise_url).
  //   3. null if the plugin is disabled.
  // The dashboard probes /api/identity on the URL before enabling the
  // configure link, so a stale or wrong advertise_url simply disables the
  // link with "not reachable from here" instead of handing the user a
  // broken link.
  let agent_url = null;
  if (localUi && localUi.enabled) {
    if (typeof localUi.advertise_url === "string" && localUi.advertise_url.trim()) {
      agent_url = localUi.advertise_url.trim();
    } else {
      agent_url = `http://${localUi.bind || "127.0.0.1"}:${localUi.port || 8202}`;
    }
  }

  const payload = {
    host,
    agent_version: AGENT_VERSION,
    schema_version: SCHEMA_VERSION,
    // Legacy alias — older dashboards read `version` and treated it as the
    // agent version. Keep it populated with the agent version so an older
    // UI + newer agent behaves correctly. The UI prefers `agent_version`.
    version: AGENT_VERSION,
    plugins: await summarizePlugins(),
    uptime_s: Math.round((Date.now() - startedAt) / 1000),
    ...(agent_url ? { agent_url } : {}),
  };

  // Tags: one per enabled plugin so queries like
  //   tags_include=[agent-heartbeat, plugin:kitty]
  // can find agents that currently have kitty on.
  const tags = ["agent-heartbeat", "fathom-agent", `host:${host}`, `version:${AGENT_VERSION}`];
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
