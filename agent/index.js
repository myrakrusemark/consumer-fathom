#!/usr/bin/env node
/**
 * fathom-agent — local agent for the Fathom memory lake.
 *
 * Pure plugin runner. Loads plugins from ./plugins/ (built-in) and
 * ~/.fathom/plugins/ (custom). Each plugin is a .js file exporting
 * { name, start(config, pusher) }.
 *
 * Config: ~/.fathom/agent.json
 * Env: FATHOM_API_URL, FATHOM_API_KEY (override config)
 */

import { readFileSync, writeFileSync, readdirSync, mkdirSync, existsSync } from "fs";
import { homedir } from "os";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { Pusher } from "./pusher.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONFIG_DIR = join(homedir(), ".fathom");
const CONFIG_PATH = join(CONFIG_DIR, "agent.json");
const BUILTIN_PLUGINS = join(__dirname, "plugins");
const CUSTOM_PLUGINS = join(CONFIG_DIR, "plugins");

// ── Config ───────────────────────────────────────

function loadConfig() {
  const defaults = { api_url: "http://localhost:8201", api_key: "", plugins: {} };
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

// ── Plugin loader ────────────────────────────────

async function discoverPlugins() {
  const plugins = new Map();

  // Built-in plugins
  if (existsSync(BUILTIN_PLUGINS)) {
    for (const file of readdirSync(BUILTIN_PLUGINS)) {
      if (!file.endsWith(".js")) continue;
      try {
        const mod = await import(join(BUILTIN_PLUGINS, file));
        const plugin = mod.default;
        if (plugin?.name && plugin?.start) {
          plugins.set(plugin.name.toLowerCase(), { ...plugin, source: "built-in" });
        }
      } catch (e) {
        console.error(`  Failed to load built-in plugin ${file}: ${e.message}`);
      }
    }
  }

  // Custom plugins override built-ins
  if (existsSync(CUSTOM_PLUGINS)) {
    for (const file of readdirSync(CUSTOM_PLUGINS)) {
      if (!file.endsWith(".js")) continue;
      try {
        const mod = await import(join(CUSTOM_PLUGINS, file));
        const plugin = mod.default;
        if (plugin?.name && plugin?.start) {
          plugins.set(plugin.name.toLowerCase(), { ...plugin, source: "custom" });
        }
      } catch (e) {
        console.error(`  Failed to load custom plugin ${file}: ${e.message}`);
      }
    }
  }

  return plugins;
}

// ── CLI ──────────────────────────────────────────

function parseArgs() {
  const args = process.argv.slice(2);
  const result = { command: null, overrides: {} };

  if (!args.length) { result.command = "help"; return result; }

  const cmd = args[0];
  if (["run", "--run", "init", "--init", "install", "--install", "uninstall", "--uninstall", "status", "--status", "help", "--help", "-h"].includes(cmd)) {
    result.command = cmd.replace(/^-+/, "");
  } else if (cmd.startsWith("--")) {
    // Treat bare flags as "run" with overrides
    result.command = "run";
  } else {
    console.error(`Unknown command: ${cmd}\nRun 'fathom-agent help' for usage.`);
    process.exit(1);
  }

  // Parse --<plugin> flags as runtime overrides
  let i = result.command === cmd.replace(/^-+/, "") ? 1 : 0;
  while (i < args.length) {
    if (args[i].startsWith("--") && args[i + 1] && !args[i + 1].startsWith("--")) {
      const name = args[i].replace(/^-+/, "");
      result.overrides[name] = args[i + 1];
      i += 2;
    } else if (args[i].startsWith("--")) {
      const name = args[i].replace(/^-+/, "");
      result.overrides[name] = true;
      i++;
    } else {
      i++;
    }
  }
  return result;
}

function showHelp(plugins) {
  console.log(`fathom-agent — local agent for the Fathom memory lake

Commands:
  fathom-agent run                   Start watching (uses config)
  fathom-agent run --vault ~/notes   Override plugin paths
  fathom-agent init                  Create default config
  fathom-agent install               Install as system service
  fathom-agent uninstall             Remove system service
  fathom-agent status                Show config and connection
  fathom-agent help                  Show this help

Config: ${CONFIG_PATH}
Env:    FATHOM_API_URL, FATHOM_API_KEY`);

  if (plugins?.size) {
    console.log(`\nPlugins:`);
    for (const [name, p] of plugins) {
      console.log(`  ${p.icon || "•"} ${p.name} (${p.source})`);
    }
  }

  console.log(`\nCustom plugins: drop .js files in ${CUSTOM_PLUGINS}/\n`);
}

// ── Service installer ────────────────────────────

function installService(config) {
  const platform = process.platform;
  const nodePath = process.execPath;
  const scriptPath = fileURLToPath(new URL("index.js", import.meta.url));
  const apiUrl = process.env.FATHOM_API_URL || config.api_url || "http://localhost:8201";
  const apiKey = process.env.FATHOM_API_KEY || config.api_key || "";

  if (platform === "linux") {
    const unit = `[Unit]
Description=Fathom Agent
After=network.target

[Service]
Type=simple
ExecStart=${nodePath} ${scriptPath} run
Environment=FATHOM_API_URL=${apiUrl}
Environment=FATHOM_API_KEY=${apiKey}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
`;
    const dir = join(homedir(), ".config", "systemd", "user");
    const path = join(dir, "fathom-agent.service");
    mkdirSync(dir, { recursive: true });
    writeFileSync(path, unit);
    console.log(`Written: ${path}\n\nRun:\n  systemctl --user daemon-reload\n  systemctl --user enable fathom-agent\n  systemctl --user start fathom-agent`);
  } else if (platform === "darwin") {
    const label = "com.fathom.agent";
    const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key><array><string>${nodePath}</string><string>${scriptPath}</string><string>run</string></array>
  <key>EnvironmentVariables</key><dict><key>FATHOM_API_URL</key><string>${apiUrl}</string><key>FATHOM_API_KEY</key><string>${apiKey}</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${join(CONFIG_DIR, "agent.log")}</string>
  <key>StandardErrorPath</key><string>${join(CONFIG_DIR, "agent.err")}</string>
</dict></plist>`;
    const path = join(homedir(), "Library", "LaunchAgents", `${label}.plist`);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, plist);
    console.log(`Written: ${path}\n\nRun:\n  launchctl load ${path}\n  launchctl start ${label}`);
  } else if (platform === "win32") {
    const batPath = join(CONFIG_DIR, "fathom-agent.bat");
    writeFileSync(batPath, `@echo off\nset FATHOM_API_URL=${apiUrl}\nset FATHOM_API_KEY=${apiKey}\n"${nodePath}" "${scriptPath}" run\n`);
    console.log(`Written: ${batPath}\n\nRun:\n  schtasks /create /tn "FathomAgent" /tr "${batPath}" /sc onlogon /rl limited\n  schtasks /run /tn "FathomAgent"`);
  }
}

function uninstallService() {
  const platform = process.platform;
  if (platform === "linux") {
    const path = join(homedir(), ".config", "systemd", "user", "fathom-agent.service");
    console.log(existsSync(path) ? `Run:\n  systemctl --user stop fathom-agent\n  systemctl --user disable fathom-agent\n  rm ${path}\n  systemctl --user daemon-reload` : "No service found.");
  } else if (platform === "darwin") {
    const path = join(homedir(), "Library", "LaunchAgents", "com.fathom.agent.plist");
    console.log(existsSync(path) ? `Run:\n  launchctl stop com.fathom.agent\n  launchctl unload ${path}\n  rm ${path}` : "No service found.");
  } else if (platform === "win32") {
    console.log('Run:\n  schtasks /delete /tn "FathomAgent" /f');
  }
}

// ── Main ─────────────────────────────────────────

async function main() {
  const cliArgs = parseArgs();
  const config = loadConfig();
  const apiUrl = process.env.FATHOM_API_URL || config.api_url;
  const apiKey = process.env.FATHOM_API_KEY || config.api_key;
  const plugins = await discoverPlugins();

  if (cliArgs.command === "help" || cliArgs.command === "h") {
    showHelp(plugins);
    process.exit(0);
  }

  if (cliArgs.command === "install") { installService(config); process.exit(0); }
  if (cliArgs.command === "uninstall") { uninstallService(); process.exit(0); }

  if (cliArgs.command === "init") {
    config.api_url = apiUrl;
    config.api_key = apiKey;
    config.plugins = {};
    for (const [name, p] of plugins) {
      config.plugins[name] = {
        enabled: false,
        ...(p.defaults || {}),
        _comment: p.description || `${p.name} plugin`,
      };
    }
    saveConfig(config);
    console.log(`Config written to ${CONFIG_PATH}`);
    console.log(`Enable plugins and adjust settings, then run 'fathom-agent run'.`);
    process.exit(0);
  }

  if (cliArgs.command === "status") {
    console.log(`\nConfig: ${CONFIG_PATH}`);
    console.log(`API:    ${apiUrl}`);
    console.log(`Key:    ${apiKey ? apiKey.slice(0, 8) + "…" : "(not set)"}`);
    console.log(`\nPlugins (${plugins.size} available):`);
    const pluginConfigs = config.plugins || {};
    for (const [name, p] of plugins) {
      const pc = pluginConfigs[name] || {};
      const status = pc.enabled ? "enabled" : "disabled";
      console.log(`  ${p.icon || "•"} ${p.name}: ${status} (${p.source})`);
    }
    try {
      const r = await fetch(`${apiUrl}/health`);
      console.log(`\nConnection: ${r.ok ? "ok" : r.status}`);
    } catch (e) {
      console.log(`\nConnection: failed (${e.message})`);
    }
    console.log();
    process.exit(0);
  }

  // ── Run ──
  if (cliArgs.command !== "run") { showHelp(plugins); process.exit(0); }

  try {
    const r = await fetch(`${apiUrl}/health`);
    if (!r.ok) throw new Error(`${r.status}`);
    console.log(`\nfathom-agent connected to ${apiUrl}`);
  } catch (e) {
    console.error(`\nCannot connect to ${apiUrl}: ${e.message}`);
    console.error("Set FATHOM_API_URL and FATHOM_API_KEY, or edit ~/.fathom/agent.json");
    process.exit(1);
  }

  const pusher = new Pusher(apiUrl, apiKey);
  pusher.start();
  const running = [];
  const pluginConfigs = config.plugins || {};

  // CLI overrides: --vault ~/path becomes { vault: { paths: ["~/path"] } }
  const overrides = cliArgs.overrides;
  const hasOverrides = Object.keys(overrides).length > 0;

  for (const [name, plugin] of plugins) {
    let pc = pluginConfigs[name] || {};

    // Apply CLI overrides
    if (overrides[name]) {
      const val = overrides[name];
      // If the override is a path string, treat as paths array
      pc = { ...pc, enabled: true, paths: typeof val === "string" ? [val] : pc.paths };
    }

    if (!hasOverrides && !pc.enabled) continue;
    if (hasOverrides && !overrides[name]) continue;

    try {
      const handle = plugin.start(pc, pusher);
      if (handle) running.push(handle);
    } catch (e) {
      console.error(`  ${plugin.name} failed: ${e.message}`);
    }
  }

  if (!running.length) {
    console.log("\nNo plugins enabled. Try:");
    console.log("  fathom-agent run --vault ~/Documents/notes");
    console.log("  fathom-agent init");
    process.exit(0);
  }

  console.log(`\nWatching... (Ctrl+C to stop)\n`);

  setInterval(() => {
    const s = pusher.stats;
    if (s.pushed > 0 || s.deduped > 0 || s.failed > 0) {
      const parts = [`pushed: ${s.pushed}`];
      if (s.deduped) parts.push(`deduped: ${s.deduped}`);
      if (s.failed) parts.push(`failed: ${s.failed}`);
      console.log(`  [${new Date().toLocaleTimeString()}] ${parts.join(", ")}`);
    }
  }, 30000);

  process.on("SIGINT", () => {
    console.log("\nShutting down...");
    running.forEach((h) => h.stop?.());
    pusher.stop();
    process.exit(0);
  });
}

main().catch((e) => { console.error(e); process.exit(1); });
