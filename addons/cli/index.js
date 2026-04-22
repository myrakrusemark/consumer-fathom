#!/usr/bin/env node
/**
 * fathom — CLI for your Fathom memories.
 *
 * Same tools as MCP, from the terminal.
 *
 * Env:
 *   FATHOM_API_URL  — consumer API (default: http://localhost:8201)
 *   FATHOM_API_KEY  — bearer token from Settings → API Keys
 *
 * Usage:
 *   fathom remember "what happened today"           # deep (plan + DAG)
 *   fathom remember "what happened today" --shallow # single similarity search
 *   fathom write "decided to ship v2 Friday" --tags decision,v2
 *   fathom recall --tags homeassistant --since 24h
 *   fathom mind                                     # stats overview
 *   fathom mind tags                                # tag catalogue
 */

const API_URL = (process.env.FATHOM_API_URL || "http://localhost:8201").replace(/\/$/, "");
const API_KEY = process.env.FATHOM_API_KEY || "";

function headers(json = true) {
  const h = {};
  if (json) h["Content-Type"] = "application/json";
  if (API_KEY) h["Authorization"] = `Bearer ${API_KEY}`;
  return h;
}

async function api(method, path, body) {
  const opts = { method, headers: headers(method !== "GET") };
  let url = `${API_URL}${path}`;
  if (method === "GET" && body) {
    url += "?" + new URLSearchParams(body);
  } else if (body) {
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    console.error(`Error: ${r.status} ${r.statusText}`);
    if (text) console.error(text);
    process.exit(1);
  }
  return r.json();
}

async function apiRaw(method, path) {
  const opts = { method, headers: headers(false) };
  const r = await fetch(`${API_URL}${path}`, opts);
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    console.error(`Error: ${r.status} ${r.statusText}`);
    if (text) console.error(text);
    process.exit(1);
  }
  return r;
}

// ── Formatters ───────────────────────────────────

function fmtMomentList(data) {
  const items = data.results || data.deltas || (Array.isArray(data) ? data : []);
  if (!items.length) { console.log("No moments surfaced."); return; }

  console.log(`${items.length} moments:\n`);
  for (const raw of items) {
    const d = raw.delta || raw;
    const ts = (d.timestamp || "").slice(0, 16);
    const tags = (d.tags || []).slice(0, 5).join(", ");
    const src = d.source || "";
    const content = (d.content || "").slice(0, 500);
    const media = d.media_hash ? ` [image: ${d.media_hash}]` : "";
    const dist = raw.distance != null ? ` d=${raw.distance.toFixed(3)}` : "";
    console.log(`  \x1b[2m${ts} · ${src}${dist}\x1b[0m`);
    console.log(`  \x1b[33m${tags}\x1b[0m${media}`);
    console.log(`  ${content}\n`);
  }
}

function fmtRecall(data) {
  const total = data.total_count || 0;
  const tree = data.tree || [];
  if (!total || !tree.length) { console.log("No moments surfaced."); return; }
  console.log(`\x1b[2m${total} moments across ${tree.length} step(s)\x1b[0m\n`);
  console.log(data.as_prompt || "");
}

// ── Commands ─────────────────────────────────────

async function cmdRemember(args) {
  const query = args.filter(a => !a.startsWith("--")).join(" ");
  if (!query) {
    console.error("Usage: fathom remember <query> [--limit N] [--shallow]");
    process.exit(1);
  }
  const limit = parseInt(flagVal(args, "--limit") || "20", 10);
  const depth = args.includes("--shallow") ? "shallow" : "deep";
  const data = await api("POST", "/v1/search", { text: query, depth, limit });
  fmtRecall(data);
}

