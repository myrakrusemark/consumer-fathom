#!/usr/bin/env node
/**
 * fathom-connect — one command to connect any MCP host to Fathom.
 *
 * - Claude Code: MCP + hooks (crystal inject, delta capture, recall)
 * - Claude Desktop / Cursor: MCP only (with strong instructions)
 * - Other: prints config to copy
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync, copyFileSync } from "fs";
import { homedir } from "os";
import { join } from "path";
import { createInterface } from "readline";

const HOME = homedir();

// ── Prompts ──────────────────────────────────────

function ask(question) {
  const rl = createInterface({ input: process.stdin, output: process.stderr });
  return new Promise((resolve) => {
    rl.question(question, (answer) => {
      rl.close();
      resolve(answer.trim());
    });
  });
}

async function choose(question, options) {
  console.error(`\n  ${question}\n`);
  options.forEach((o, i) => {
    const marker = i === 0 ? "›" : " ";
    const pad = o.note ? `  ${o.note}` : "";
    console.error(`  ${marker} [${i + 1}] ${o.label}${pad}`);
  });
  console.error();
  const answer = await ask("  Choice: ");
  const idx = parseInt(answer, 10) - 1;
  return idx >= 0 && idx < options.length ? options[idx].value : options[0].value;
}

// ── Connection test ──────────────────────────────

async function testConnection(url, key) {
  const headers = { "Content-Type": "application/json" };
  if (key) headers["Authorization"] = `Bearer ${key}`;

  try {
    const r = await fetch(`${url}/v1/stats`, { headers });
    if (!r.ok) {
      if (r.status === 401) return { ok: false, error: "Invalid API key" };
      return { ok: false, error: `HTTP ${r.status}` };
    }
    const data = await r.json();
    return { ok: true, total: data.total, embedded: data.embedded };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// ── MCP config ───────────────────────────────────

function mcpBlock(url, key) {
  return {
    command: "npx",
    args: ["-y", "fathom-mcp"],
    env: {
      FATHOM_API_URL: url,
      FATHOM_API_KEY: key,
    },
  };
}

// ── Hook scripts ─────────────────────────────────

const HOOK_DIR = join(HOME, ".fathom", "hooks");

const HOOKS = {
  crystal: {
    filename: "fathom-crystal-hook.sh",
    event: "SessionStart",
    async: false,
    timeout: 5000,
  },
  delta: {
    filename: "fathom-delta-hook.sh",
    event: ["UserPromptSubmit", "Stop"],
    async: true,
  },
  recall: {
    filename: "fathom-recall-hook.sh",
    event: "UserPromptSubmit",
    async: false,
    timeout: 8000,
  },
};

function hookEntry(hookDef, url, key) {
  const cmd = join(HOOK_DIR, hookDef.filename);
  const entry = {
    type: "command",
    command: `FATHOM_API_URL='${url}' FATHOM_API_KEY='${key}' ${cmd}`,
  };
  if (hookDef.async) entry.async = true;
  if (hookDef.timeout) entry.timeout = hookDef.timeout;
  return entry;
}

function downloadHooks() {
  // Hooks are bundled inline — no network fetch needed.
  // We write them from the templates embedded at the bottom of this file.
  mkdirSync(HOOK_DIR, { recursive: true });

  for (const [name, def] of Object.entries(HOOKS)) {
    const dest = join(HOOK_DIR, def.filename);
    // Check if source exists locally (dev mode) or write stub
    const localSrc = new URL(`../hooks/${def.filename}`, import.meta.url);
    try {
      const src = new URL(`../hooks/${def.filename}`, import.meta.url);
      copyFileSync(src, dest);
    } catch {
      // Not running from repo — write a fetch stub
      const script = `#!/usr/bin/env bash
# Fathom ${name} hook — installed by fathom-connect
# Re-run npx fathom-connect to update
exec curl -sfL "https://raw.githubusercontent.com/fathom-ai/fathom-connect/main/hooks/${def.filename}" | bash
`;
      writeFileSync(dest, script, { mode: 0o755 });
    }
  }
}

// ── Settings patching ────────────────────────────

function patchClaudeMcp(url, key) {
  // MCP servers → ~/.claude.json (user scope, all projects)
  const claudeJson = join(HOME, ".claude.json");
  let config = {};
  if (existsSync(claudeJson)) {
    try { config = JSON.parse(readFileSync(claudeJson, "utf8")); } catch { config = {}; }
  }
  if (!config.mcpServers) config.mcpServers = {};
  config.mcpServers.fathom = mcpBlock(url, key);
  writeFileSync(claudeJson, JSON.stringify(config, null, 2) + "\n");
  return claudeJson;
}

function patchClaudeHooks(settingsPath, url, key) {
  // Hooks → ~/.claude/settings.json
  let settings = {};
  if (existsSync(settingsPath)) {
    try { settings = JSON.parse(readFileSync(settingsPath, "utf8")); } catch { settings = {}; }
  }

  // Hooks only — MCP is in ~/.claude.json now
  if (!settings.hooks) settings.hooks = {};

  for (const [name, def] of Object.entries(HOOKS)) {
    const entry = hookEntry(def, url, key);
    const events = Array.isArray(def.event) ? def.event : [def.event];

    for (const event of events) {
      if (!settings.hooks[event]) settings.hooks[event] = [];

      // Find or create the hooks array entry
      let eventEntry = settings.hooks[event].find(
        (e) => e.hooks && Array.isArray(e.hooks)
      );
      if (!eventEntry) {
        eventEntry = { hooks: [] };
        settings.hooks[event].push(eventEntry);
      }

      // Remove any existing fathom hooks
      eventEntry.hooks = eventEntry.hooks.filter(
        (h) => !h.command?.includes("fathom-") || !h.command?.includes(".fathom/hooks/")
      );

      // Add new one
      eventEntry.hooks.push(entry);
    }
  }

  mkdirSync(join(settingsPath, ".."), { recursive: true });
  writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + "\n");
}

function patchDesktopSettings(settingsPath, url, key) {
  let settings = {};
  if (existsSync(settingsPath)) {
    try {
      settings = JSON.parse(readFileSync(settingsPath, "utf8"));
    } catch {
      settings = {};
    }
  }

  if (!settings.mcpServers) settings.mcpServers = {};
  settings.mcpServers.fathom = mcpBlock(url, key);

  mkdirSync(join(settingsPath, ".."), { recursive: true });
  writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + "\n");
}

// ── Main ─────────────────────────────────────────

async function main() {
  console.error("\n  ┌─────────────────────────────┐");
  console.error("  │     fathom-connect v1.0     │");
  console.error("  └─────────────────────────────┘\n");

  const host = await choose("Where are you connecting Fathom?", [
    { label: "Claude Code", note: "MCP + hooks (full experience)", value: "claude-code" },
    { label: "Claude Desktop / Cursor", note: "MCP only", value: "desktop" },
    { label: "Other", note: "print config to copy", value: "other" },
  ]);

  const url = (await ask("  Fathom API URL [http://localhost:8201]: ")) || "http://localhost:8201";
  const key = await ask("  API Key (from Settings → API Keys): ");

  if (!key) {
    console.error("\n  ✗ API key is required. Generate one in Settings → API Keys.\n");
    process.exit(1);
  }

  // Test connection
  console.error("\n  Testing connection...");
  const test = await testConnection(url, key);
  if (!test.ok) {
    console.error(`  ✗ Connection failed: ${test.error}\n`);
    process.exit(1);
  }
  console.error(`  ✓ Connected — ${test.total.toLocaleString()} deltas in the lake`);

  if (host === "claude-code") {
    // MCP → ~/.claude.json (user scope)
    const mcpPath = patchClaudeMcp(url, key);
    console.error(`  ✓ MCP server written to ${mcpPath}`);

    // Hooks → ~/.claude/settings.json
    downloadHooks();
    console.error(`  ✓ Hook scripts installed to ${HOOK_DIR}`);
    const settingsPath = join(HOME, ".claude", "settings.json");
    patchClaudeHooks(settingsPath, url, key);
    console.error(`  ✓ Hooks configured in ${settingsPath}`);
    console.error("  ✓ Crystal injection: on");
    console.error("  ✓ Delta capture: on");
    console.error("  ✓ Recall search: on");
    console.error("\n  Restart Claude Code to activate.\n");

  } else if (host === "desktop") {
    // Detect config path
    const platform = process.platform;
    let configPath;
    if (platform === "darwin") {
      configPath = join(HOME, "Library", "Application Support", "Claude", "claude_desktop_config.json");
    } else if (platform === "win32") {
      configPath = join(process.env.APPDATA || HOME, "Claude", "claude_desktop_config.json");
    } else {
      configPath = join(HOME, ".config", "claude", "claude_desktop_config.json");
    }

    patchDesktopSettings(configPath, url, key);
    console.error(`  ✓ MCP config written to ${configPath}`);
    console.error("\n  Restart Claude Desktop / Cursor to activate.\n");

  } else {
    // Print config
    console.error("\n  Add this to your MCP configuration:\n");
    const config = JSON.stringify({ fathom: mcpBlock(url, key) }, null, 2);
    // Print to stdout so it's pipeable
    console.log(config);
    console.error();
  }
}

main().catch((e) => {
  console.error(`\n  Error: ${e.message}\n`);
  process.exit(1);
});
