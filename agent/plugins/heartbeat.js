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

// Dynamic-import a plugin module by name to read its SOURCE_CAPABILITIES
// export. Custom plugins override built-ins (same precedence as the loader
// in agent/index.js). Returns null for plugins that don't declare caps or
// fail to import — callers treat "no declared caps" as the default, not an
// error.
const _capCache = new Map();
async function readPluginCapabilities(name) {
  if (_capCache.has(name)) return _capCache.get(name);
  const candidates = [
    join(CUSTOM_PLUGIN_DIR, `${name}.js`),
    join(BUILTIN_PLUGIN_DIR, `${name}.js`),
  ];
  for (const path of candidates) {
    if (!existsSync(path)) continue;
    try {
      const mod = await import(pathToFileURL(path).href);
      const caps = mod.SOURCE_CAPABILITIES || null;
      _capCache.set(name, caps);
      return caps;
    } catch (e) {
      console.error(`  heartbeat: failed to read capabilities for ${name}: ${e.message}`);
      _capCache.set(name, null);
      return null;
    }
  }
  _capCache.set(name, null);
  return null;
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

    // Capability descriptor (phase 4). Plugins self-register their shape by
    // exporting SOURCE_CAPABILITIES; the dashboard uses this to offer Add-
    // source UI for plugin kinds it has never seen before.
    const caps = await readPluginCapabilities(name);
    if (caps) slim.capabilities = caps;

    out[name] = slim;
  }
  return out;
}

async function emitHeartbeat(config, pusher, startedAt) {
  const intervalMs = config.interval_ms || 60000;
  const expiryMs = config.expiry_ms || 2 * intervalMs;
  const host = config.host || hostname();

  const payload = {
    host,
    version: VERSION,
    plugins: await summarizePlugins(),
    uptime_s: Math.round((Date.now() - startedAt) / 1000),
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
  icon: "💓",
  description: "Periodic agent-alive signal to the lake (short expiry).",
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