async function cmdWrite(args) {
  // Content is everything that's not a flag
  const flags = new Set(["--tags", "--source"]);
  const parts = [];
  let i = 0;
  while (i < args.length) {
    if (flags.has(args[i])) { i += 2; continue; }
    if (args[i] === "-") {
      // Read from stdin
      const chunks = [];
      for await (const chunk of process.stdin) chunks.push(chunk);
      parts.push(Buffer.concat(chunks).toString().trim());
      i++;
    } else {
      parts.push(args[i]);
      i++;
    }
  }
  const content = parts.join(" ");
  if (!content) { console.error("Usage: fathom write <content> [--tags a,b] [--source x]"); process.exit(1); }

  const tags = (flagVal(args, "--tags") || "").split(",").filter(Boolean);
  const source = flagVal(args, "--source") || "cli";

  const data = await api("POST", "/v1/deltas", { content, tags, source });
  console.log(`Written. ID: ${data.id || "?"}`);
}

async function cmdRecall(args) {
  const params = {};
  const tags = flagVal(args, "--tags");
  const source = flagVal(args, "--source");
  const since = flagVal(args, "--since");
  const limit = flagVal(args, "--limit") || "30";

  params.limit = limit;
  if (tags) params.tags_include = tags;
  if (source) params.source = source;
  if (since) {
    // Parse relative time: 24h, 7d, 30m
    const match = since.match(/^(\d+)([mhd])$/);
    if (match) {
      const [, n, unit] = match;
      const ms = { m: 60000, h: 3600000, d: 86400000 }[unit];
      params.time_start = new Date(Date.now() - parseInt(n) * ms).toISOString();
    } else {
      params.time_start = since; // Assume ISO
    }
  }

  const data = await api("GET", "/v1/deltas", params);
  fmtMomentList(data);
}

async function cmdDeepRecall(args) {
  // Accept JSON plan as single arg, or `-` for stdin
  let planJson = args.find(a => !a.startsWith("--"));
  if (planJson === "-") {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    planJson = Buffer.concat(chunks).toString().trim();
  }
  if (!planJson) {
    console.error('Usage: fathom deep_recall \'<plan-json>\'  (or pipe plan via stdin: echo \'{"steps":[...]}\' | fathom deep_recall -)');
    process.exit(1);
  }
  let plan;
  try {
    plan = JSON.parse(planJson);
  } catch (e) {
    console.error(`Invalid JSON plan: ${e.message}`);
    process.exit(1);
  }
  const body = Array.isArray(plan) ? { steps: plan } : plan;
  const data = await api("POST", "/v1/plan", body);
  fmtRecall(data);
}

async function cmdSeeImage(args) {
  const hash = args.find(a => !a.startsWith("--"));
  if (!hash) {
    console.error("Usage: fathom see_image <media_hash>");
    process.exit(1);
  }
  const r = await apiRaw("GET", `/v1/media/${hash}`);
  const buf = Buffer.from(await r.arrayBuffer());
  const ctype = r.headers.get("content-type") || "image/webp";
  const ext = ctype.includes("png") ? "png" : ctype.includes("jpeg") ? "jpg" : "webp";
  const fs = await import("node:fs/promises");
  const os = await import("node:os");
  const path = await import("node:path");
  const outPath = path.join(os.tmpdir(), `fathom-${hash.slice(0, 12)}.${ext}`);
  await fs.writeFile(outPath, buf);
  console.log(outPath);
}

async function cmdMind(args) {
  // Subcommand: `fathom mind tags`
  if (args[0] === "tags") {
    const tags = await api("GET", "/v1/tags");
    if (typeof tags === "object" && !Array.isArray(tags)) {
      const sorted = Object.entries(tags).sort((a, b) => b[1] - a[1]);
      for (const [tag, count] of sorted) {
        console.log(`  \x1b[33m${tag}\x1b[0m (${count})`);
      }
    } else {
      console.log(JSON.stringify(tags, null, 2));
    }
    return;
  }

  // Default: stats overview
  const [stats, tags] = await Promise.all([
    api("GET", "/v1/stats"),
    api("GET", "/v1/tags"),
  ]);

  const total = (stats.total || 0).toLocaleString();
  const embedded = (stats.embedded || 0).toLocaleString();
  const pct = stats.percent || 0;
  console.log(`Your mind: ${total} moments, ${embedded} embedded (${pct}% coverage)\n`);

  // Top tags
  if (typeof tags === "object" && !Array.isArray(tags)) {
    const sorted = Object.entries(tags).sort((a, b) => b[1] - a[1]).slice(0, 20);
    console.log("Top tags:");
    for (const [tag, count] of sorted) {
      console.log(`  \x1b[33m${tag}\x1b[0m (${count})`);
    }
  }
}

