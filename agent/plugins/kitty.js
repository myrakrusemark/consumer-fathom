/**
 * Kitty — routine execution surface.
 *
 * Polls the delta lake for `routine-fire` deltas. For each new one, spawns
 * a standalone kitty window with `claude` and injects the routine prompt
 * via kitty's remote-control protocol. The user sees the routine running on
 * their desktop as a real interactive terminal — they can intervene at any
 * time.
 *
 * Fire delta shape:
 *   Tags:    [routine-fire, routine-id:<id>, workspace:<name>]
 *   Source:  any (dashboard, fathom-cli, scheduler, manual)
 *   Content: the prompt to inject into claude
 *
 * State file (~/.fathom/kitty-state.json) tracks the last-processed delta
 * timestamp so restarts don't re-fire historical events.
 */

import { spawn } from "child_process";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { homedir, hostname } from "os";
import { join, dirname } from "path";

const STATE_PATH = join(homedir(), ".fathom", "kitty-state.json");
const SOCKET_DIR = "/tmp";

// Permission modes are deliberately file-only — accidentally widening the
// veto list from a browser is the exact risk the trust discussion flagged.
// Everything else is UI-editable.
export const CONFIG_SHAPE = {
  workspace_root: { type: "string", required: false, help: "Base directory for workspace-pinned routines. Default: ~/Dropbox/Work." },
  claude_command: { type: "string", required: false, help: "Claude CLI binary. Default: 'claude'." },
  kitty_command: { type: "string", required: false, help: "Kitty binary. Default: 'kitty'." },
  kitty_background: { type: "string", required: false, help: "Background hex color for the spawned kitty window. Default: #17303a." },
  auto_submit: { type: "string", required: false, help: "'true' to auto-submit prompts after injection, anything else to wait. Default: true." },
  allowed_permission_modes: { type: "string[]", required: false, editable_from_ui: false, help: "Which claude permission modes routines may request. File-only for safety." },
};

// Map of fire-delta-id → { socket, routineId, launched_at } for open windows.
// When a routine-summary delta lands tagged `fire-delta:<id>` matching one of
// these, the corresponding kitty window is closed via `kitten @ close-window`.
// Entries are pruned after MAX_FIRE_AGE_MS so claude sessions that never write
// a summary don't leak memory indefinitely (the window itself stays open —
// user can close it, or a future idle-watchdog can handle the cleanup).
const openFires = new Map();
const MAX_FIRE_AGE_MS = 6 * 60 * 60 * 1000;  // 6h

function loadState() {
  try {
    return JSON.parse(readFileSync(STATE_PATH, "utf8"));
  } catch {
    return { last_seen: new Date().toISOString() };
  }
}

function saveState(state) {
  mkdirSync(dirname(STATE_PATH), { recursive: true });
  writeFileSync(STATE_PATH, JSON.stringify(state, null, 2));
}

function tag(delta, prefix) {
  const t = (delta.tags || []).find((x) => x.startsWith(prefix));
  return t ? t.slice(prefix.length) : null;
}

function workspacePath(workspaceRoot, workspace) {
  if (!workspace) return workspaceRoot;
  // Avoid path traversal — workspace is a tag value, treat as literal segment
  const safe = workspace.replace(/[^a-zA-Z0-9_-]/g, "");
  return join(workspaceRoot, safe);
}

async function fetchTagged(config, tag, since) {
  const url = new URL(`${config.delta_store_url}/deltas`);
  url.searchParams.set("tags_include", tag);
  if (since) url.searchParams.append("time_start", since);
  url.searchParams.set("limit", "50");
  const r = await fetch(url, { signal: AbortSignal.timeout(5000) });
  if (!r.ok) throw new Error(`${r.status}`);
  return await r.json();
}

