#!/usr/bin/env node
/**
 * fathom-agent — local agent for the Fathom memory lake.
 *
 * Watches local files, clipboard, and more. Pushes deltas to your lake.
 *
 * Config: ~/.fathom/agent.json
 * Env: FATHOM_API_URL, FATHOM_API_KEY (override config)
 *
 * Usage:
 *   fathom-agent                    # run with config
 *   fathom-agent --vault ~/notes    # quick start: watch a directory
 *   fathom-agent --clipboard        # quick start: watch clipboard
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { homedir } from "os";
import { join } from "path";
import { Pusher } from "./pusher.js";
import vault from "./plugins/vault.js";
import clipboard from "./plugins/clipboard.js";

const CONFIG_DIR = join(homedir(), ".fathom");
const CONFIG_PATH = join(CONFIG_DIR, "agent.json");

// ── Config ───────────────────────────────────────

function loadConfig() {
  const defaults = {
    api_url: "http://localhost:8201",
    api_key: "",
    plugins: {},
  };

  if (existsSync(CONFIG_PATH)) {
    try {
      return { ...defaults, ...JSON.parse(readFileSync(CONFIG_PATH, "utf8")) };
    } catch (e) {
      console.error(`Warning: failed to parse ${CONFIG_PATH}: ${e.message}`);
    }
  }
  return defaults;
}

function saveConfig(config) {
  mkdirSync(CONFIG_DIR, { recursive: true });
  writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2) + "\n");
}

// ── CLI arg parsing ──────────────────────────────

function parseArgs() {
  const args = process.argv.slice(2);
  const result = { vaultPaths: [], clipboard: false, init: false };

  let i = 0;
  while (i < args.length) {
    if (args[i] === "--vault" && args[i + 1]) {
      result.vaultPaths.push(args[i + 1]);
      i += 2;
    } else if (args[i] === "--clipboard") {
      result.clipboard = true;
      i++;
    } else if (args[i] === "--init") {
      result.init = true;
      i++;
    } else if (args[i] === "--help" || args[i] === "-h") {
      console.log(`fathom-agent — local agent for the Fathom memory lake

Usage:
  fathom-agent                       Run with ~/.fathom/agent.json config
  fathom-agent --vault ~/notes       Watch a directory
  fathom-agent --clipboard           Watch clipboard
  fathom-agent --vault ~/a --vault ~/b --clipboard   Combine
  fathom-agent --init                Create default config

Config: ${CONFIG_PATH}
Env: FATHOM_API_URL, FATHOM_API_KEY (override config values)
`);
      process.exit(0);
    } else {
      console.error(`Unknown arg: ${args[i]}. Run fathom-agent --help`);
      process.exit(1);
    }
  }
  return result;
}

// ── Main ─────────────────────────────────────────

async function main() {
  const cliArgs = parseArgs();
  const config = loadConfig();

  // Env overrides config
  const apiUrl = process.env.FATHOM_API_URL || config.api_url;
  const apiKey = process.env.FATHOM_API_KEY || config.api_key;

  if (cliArgs.init) {
    config.api_url = apiUrl;
    config.api_key = apiKey;
    config.plugins = {
      vault: { enabled: false, paths: [], tags: [] },
      clipboard: { enabled: false, tags: [] },
    };
    saveConfig(config);
    console.log(`Config written to ${CONFIG_PATH}`);
    console.log("Edit it to configure your watchers, then run fathom-agent.");
    process.exit(0);
  }

  // Test connection
  const headers = { "Content-Type": "application/json" };
  if (apiKey) headers["Authorization"] = `Bearer ${apiKey}`;
  try {
    const r = await fetch(`${apiUrl}/v1/stats`, { headers });
    if (!r.ok) throw new Error(`${r.status}`);
    const stats = await r.json();
    console.log(`\nfathom-agent connected — ${stats.total?.toLocaleString()} deltas in the lake`);
  } catch (e) {
    console.error(`\nCannot connect to ${apiUrl}: ${e.message}`);
    console.error("Set FATHOM_API_URL and FATHOM_API_KEY, or edit ~/.fathom/agent.json");
    process.exit(1);
  }

  const pusher = new Pusher(apiUrl, apiKey);
  pusher.start();

  const running = [];

  // CLI quick-start overrides config
  if (cliArgs.vaultPaths.length || cliArgs.clipboard) {
    if (cliArgs.vaultPaths.length) {
      const handle = vault.start({ paths: cliArgs.vaultPaths }, pusher);
      if (handle) running.push(handle);
    }
    if (cliArgs.clipboard) {
      const handle = clipboard.start({}, pusher);
      if (handle) running.push(handle);
    }
  } else {
    // Load from config
    const plugins = config.plugins || {};

    if (plugins.vault?.enabled && plugins.vault.paths?.length) {
      const handle = vault.start(plugins.vault, pusher);
      if (handle) running.push(handle);
    }

    if (plugins.clipboard?.enabled) {
      const handle = clipboard.start(plugins.clipboard || {}, pusher);
      if (handle) running.push(handle);
    }

    // Load custom plugins from ~/.fathom/plugins/
    const customDir = join(CONFIG_DIR, "plugins");
    if (existsSync(customDir)) {
      const { readdirSync } = await import("fs");
      for (const file of readdirSync(customDir)) {
        if (!file.endsWith(".js")) continue;
        try {
          const mod = await import(join(customDir, file));
          const plugin = mod.default;
          console.log(`  custom: ${plugin.name || file}`);
          const handle = plugin.start(plugins[plugin.name?.toLowerCase()] || {}, pusher);
          if (handle) running.push(handle);
        } catch (e) {
          console.error(`  custom plugin ${file} failed: ${e.message}`);
        }
      }
    }
  }

  if (!running.length) {
    console.log("\nNo watchers configured. Try:");
    console.log("  fathom-agent --vault ~/Documents/notes");
    console.log("  fathom-agent --clipboard");
    console.log("  fathom-agent --init  (create config file)");
    process.exit(0);
  }

  console.log(`\nWatching... (Ctrl+C to stop)\n`);

  // Stats every 30s
  setInterval(() => {
    if (pusher.stats.pushed > 0 || pusher.stats.failed > 0) {
      console.log(`  [${new Date().toLocaleTimeString()}] pushed: ${pusher.stats.pushed}, failed: ${pusher.stats.failed}`);
    }
  }, 30000);

  // Graceful shutdown
  process.on("SIGINT", () => {
    console.log("\nShutting down...");
    running.forEach((h) => h.stop?.());
    pusher.stop();
    process.exit(0);
  });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
