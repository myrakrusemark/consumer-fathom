/**
 * Chat-router — bridge between lake chat sessions and local claude-code.
 *
 * Watches the lake for deltas tagged `to:agent:<this-host>` with a `chat:<slug>`
 * tag. For each one:
 *   - if no engagement exists for that session yet, fetch the Fathom chat
 *     orient from the consumer-api and spawn claude-code in a kitty window
 *     with that orient as the prompt.
 *   - if an engagement IS live, inject the message as a framed user message
 *     into the running claude-code session.
 *
 * CC acts AS Fathom (not as a delegated subordinate): writes land with
 * `participant:fathom` + source `claude-code:fathom`. Identity stays Fathom;
 * the substrate is CC on this host.
 *
 * Also watches deltas in *currently engaged* sessions from other participants
 * (user, Fathom-via-loop, other agents) so claude-code hears the full
 * conversation while it's working, not just the message addressed to it.
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

// Fetch the chat orient from the consumer-api. Centralizing it on the server
// keeps SYSTEM_PREAMBLE as the single source of truth for Fathom's voice —
// chat-router just pipes what the server assembles (preamble + session block
// + mood + tag-contract coda; crystal skipped since CC already has it via
// the CLAUDE.md cascade).
async function fetchChatOrient(pusher, sessionSlug) {
  const url = `${pusher.apiUrl}/v1/cc-orient?session=${encodeURIComponent(sessionSlug)}`;
  const headers = {};
  if (pusher.apiKey) headers["Authorization"] = `Bearer ${pusher.apiKey}`;
  const r = await fetch(url, { headers, signal: AbortSignal.timeout(10000) });
  if (!r.ok) throw new Error(`/v1/cc-orient ${r.status}`);
  return await r.text();
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

function framedMessage({ sessionSlug, participant, content }) {
  // Keep it short — CC sees this as if the user typed it into claude.
  // Role label helps it distinguish user from Fathom-via-loop from another
  // participant.
  const who = participant || "someone";
  return `\nMessage from ${who} in chat:${sessionSlug}: ${content}\n`;
}

async function pollInvitations(pusher, config, state, host) {
  // Fetch every delta that addresses this host. A single `to:agent:<host>`
  // query is enough — lake's tags_include is AND-semantic, so adding
  // chat:* would narrow needlessly when we can filter in-process.
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
    await handleInvite(d, pusher, config, host);
    state.last_seen = d.timestamp;
  }
  if (invites.length) saveState(state);
}

async function handleInvite(delta, pusher, config, host) {
  const sessionSlugs = allTagValues(delta, "chat:");
  if (!sessionSlugs.length) {
    // Non-chat routing (no chat: tag). Ignore for now — future expansion.
    console.log(`  chat-router: skipping to:agent:${host} delta ${delta.id.slice(0, 8)} (no chat: tag)`);
    return;
  }

  const [primary] = sessionSlugs;

  if (engagements.has(primary)) {
    // Engagement already live — inject the new message as a framed user
    // message into the running claude-code session.
    await injectIntoEngagement(primary, delta, host);
    return;
  }

  const workspaceName = tagValue(delta, "workspace:") || config.default_workspace || "";
  const cwd = workspacePath(config.workspace_root, workspaceName);

  let prompt;
  try {
    prompt = await fetchChatOrient(pusher, primary);
  } catch (e) {
    console.error(`  chat-router: orient fetch failed for ${primary}: ${e.message}`);
    return;
  }

  console.log(`  💬 engage ${primary} (ws: ${workspaceName || "default"})`);

  const { socket, title, spawnedAt } = spawnClaudeInKitty({
    workspaceCwd: cwd,
    prompt,
    permissionMode: config.permission_mode || "auto",
    sessionLabel: `chat-${primary}`,
    kittyBackground: config.kitty_background || "#1a2e3a",
    autoSubmit: true,
    // Larger delay than routine spawns — the fetched orient is ~8KB and kitty
    // + Ink TUI need time to absorb it before we press enter.
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
    since: delta.timestamp || new Date(spawnedAt).toISOString(),
    // CC writes with source `claude-code:fathom` (Fathom voice, full body);
    // this keeps the own-writes filter in pollLiveSessions honest so CC
    // doesn't echo itself.
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
      // CC writes as Fathom: participant:fathom + signoff + source matches ownSource.
      if (
        tags.includes("signoff") &&
        tags.includes("participant:fathom") &&
        (d.source || "") === eng.ownSource
      ) {
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
  description: "Bridge lake chat sessions to local claude-code subprocesses. Spawns and feeds claude-code when deltas address this host in a chat session; CC writes as Fathom.",
  defaults: {
    // On by default — this is the plumbing that lets Fathom's chat answer
    // with the body's full capabilities (Bash, web, file edits) when the
    // user force-routes a message. Disabling the plugin means the agent
    // ignores to:agent:<host> deltas entirely (the delta still lands; the
    // consumer-api's 15s fallback then runs the turn through loop-api).
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