async function pollOnce(config, pusher, state) {
  let fires, summaries;
  try {
    [fires, summaries] = await Promise.all([
      fetchTagged(config, "routine-fire", state.last_seen),
      // Summaries poll from the earliest open fire, so a slow routine whose
      // summary lands after state.last_seen advances still gets matched.
      fetchTagged(config, "routine-summary", state.oldest_open_fire || state.last_seen),
    ]);
  } catch (e) {
    console.error(`  kitty: poll failed: ${e.message}`);
    return;
  }

  // Sort oldest-first so we fire in order
  fires.sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));

  for (const d of fires) {
    if (d.timestamp <= state.last_seen) continue; // safety filter
    fire(d, config, pusher);
    state.last_seen = d.timestamp;
  }
  if (fires.length) saveState(state);

  // Close windows whose routine just wrote a summary. Summary tags include
  // `fire-delta:<fire_id>` so we can find the matching open window.
  for (const s of summaries) {
    const fireId = tag(s, "fire-delta:");
    if (!fireId) continue;
    const entry = openFires.get(fireId);
    if (!entry) continue;
    console.log(`  🐈 close ${entry.routineId} (summary ${s.id.slice(0, 8)} landed)`);
    closeWindow(entry.socket);
    openFires.delete(fireId);
  }

  // Prune stale entries whose summary never arrived.
  const now = Date.now();
  for (const [fireId, entry] of openFires) {
    if (now - entry.launched_at > MAX_FIRE_AGE_MS) openFires.delete(fireId);
  }
  // Track the oldest open fire's delta timestamp so the next summary poll
  // reaches back far enough to catch it.
  state.oldest_open_fire = openFires.size
    ? [...openFires.values()].map((e) => e.launched_iso).sort()[0]
    : null;
}

function closeWindow(socket) {
  if (!existsSync(socket)) return;
  runKitten(["@", "--to", `unix:${socket}`, "close-window"], (code, err) => {
    if (code !== 0) console.error(`  ✗ close-window failed (${code}): ${err.trim()}`);
  });
}

// ── Public helpers (used by chat-router + any future engagement plugin) ──

/**
 * Spawn a detached kitty window running `claude` in the given workspace and
 * schedule a prompt injection once the TUI is input-ready.
 *
 * Returns { socket, title, spawnedAt } so callers can inject more text later
 * (via kittySendText) or close the window (via runKitten close-window).
 * Does NOT track state — caller owns the lifecycle map.
 */
export function spawnClaudeInKitty({
  workspaceCwd,
  prompt,
  permissionMode = "auto",
  sessionLabel,                       // e.g. "chat-bubbly-brown-beaver"
  claudeBin = "claude",
  kittyBin = "kitty",
  kittyBackground = "#17303a",
  autoSubmit = true,
  injectDelayMs = 3000,
  pusher,                             // optional — for logging a launch receipt
}) {
  const stamp = Date.now();
  const title = `fathom-${sessionLabel}-${stamp}`;
  const socket = join(SOCKET_DIR, `kitty-${title}`);

  const claudeArgs = claudeArgsForMode(permissionMode);
  const args = [
    "--listen-on", `unix:${socket}`,
    "-o", "allow_remote_control=yes",
    "-o", `background=${kittyBackground}`,
    "--title", title,
    "--directory", workspaceCwd,
    "--detach",
    claudeBin, ...claudeArgs,
  ];
  const child = spawn(kittyBin, args, { stdio: "ignore", detached: true });
  child.unref();
  child.on("error", (e) => console.error(`  kitty spawn failed: ${e.message}`));

  setTimeout(
    () => injectPrompt(socket, prompt, sessionLabel, null, pusher, autoSubmit),
    injectDelayMs,
  );

  return { socket, title, spawnedAt: stamp };
}

/**
 * Send text into an already-running kitty session at `socket`. No enter key —
 * just types the text. Use this for mid-engagement message delivery where the
 * agent's claude-code sees it like the user typed it.
 *
 * Returns a promise resolving to true on success.
 */
