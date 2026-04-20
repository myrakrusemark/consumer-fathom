/**
 * Chat-router — bridge between lake chat sessions and local claude-code.
 *
 * Watches the lake for deltas tagged `to:agent:<this-host>` with a `chat:<slug>`
 * tag. For each one:
 *   - if no engagement exists for that session yet, spawn claude-code in a
 *     kitty window with an orient prompt that tells it to search the lake for
 *     the session and tag its outgoing deltas so they land in chat.
 *   - if an engagement IS live, inject the message as a framed user message
 *     into the running claude-code session.
 *
 * Also watches deltas in *currently engaged* sessions from other participants
 * (user, fathom, other agents) so claude-code hears the full conversation
 * while it's working, not just the message addressed to it.
 *
 * See `consumer-fathom/CLAUDE.md` → "Chat sessions" for the tag contract this
 * plugin implements.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { homedir, hostname } from "os";
import { join, dirname } from "path";
import { spawnClaudeInKitty, kittySendText, kittySocketAlive, closeWindow } from "./kitty.js";

const STATE_PATH = join(homedir(), ".fathom", "chat-router-state.json");

export const CONFIG_SHAPE = {
  poll_interval_ms: { type: "number", required: false, help: "Ms between lake polls. Default: 2000." },
  workspace_root: { type: "string", required: false, help: "Base directory for chat-session spawns. Default: ~/Dropbox/Work." },
  default_workspace: { type: "string", required: false, help: "Fallback workspace subdir if the routing delta has no workspace: tag. Default: empty (workspace_root itself)." },
  permission_mode: { type: "string", required: false, help: "Claude permission mode for chat engagements. 'auto' or 'normal'. Default: auto." },
  kitty_background: { type: "string", required: false, help: "Hex color for chat-engagement kitty windows. Distinct from routines so you can tell them apart. Default: #1a2e3a." },
};

// In-memory engagement map. Key: session slug. Value: { socket, title,
// spawnedAt, sessionSlug }. When the kitty socket disappears, we consider the
// engagement ended and drop the entry on the next poll.
const engagements = new Map();

// Track the newest delta timestamp we've processed, so a restart doesn't
// re-fire historical invitations. Persisted to disk.
//   last_seen              — agent-mode invitations (to:agent:<host>)
//   last_seen_fathom_cc    — Fathom-mode invitations (fathom-mode:cc)
// Separate cursors because the two queries run independently; sharing one
// cursor would let a fast-moving poll skip the other mode's pending deltas.
function loadState() {
  try {
    const s = JSON.parse(readFileSync(STATE_PATH, "utf8"));
    if (!s.last_seen_fathom_cc) s.last_seen_fathom_cc = s.last_seen || new Date().toISOString();
    return s;
  } catch {
    const now = new Date().toISOString();
    return { last_seen: now, last_seen_fathom_cc: now };
  }
}

// Fetch the CC-Fathom orient prompt from the consumer-api. Centralizing the
// orient on the server keeps SYSTEM_PREAMBLE as the single source of truth —
// chat-router doesn't duplicate Fathom's voice, it just pipes what the server
// assembles (preamble + session block + mood, crystal skipped).
async function fetchFathomModeOrient(pusher, sessionSlug) {
  const url = `${pusher.apiUrl}/v1/cc-orient?session=${encodeURIComponent(sessionSlug)}`;
  const headers = {};
  if (pusher.apiKey) headers["Authorization"] = `Bearer ${pusher.apiKey}`;
  const r = await fetch(url, { headers, signal: AbortSignal.timeout(10000) });
  if (!r.ok) throw new Error(`/v1/cc-orient ${r.status}`);
  return await r.text();
}

function saveState(state) {
  mkdirSync(dirname(STATE_PATH), { recursive: true });
  writeFileSync(STATE_PATH, JSON.stringify(state, null, 2));
}

function tagValue(delta, prefix) {
  const t = (delta.tags || []).find((x) => x.startsWith(prefix));
  return t ? t.slice(prefix.length) : null;
}

function allTagValues(delta, prefix) {
  return (delta.tags || [])
    .filter((x) => x.startsWith(prefix))
    .map((x) => x.slice(prefix.length));
}

function workspacePath(root, sub) {
  if (!sub) return root;
  const safe = sub.replace(/[^a-zA-Z0-9_-]/g, "");
  return join(root, safe);
}

function orientPrompt({ host, sessionSlug, otherSessions }) {
  const others = otherSessions.length
    ? ` You may also see deltas tagged ${otherSessions.map((s) => `chat:${s}`).join(", ")} — those are other sessions this message addressed you in.`
    : "";
  return [
    `You are the local agent on \`${host}\`. You've been called into chat session \`${sessionSlug}\`.${others}`,
    ``,
    `## Orient first`,
    `Search the lake for the session history — \`remember\` with \`tags_include: ["chat:${sessionSlug}"]\` and limit 30. The most recent delta with \`to:agent:${host}\` in it is what called you in; read it to understand what's being asked. Older deltas give you context.`,
    ``,
    `## While you work, you may receive live messages`,
    `Other participants (the human, Fathom, other agents) may write more deltas into this session while you're working. The framework will deliver them to you framed as:`,
    `> Message from <participant> in chat:${sessionSlug}: <content>`,
    `Respond only if it's necessary — sometimes they're just watching. If you do respond, write a delta with the session tag.`,
    ``,
    `## Every delta you write must carry the session tag`,
    `Any observation, tool output, partial result, or message you want in the conversation needs \`chat:${sessionSlug}\` in its tags. Use source \`claude-code:chat\`. Tag your role as \`participant:agent:${host}\`.`,
    ``,
    `## Signoff`,
    `When you're done, write a final delta tagged \`chat:${sessionSlug}\` AND \`signoff\` AND \`participant:agent:${host}\`. Short — one sentence summary of what you did. That's how participants know you've left.`,
    ``,
    `Now go — orient on the session and do what the caller asked.`,
  ].join("\n");
}

function framedMessage({ sessionSlug, participant, content }) {
  // Keep it short — the agent sees this as if the user typed it into claude.
  // Role label helps it distinguish user from Fathom from another agent.
  const who = participant || "someone";
  return `\nMessage from ${who} in chat:${sessionSlug}: ${content}\n`;
}

async function pollInvitations(pusher, config, state, host) {
  // Fetch every delta that addresses this host. The lake's tags_include is
  // AND-semantic per call, so one fetch per addressing tag is enough — we
  // don't need a cross-product of chat:* × to:agent:host.
  let invites;
  try {
    invites = await pusher.query({
      tags_include: `to:agent:${host}`,
      time_start: state.last_seen,
      limit: 100,
    });
  } catch (e) {
    const cause = e.cause ? ` (cause: ${e.cause.code || e.cause.message})` : "";
    console.error(`  chat-router: invite poll failed: ${e.message}${cause}`);
    return;
  }

  invites.sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));

  for (const d of invites) {
    if (d.timestamp <= state.last_seen) continue;
    handleInvite(d, config, host);
    state.last_seen = d.timestamp;
  }
  if (invites.length) saveState(state);
}

function handleInvite(delta, config, host) {
  const sessionSlugs = allTagValues(delta, "chat:");
  if (!sessionSlugs.length) {
    // Non-chat routing (no chat: tag). Ignore for now — future expansion.
    console.log(`  chat-router: skipping to:agent:${host} delta ${delta.id.slice(0, 8)} (no chat: tag)`);
    return;
  }

  // Multi-session invites: pick the first as primary, list the rest in the
  // orient prompt so the agent knows it can tag outputs for all of them.
  const [primary, ...others] = sessionSlugs;

  if (engagements.has(primary)) {
    // Engagement already live — the invite doesn't re-spawn, but we still
    // deliver the message into the running session so the agent hears it.
    injectIntoEngagement(primary, delta, host);
    return;
  }

  const workspaceName = tagValue(delta, "workspace:") || config.default_workspace || "";
  const cwd = workspacePath(config.workspace_root, workspaceName);

  console.log(`  💬 engage ${primary} (ws: ${workspaceName || "default"})`);

  const prompt = orientPrompt({ host, sessionSlug: primary, otherSessions: others });
  const { socket, title, spawnedAt } = spawnClaudeInKitty({
    workspaceCwd: cwd,
    prompt,
    permissionMode: config.permission_mode || "auto",
    sessionLabel: `chat-${primary}`,
    kittyBackground: config.kitty_background || "#1a2e3a",
    autoSubmit: true,
    injectDelayMs: 3000,
  });

  engagements.set(primary, {
    socket,
    title,
    spawnedAt,
    // Kitty takes a few seconds to launch and open its control socket. Skip
    // the socket-missing eviction until we're past this grace window,
    // otherwise the very first liveness poll after spawn wrongly concludes
    // the engagement ended.
    graceUntil: spawnedAt + 10_000,
    sessionSlug: primary,
    coSessions: others,
    since: delta.timestamp || new Date(spawnedAt).toISOString(),
    ownSource: "claude-code:chat",
  });
}

async function pollFathomModeInvitations(pusher, config, state) {
  // Fathom-mode deltas are tagged `fathom-mode:cc` by the consumer-api when
  // the user toggles the robot icon. Any fathom-agent sees these; for single-
  // host setups (the common case) exactly one chat-router engages. Multi-host
  // scoping would layer a to:agent:<host> tag on top; for v1 we assume one.
  let invites;
  try {
    invites = await pusher.query({
      tags_include: "fathom-mode:cc",
      time_start: state.last_seen_fathom_cc,
      limit: 100,
    });
  } catch (e) {
    const cause = e.cause ? ` (cause: ${e.cause.code || e.cause.message})` : "";
    console.error(`  chat-router: fathom-mode invite poll failed: ${e.message}${cause}`);
    return;
  }

  invites.sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));

  for (const d of invites) {
    if (d.timestamp <= state.last_seen_fathom_cc) continue;
    await handleFathomModeInvite(d, pusher, config);
    state.last_seen_fathom_cc = d.timestamp;
  }
  if (invites.length) saveState(state);
}

async function handleFathomModeInvite(delta, pusher, config) {
  const sessionSlugs = allTagValues(delta, "chat:");
  if (!sessionSlugs.length) {
    console.log(`  chat-router: skipping fathom-mode:cc delta ${delta.id.slice(0, 8)} (no chat: tag)`);
    return;
  }
  const [primary] = sessionSlugs;

  // Live engagement for this session → inject the new message. Works whether
  // the existing engagement is agent-mode or fathom-mode; the live-inject
  // path is the same either way.
  if (engagements.has(primary)) {
    await injectIntoEngagement(primary, delta, null);
    return;
  }

  // Fresh spawn in Fathom-mode. Workspace defaults to the agent's
  // workspace_root — Fathom-mode is about answering with the mind's own hands
  // on this host, not dispatching to a per-project workspace.
  const cwd = workspacePath(config.workspace_root, config.default_workspace || "");

  let prompt;
  try {
    prompt = await fetchFathomModeOrient(pusher, primary);
  } catch (e) {
    console.error(`  chat-router: fathom-mode orient fetch failed for ${primary}: ${e.message}`);
    return;
  }

  console.log(`  🤖 fathom-mode engage ${primary}`);

  const { socket, title, spawnedAt } = spawnClaudeInKitty({
    workspaceCwd: cwd,
    prompt,
    permissionMode: config.permission_mode || "auto",
    sessionLabel: `fathom-${primary}`,
    // Distinct background so the Fathom-mode kitty is visually separable from
    // agent-mode. Dark plum against agent-mode's teal.
    kittyBackground: config.fathom_mode_background || "#2a1a3a",
    autoSubmit: true,
    injectDelayMs: 2000,
  });

  engagements.set(primary, {
    socket,
    title,
    spawnedAt,
    graceUntil: spawnedAt + 10_000,
    sessionSlug: primary,
    coSessions: [],
    since: delta.timestamp || new Date(spawnedAt).toISOString(),
    // CC in Fathom-mode writes with source=claude-code:fathom; this keeps the
    // own-writes filter in pollLiveSessions honest so CC doesn't echo itself.
    ownSource: "claude-code:fathom",
  });
}

async function injectIntoEngagement(sessionSlug, delta, host) {
  const engagement = engagements.get(sessionSlug);
  if (!engagement) return;
  // Respect grace window — socket may not exist yet on first poll after spawn.
  const inGrace = Date.now() < (engagement.graceUntil || 0);
  if (!inGrace && !kittySocketAlive(engagement.socket)) {
    console.log(`  💬 engagement ${sessionSlug} socket closed — dropping`);
    engagements.delete(sessionSlug);
    return;
  }
  if (inGrace) {
    // Queue-less: drop the live-injection attempt during grace. The initial
    // orient prompt will pick up the invitation delta when claude-code
    // searches the lake. Future deltas are caught by the next live poll.
    return;
  }

  // Skip our own writes — claude-code's outputs tagged with this session
  // shouldn't be injected back into itself.
  if (delta.source === engagement.ownSource) return;

  const participant =
    tagValue(delta, "participant:") ||
    (delta.source || "").replace(/^.*:/, "") ||
    "someone";

  const text = framedMessage({
    sessionSlug,
    participant,
    content: (delta.content || "").trim(),
  });

  console.log(`  💬 inject → ${sessionSlug}: ${(delta.content || "").slice(0, 60)}…`);
  await kittySendText(engagement.socket, text);
}

async function pollLiveSessions(pusher, host) {
  // For each open engagement, pull deltas in its session newer than the
  // engagement's `since` marker. Deliver any that aren't from us.
  const now = Date.now();
  for (const [slug, eng] of engagements) {
    const inGrace = now < (eng.graceUntil || 0);
    if (!inGrace && !kittySocketAlive(eng.socket)) {
      console.log(`  💬 engagement ${slug} ended (kitty window closed)`);
      engagements.delete(slug);
      continue;
    }
    // Skip delivery during grace — kitty isn't ready to receive input yet.
    if (inGrace) continue;
    let deltas;
    try {
      deltas = await pusher.query({
        tags_include: `chat:${slug}`,
        time_start: eng.since,
        limit: 100,
      });
    } catch (e) {
      // Don't spam — transient network blips are fine
      continue;
    }
    deltas.sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));
    let signoffSeen = false;
    for (const d of deltas) {
      if (d.timestamp <= eng.since) continue;
      eng.since = d.timestamp;
      const tags = d.tags || [];
      // Signoff from this engagement's CC — kitty doesn't self-close when
      // claude exits, so we detect the signoff delta and close the window.
      // Agent-mode signoff: participant:agent:<host>.
      // Fathom-mode signoff: participant:fathom + source=claude-code:fathom.
      const hasSignoff = tags.includes("signoff");
      const isAgentSignoff = hasSignoff && tags.includes(`participant:agent:${host}`);
      const isFathomSignoff =
        hasSignoff &&
        tags.includes("participant:fathom") &&
        (d.source || "") === "claude-code:fathom";
      if (isAgentSignoff || isFathomSignoff) {
        signoffSeen = true;
        continue;
      }
      // Don't re-inject the initial invitation (it's in the orient prompt).
      // Own writes also shouldn't loop back.
      if ((d.source || "") === eng.ownSource) continue;
      await injectIntoEngagement(slug, d, host);
    }
    if (signoffSeen) {
      console.log(`  💬 engagement ${slug} signed off — closing window`);
      try { closeWindow(eng.socket); } catch {}
      engagements.delete(slug);
    }
  }
}

export default {
  name: "Chat-router",
  category: "runtime",
  icon: "💬",
  description: "Bridge lake chat sessions to local claude-code subprocesses. Spawns and feeds claude-code when deltas address this host in a chat session.",
  defaults: {
    // On by default — this is the plumbing that makes Fathom's chat
    // delegations reach a local machine. Disabling the plugin means the
    // agent ignores to:agent:<host> deltas entirely (Fathom can still
    // write them; they just sit in the lake without a pickup).
    enabled: true,
    poll_interval_ms: 2000,
    workspace_root: join(homedir(), "Dropbox", "Work"),
    default_workspace: "",
    permission_mode: "auto",
    kitty_background: "#1a2e3a",
  },

  start(config, pusher) {
    const host = config.host || hostname();
    const state = loadState();
    console.log(`  chat-router: polling lake for to:agent:${host} deltas (since ${state.last_seen})`);

    const tick = async () => {
      await pollInvitations(pusher, config, state, host);
      await pollFathomModeInvitations(pusher, config, state);
      await pollLiveSessions(pusher, host);
    };

    const timer = setInterval(() => {
      tick().catch((e) => console.error(`  chat-router: tick error: ${e.message}`));
    }, config.poll_interval_ms || 2000);
    tick().catch((e) => console.error(`  chat-router: initial tick error: ${e.message}`));

    return {
      stop() {
        clearInterval(timer);
        saveState(state);
      },
    };
  },
};