async function cmdProposeContact(args) {
  // Parse positional display_name + optional --flags.
  const flagged = new Set(["--slug", "--candidate-slug", "--rationale", "--context"]);
  const parts = [];
  let i = 0;
  while (i < args.length) {
    if (flagged.has(args[i])) { i += 2; continue; }
    parts.push(args[i]);
    i++;
  }
  const displayName = parts.join(" ").trim();
  const rationale = flagVal(args, "--rationale") || "";
  const slug = flagVal(args, "--slug") || flagVal(args, "--candidate-slug") || "";
  const contextRaw = flagVal(args, "--context") || "";
  if (!displayName || !rationale) {
    console.error("Usage: fathom propose_contact <display_name> --rationale \"<why>\" [--slug bob] [--context '{\"channel\":\"telegram\"}']");
    process.exit(1);
  }
  let context = {};
  if (contextRaw) {
    try { context = JSON.parse(contextRaw); }
    catch { console.error("--context must be valid JSON"); process.exit(1); }
  }
  const data = await api("POST", "/v1/contact-proposals", {
    display_name: displayName,
    rationale,
    candidate_slug: slug || null,
    source_context: context,
  });
  console.log(`Proposal written. id=${data.id || "?"}`);
  console.log(`  display_name: ${data.display_name}`);
  if (data.candidate_slug) console.log(`  candidate_slug: ${data.candidate_slug}`);
  console.log("  Admin can review in Settings → Contacts.");
}

// ── Flag parsing ─────────────────────────────────

function flagVal(args, flag) {
  const i = args.indexOf(flag);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : null;
}

// ── Main ─────────────────────────────────────────

const COMMANDS = {
  remember:    { fn: cmdRemember,   usage: 'fathom remember <query> [--limit N] [--shallow]' },
  write:       { fn: cmdWrite,      usage: 'fathom write <content> [--tags a,b] [--source x]' },
  recall:      { fn: cmdRecall,     usage: 'fathom recall [--tags a,b] [--source x] [--since 24h] [--limit N]' },
  deep_recall: { fn: cmdDeepRecall, usage: "fathom deep_recall '<plan-json>'  (or pipe via stdin with -)" },
  see_image:   { fn: cmdSeeImage,   usage: 'fathom see_image <media_hash>' },
  mind:        { fn: cmdMind,       usage: 'fathom mind [tags]' },
  propose_contact: { fn: cmdProposeContact, usage: 'fathom propose_contact <display_name> --rationale "<why>" [--slug bob] [--context \'{"channel":"telegram"}\']' },

  // Silent aliases — old verb names still work, undocumented in help
  search: { fn: cmdRemember, hidden: true },
  query:  { fn: cmdRecall,   hidden: true },
  stats:  { fn: cmdMind,     hidden: true },
};

const [cmd, ...args] = process.argv.slice(2);

if (!cmd || cmd === "help" || cmd === "--help") {
  console.log("fathom — CLI for your Fathom memories\n");
  console.log("Commands:");
  for (const [, { usage, hidden }] of Object.entries(COMMANDS)) {
    if (hidden || !usage) continue;
    console.log(`  ${usage}`);
  }
  console.log("\nPipe stdin:  echo 'notes' | fathom write - --tags meeting");
  console.log(`\nAPI: ${API_URL}`);
  console.log(`Key: ${API_KEY ? API_KEY.slice(0, 8) + "…" : "(not set)"}`);
  process.exit(0);
}

const command = COMMANDS[cmd];
if (!command) {
  console.error(`Unknown command: ${cmd}\nRun 'fathom help' for usage.`);
  process.exit(1);
}

command.fn(args).catch(e => {
  console.error(`Error: ${e.message}`);
  process.exit(1);
});
