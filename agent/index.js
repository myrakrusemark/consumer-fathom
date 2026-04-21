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

import { readFileSync, writeFileSync, readdirSync, mkdirSync, existsSync, copyFileSync } from "fs";
import { homedir, hostname } from "os";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { createInterface } from "readline";
import { execFileSync } from "child_process";
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
    console.log(`Written: ${path}`);
    // restart (not enable --now) so upgrade installs re-exec with the new
    // ExecStart path; enable is separate and idempotent for boot-persistence.
    const steps = [
      ["systemctl", ["--user", "daemon-reload"]],
      ["systemctl", ["--user", "enable", "fathom-agent"]],
      ["systemctl", ["--user", "restart", "fathom-agent"]],
    ];
    try {
      for (const [cmd, args] of steps) {
        process.stdout.write(`  ${cmd} ${args.join(" ")} … `);
        execFileSync(cmd, args, { stdio: "inherit" });
        process.stdout.write("ok\n");
      }
      console.log("\n✓ fathom-agent is running. Check status: systemctl --user status fathom-agent");
    } catch (e) {
      console.error(`\nCouldn't auto-start the service: ${e.message}`);
      console.error("Run these manually:");
      console.error("  systemctl --user daemon-reload");
      console.error("  systemctl --user enable --now fathom-agent");
    }
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
    console.log(`Written: ${path}`);
    try {
      // Unload first in case this is an upgrade of an existing install.
      try { execFileSync("launchctl", ["unload", path], { stdio: "ignore" }); } catch {}
      execFileSync("launchctl", ["load", path], { stdio: "inherit" });
      execFileSync("launchctl", ["start", label], { stdio: "inherit" });
      console.log(`\n✓ fathom-agent is running. Check status: launchctl list | grep ${label}`);
    } catch (e) {
      console.error(`\nCouldn't auto-start the service: ${e.message}`);
      console.error("Run these manually:");
      console.error(`  launchctl load ${path}`);
      console.error(`  launchctl start ${label}`);
    }
  } else if (platform === "win32") {
    const batPath = join(CONFIG_DIR, "fathom-agent.bat");
    writeFileSync(batPath, `@echo off\nset FATHOM_API_URL=${apiUrl}\nset FATHOM_API_KEY=${apiKey}\n"${nodePath}" "${scriptPath}" run\n`);
    console.log(`Written: ${batPath}`);
    try {
      // /f overwrites any existing task so `install` is idempotent (upgrade re-registers).
      execFileSync("schtasks", ["/create", "/tn", "FathomAgent", "/tr", batPath, "/sc", "onlogon", "/rl", "limited", "/f"], { stdio: "inherit" });
      execFileSync("schtasks", ["/run", "/tn", "FathomAgent"], { stdio: "inherit" });
      console.log("\n✓ fathom-agent scheduled task is running. Check: schtasks /query /tn FathomAgent");
    } catch (e) {
      console.error(`\nCouldn't auto-create the scheduled task: ${e.message}`);
      console.error("Run these manually:");
      console.error(`  schtasks /create /tn "FathomAgent" /tr "${batPath}" /sc onlogon /rl limited /f`);
      console.error('  schtasks /run /tn "FathomAgent"');
    }
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

// ── Onboarding (init) ────────────────────────────
//
// `init` is the agent's onboarding entry point. It expects:
//   --api-url    Dashboard/consumer API base URL
//   --pair-code  Short-lived admission token from the dashboard
//
// Both can be omitted; init falls back to interactive prompts. The pair
// code is exchanged for a real API token via POST /v1/pair/redeem, then
// written into agent.json.
//
// Existing config detection: if agent.json exists, init offers three
// branches — keep plugin config and only update url/key (the rotation
// case), overwrite with a fresh default (with timestamped backup), or
// quit. --yes takes the "keep" branch non-interactively.

function prompt(question, defaultValue) {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) => {
    const hint = defaultValue ? ` [${defaultValue}]` : "";
    rl.question(`${question}${hint}: `, (answer) => {
      rl.close();
      resolve((answer || "").trim() || defaultValue || "");
    });
  });
}

function promptChoice(question, choices, defaultKey) {
  const labels = choices.map((c) => (c.key === defaultKey ? `[${c.key.toUpperCase()}]${c.label}` : `${c.key}${c.label}`)).join(" / ");
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) => {
    rl.question(`${question} (${labels}): `, (answer) => {
      rl.close();
      const a = (answer || "").trim().toLowerCase();
      const match = choices.find((c) => c.key.toLowerCase() === a);
      const picked = match ? match : choices.find((c) => c.key === defaultKey);
      // Return the choice's `value` when provided so callers can compare
      // against readable names rather than single-letter keys. Falls back to
      // the key itself for backwards compat with choices that don't define a
      // value.
      resolve(picked ? (picked.value ?? picked.key) : defaultKey);
    });
  });
}

function freshConfigFromPlugins(plugins, apiUrl, apiKey, host) {
  const out = { api_url: apiUrl, api_key: apiKey, host, plugins: {} };
  for (const [name, p] of plugins) {
    out.plugins[name] = {
      enabled: false,
      ...(p.defaults || {}),
      _comment: p.description || `${p.name} plugin`,
    };
  }
  return out;
}