export function kittySendText(socket, text, { submit = true } = {}) {
  return new Promise((resolve) => {
    if (!existsSync(socket)) {
      console.error(`  kitty: socket ${socket} not found — window may have closed`);
      resolve(false);
      return;
    }
    runKitten(["@", "--to", `unix:${socket}`, "send-text", text], (code, err) => {
      if (code !== 0) {
        console.error(`  ✗ send-text failed (${code}): ${err.trim()}`);
        resolve(false);
        return;
      }
      if (!submit) { resolve(true); return; }
      setTimeout(() => {
        runKitten(["@", "--to", `unix:${socket}`, "send-key", "enter"], (code2, err2) => {
          if (code2 !== 0) {
            console.error(`  ✗ send-key enter failed (${code2}): ${err2.trim()}`);
            resolve(false);
          } else {
            resolve(true);
          }
        });
      }, 800);
    });
  });
}

/** True if the kitty window at this socket is still alive. */
export function kittySocketAlive(socket) {
  return existsSync(socket);
}

// Map a permission-mode tag value → claude-code CLI args.
// `auto`   → classifier auto-approves safe actions, blocks risky ones
// `normal` → no flag (claude prompts for each tool — user approves in kitty)
// Anything else falls back to normal (defensive).
function claudeArgsForMode(mode) {
  if (mode === "auto") return ["--permission-mode", "auto"];
  return [];
}

function fire(delta, config, pusher) {
  const routineId = tag(delta, "routine-id:") || "unknown";
  const workspace = tag(delta, "workspace:") || "";
  const requestedMode = tag(delta, "permission-mode:") || "auto";
  const targetHost = tag(delta, "host:") || "";

  // ── Host-pinning veto ──
  // A fire with `host:<name>` is reserved for that specific agent. Silently
  // skip fires not addressed to us so every agent's kitty plugin doesn't
  // race to spawn windows for host-pinned routines. Fires with no host tag
  // are fleet-wide and accepted everywhere.
  const myHost = config.host || hostname();
  if (targetHost && targetHost !== myHost) return;

  // ── Agent veto ──
  // The dashboard controls routines; the agent controls its own execution.
  // `allowed_permission_modes` is the local kill switch: anything not in the
  // list is refused with a blocked-locally receipt delta so the dashboard can
  // surface it.
  const allowed = config.allowed_permission_modes || ["auto", "normal"];
  if (!allowed.includes(requestedMode)) {
    console.log(`  🚫 vetoed ${routineId}: mode ${requestedMode} not allowed (allowed: ${allowed.join(",")})`);
    pusher?.push?.({
      content: `[kitty-veto] Fire ${delta.id} for routine ${routineId} blocked locally — permission-mode "${requestedMode}" not in this agent's allow-list (${allowed.join(", ")}).`,
      tags: [
        "kitty-fire-blocked",
        `routine-id:${routineId}`,
        `fire-delta:${delta.id}`,
        `blocked-mode:${requestedMode}`,
      ],
      source: "kitty",
    });
    return;
  }

  const cwd = workspacePath(config.workspace_root, workspace);
  const body = (delta.content || "").trim();
  const footer = [
    "",
    "---",
    "When you finish, write a one-line summary delta with these tags so the dashboard can link it to this run:",
    `\`fathom delta write "[${routineId}] <one-sentence summary>" --tags routine-summary,routine-id:${routineId},fire-delta:${delta.id} --source claude-code:routine\``,
  ].join("\n");
  const prompt = `${body}\n${footer}`;

  console.log(`  🐈 fire ${routineId} (ws: ${workspace || "default"}, mode: ${requestedMode})`);

  const { socket, spawnedAt } = spawnClaudeInKitty({
    workspaceCwd: cwd,
    prompt,
    permissionMode: requestedMode,
    sessionLabel: routineId,
    claudeBin: config.claude_command,
    kittyBin: config.kitty_command,
    kittyBackground: config.kitty_background,
    autoSubmit: config.auto_submit !== false,
    injectDelayMs: config.inject_delay_ms,
    pusher,
  });

  // Track the open window so a matching routine-summary delta can close it.
  openFires.set(delta.id, {
    socket,
    routineId,
    launched_at: spawnedAt,
    launched_iso: new Date(spawnedAt).toISOString(),
  });
}