async function redeemPairCode(apiUrl, code, host) {
  const base = apiUrl.replace(/\/$/, "");
  const r = await fetch(`${base}/v1/pair/redeem`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, host }),
  });
  if (!r.ok) {
    const body = await r.text().catch(() => "");
    let detail = body;
    try { detail = JSON.parse(body).detail || body; } catch {}
    throw new Error(`HTTP ${r.status}: ${detail}`);
  }
  return r.json();
}

async function runInit(cliArgs, plugins, existingConfig) {
  const overrides = cliArgs.overrides || {};
  const yes = !!(overrides.yes || overrides.y);
  let apiUrl = overrides["api-url"] || overrides.url || process.env.FATHOM_API_URL || existingConfig.api_url || "http://localhost:8201";
  const pairCode = overrides["pair-code"] || overrides.code || "";
  const host = overrides.host || existingConfig.host || hostname();

  const configExists = existsSync(CONFIG_PATH);
  let branch = "fresh";
  if (configExists) {
    const keyPreview = existingConfig.api_key ? existingConfig.api_key.slice(0, 8) + "…" : "(none)";
    const pluginCount = Object.keys(existingConfig.plugins || {}).length;
    console.log(`\nExisting config: ${CONFIG_PATH}`);
    console.log(`  url:     ${existingConfig.api_url || "(none)"}`);
    console.log(`  key:     ${keyPreview}`);
    console.log(`  plugins: ${pluginCount} configured\n`);

    if (yes) {
      branch = "keep";
    } else {
      branch = await promptChoice(
        "(K)eep plugins & update url/key only, (O)verwrite with defaults (backup saved), or (Q)uit?",
        [
          { key: "k", label: "eep", value: "keep" },
          { key: "o", label: "verwrite", value: "overwrite" },
          { key: "q", label: "uit", value: "quit" },
        ],
        "k",
      );
      if (branch === "quit") { console.log("No changes written."); return; }
    }
  }

  // Ask for URL/pair code if not provided.
  if (!pairCode) {
    if (!cliArgs.overrides["api-url"] && !overrides.url) {
      apiUrl = await prompt("Fathom dashboard URL", apiUrl);
    }
    var code = await prompt("Pair code from your dashboard (starts with 'pair_')", "");
    if (!code) { console.error("Pair code is required to register this agent."); process.exit(1); }
  } else {
    code = pairCode;
  }

  console.log(`\nRedeeming pair code against ${apiUrl}…`);
  let redemption;
  try {
    redemption = await redeemPairCode(apiUrl, code, host);
  } catch (e) {
    console.error(`\nPairing failed: ${e.message}`);
    console.error("Ask for a fresh code in the dashboard and try again.");
    process.exit(1);
  }

  // Ask for a default workspace (only on fresh/overwrite — "keep" leaves
  // existing config alone). Used as the routine form's prefilled workspace
  // value and as the fallback directory when kitty fires an unpinned
  // routine. Blank is fine — when the field is absent or empty, kitty
  // opens claude in $HOME as the "always exists" fallback.
  //
  // --default-workspace=<dir> on the CLI skips the prompt entirely. That
  // flag is what the dashboard's pair modal injects so the whole install
  // is one copy-paste — no second interactive step on the target machine.
  let defaultWorkspace = "";
  if (branch !== "keep") {
    const existingDefault = ((existingConfig.plugins || {}).kitty || {}).default_workspace || "";
    const flagValue = overrides["default-workspace"];
    if (typeof flagValue === "string") {
      defaultWorkspace = flagValue.trim();
    } else {
      defaultWorkspace = await prompt(
        "Default directory for this agent's routines (absolute path or ~-prefix, blank = ~)",
        existingDefault,
      );
    }
  }

  // --localui-advertise-url=<url> lets the dashboard link the "configure ↗"
  // chip to a cross-machine-reachable URL for this agent. Loopback is fine
  // for same-machine dashboards; set this when the dashboard runs elsewhere.
  // Non-interactive: no prompt, flag-only, blank is valid (skip override).
  let advertiseUrl = "";
  if (branch !== "keep") {
    const flagValue = overrides["localui-advertise-url"];
    if (typeof flagValue === "string") {
      advertiseUrl = flagValue.trim();
    }
  }

  const apiKey = redemption.token;
  let nextConfig;
  if (branch === "keep") {
    nextConfig = { ...existingConfig, api_url: apiUrl, api_key: apiKey, host };
  } else if (branch === "overwrite" || branch === "fresh") {
    // Always back up when overwriting an existing file. Previously this only
    // ran in the "overwrite" branch, so any future branch-routing bug that
    // landed here with configExists=true would silently destroy the user's
    // config. The backup is cheap and defensive.
    if (configExists) {
      const bak = `${CONFIG_PATH}.bak-${new Date().toISOString().replace(/[:.]/g, "-")}`;
      copyFileSync(CONFIG_PATH, bak);
      console.log(`Backed up existing config to ${bak}`);
    }
    nextConfig = freshConfigFromPlugins(plugins, apiUrl, apiKey, host);
    if (defaultWorkspace && nextConfig.plugins?.kitty) {
      nextConfig.plugins.kitty.default_workspace = defaultWorkspace;
    }
    if (advertiseUrl && nextConfig.plugins?.localui) {
      nextConfig.plugins.localui.advertise_url = advertiseUrl;
    }
  } else {
    // Unrecognized branch value — surface it rather than silently wiping.
    console.error(`Internal error: unknown branch '${branch}'. Aborting without writing.`);
    process.exit(1);
  }

  saveConfig(nextConfig);
  // Detect launch via npx (process.argv[1] lives under the npx cache) so the
  // follow-up hints match how the user actually invoked this CLI. Otherwise a
  // globally-installed user gets npx advice and an npx user gets `command
  // not found`.
  const launchedViaNpx = /[\\/]_npx[\\/]/.test(process.argv[1] || "");
  const cmd = launchedViaNpx ? "npx fathom-agent" : "fathom-agent";
  console.log(`\n✓ Paired as '${host}'`);
  console.log(`  token id: ${redemption.token_id}`);
  console.log(`  scopes:   ${(redemption.scopes || []).join(", ")}`);
  console.log(`  config:   ${CONFIG_PATH}\n`);
  console.log("Start the agent:");
  console.log(`  ${cmd} run\n`);
  console.log("Or install as a background service:");
  console.log(`  ${cmd} install\n`);
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
    await runInit(cliArgs, plugins, config);
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
  const running = new Map();  // plugin name (lowercase) → start() handle

  // CLI overrides: --vault ~/path becomes { vault: { paths: ["~/path"] } }
  const overrides = cliArgs.overrides;
  const hasOverrides = Object.keys(overrides).length > 0;

  // Top-level config.host (set by `init --host`) is the dashboard
  // identity for this machine. Inject it as the default `host` on every
  // plugin's config so plugin-level `config.host || hostname()` fallbacks
  // still win if someone explicitly sets one, but unset plugins pick up
  // the dashboard-facing name instead of the raw OS hostname.
  function makePluginConfig(name, pluginConfigs, topLevelHost) {
    let pc = {
      ...(topLevelHost ? { host: topLevelHost } : {}),
      ...(pluginConfigs[name] || {}),
    };
    if (overrides[name]) {
      const val = overrides[name];
      pc = { ...pc, enabled: true, paths: typeof val === "string" ? [val] : pc.paths };
    }
    return pc;
  }

  // context — plugins receive this as the third arg to start(). They can
  // use it to trigger a reload of themselves or other plugins after an
  // out-of-band config write (local-ui plugin uses this after a save).
  const context = {
    reloadPlugin: async (nameLower) => {
      const plugin = plugins.get(nameLower);
      if (!plugin) throw new Error(`unknown plugin: ${nameLower}`);

      // Pick up fresh config from disk
      const freshConfig = loadConfig();
      const topLevelHost = freshConfig.host || undefined;
      const pc = makePluginConfig(nameLower, freshConfig.plugins || {}, topLevelHost);

      // Tear down existing
      const existing = running.get(nameLower);
      if (existing?.stop) {
        try { await existing.stop(); }
        catch (e) { console.error(`  ${plugin.name} stop failed: ${e.message}`); }
        running.delete(nameLower);
      }

      // Start fresh if still enabled
      if (!pc.enabled) {
        console.log(`  ${plugin.name}: reloaded — now disabled`);
        return;
      }
      try {
        const handle = plugin.start(pc, pusher, context);
        if (handle) running.set(nameLower, handle);
        console.log(`  ${plugin.name}: reloaded`);
      } catch (e) {
        console.error(`  ${plugin.name} reload failed: ${e.message}`);
      }
    },
    listPlugins: () => [...plugins.keys()],
    getPluginMeta: (nameLower) => plugins.get(nameLower) || null,
  };

  const pluginConfigs = config.plugins || {};
  const topLevelHost = config.host || undefined;

  for (const [name, plugin] of plugins) {
    const pc = makePluginConfig(name, pluginConfigs, topLevelHost);

    if (!hasOverrides && !pc.enabled) continue;
    if (hasOverrides && !overrides[name]) continue;

    try {
      const handle = plugin.start(pc, pusher, context);
      if (handle) running.set(name, handle);
    } catch (e) {
      console.error(`  ${plugin.name} failed: ${e.message}`);
    }
  }

  if (!running.size) {
    console.log("\nNo plugins enabled. Try:");
    console.log("  fathom-agent run --vault ~/Documents/notes");
    console.log("  fathom-agent init");
    process.exit(0);
  }

  console.log(`\nWatching... (Ctrl+C to stop)\n`);

  process.on("SIGINT", () => {
    console.log("\nShutting down...");
    for (const h of running.values()) h.stop?.();
    pusher.stop();
    process.exit(0);
  });
}

main().catch((e) => { console.error(e); process.exit(1); });