function injectPrompt(socket, prompt, routineId, fireDeltaId, pusher, autoSubmit = true) {
  if (!existsSync(socket)) {
    console.error(`  kitty: socket ${socket} not found — kitty may have failed to start`);
    return;
  }
  // Two-step injection: send-text writes the prompt into the input field,
  // then send-key enter submits it. Claude-code's TUI treats raw newlines as
  // literal multiline input, not submission — Enter must be a real keypress.
  runKitten(["@", "--to", `unix:${socket}`, "send-text", prompt], (code, err) => {
    if (code !== 0) {
      console.error(`  ✗ send-text failed (${code}): ${err.trim()}`);
      return;
    }
    if (!autoSubmit) {
      console.log(`  ✓ injected ${prompt.length}-char prompt → ${routineId} (awaiting user submit)`);
      return;
    }
    // Claude-code's Ink TUI needs time to commit a pasted multiline buffer
    // before an Enter is interpreted as submit rather than newline. A short
    // 250ms delay proved unreliable — the prompt typed but didn't submit.
    // 800ms gives Ink's re-render loop headroom on slower machines.
    setTimeout(() => {
      runKitten(["@", "--to", `unix:${socket}`, "send-key", "enter"], (code2, err2) => {
        if (code2 !== 0) {
          console.error(`  ✗ send-key enter failed (${code2}): ${err2.trim()}`);
          return;
        }
        console.log(`  ✓ injected + submitted ${prompt.length}-char prompt → ${routineId}`);
        // Receipt only makes sense for routine-fire-driven spawns; chat-router
        // spawns pass fireDeltaId=null and shouldn't pollute the lake with a
        // fake fire-delta: tag.
        if (fireDeltaId && pusher?.push) {
          pusher.push({
            content: `[kitty-fire] routine ${routineId} launched. Prompt: ${prompt.slice(0, 200)}${prompt.length > 200 ? "…" : ""}`,
            tags: ["kitty-fire-receipt", `routine-id:${routineId}`, `fire-delta:${fireDeltaId}`],
            source: "kitty",
          });
        }
      });
    }, 800);
  });
}

function runKitten(args, onDone) {
  const child = spawn("kitten", args, { stdio: "pipe" });
  let err = "";
  child.stderr.on("data", (b) => (err += b.toString()));
  child.on("close", (code) => onDone(code, err));
}

export default {
  name: "Kitty",
  category: "runtime",
  icon: "🐈",
  description: "Spawn kitty windows with claude when routines fire.",
  defaults: {
    delta_store_url: "http://localhost:4246",
    workspace_root: join(homedir(), "Dropbox", "Work"),
    poll_interval_ms: 3000,
    inject_delay_ms: 3000,
    auto_submit: true,
    claude_command: "claude",
    kitty_command: "kitty",
    // Background color for routine-spawned kitty windows. Teal tint so they
    // stand out from regular kitty sessions on the desktop. Any hex color or
    // named color kitty accepts works here.
    kitty_background: "#17303a",
    // Agent veto list: only fires whose permission-mode tag is in this list
    // will spawn kitty. Any other fire writes a [kitty-fire-blocked] receipt
    // delta and is skipped. Set to ["normal"] to refuse all auto-mode routines
    // locally, or [] to stop all routine connectivity while keeping sources.
    allowed_permission_modes: ["auto", "normal"],
  },

  start(config, pusher) {
    const state = loadState();
    const allowed = config.allowed_permission_modes || ["auto", "normal"];
    console.log(`  kitty: polling lake for routine-fire deltas (last seen: ${state.last_seen})`);
    console.log(`  kitty: allowed permission modes = [${allowed.join(", ")}]`);

    const tick = () => pollOnce(config, pusher, state);
    const timer = setInterval(tick, config.poll_interval_ms || 3000);
    tick(); // fire one immediately

    return {
      stop() {
        clearInterval(timer);
        saveState(state);
      },
    };
  },
};
