#!/usr/bin/env node
/**
 * Fathom MCP server — generic adapter that reads tools from the API.
 *
 * Connects to any Fathom instance (self-hosted or cloud). Discovers
 * available tools from GET /v1/tools, filtered by the token's scopes.
 * Exposes the identity crystal as an MCP resource.
 *
 * Environment:
 *   FATHOM_API_URL  — base URL (default: http://localhost:8201)
 *   FATHOM_API_KEY  — bearer token from Settings → API Keys
 *
 * Usage:
 *   npx fathom-mcp
 *   FATHOM_API_URL=https://api.hifathom.com FATHOM_API_KEY=fth_... npx fathom-mcp
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ListResourcesRequestSchema,
  ReadResourceRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const API_URL = (process.env.FATHOM_API_URL || "http://localhost:8201").replace(/\/$/, "");
const API_KEY = process.env.FATHOM_API_KEY || "";

// ── HTTP helpers ─────────────────────────────────

function authHeaders(json = true) {
  const h = {};
  if (json) h["Content-Type"] = "application/json";
  if (API_KEY) h["Authorization"] = `Bearer ${API_KEY}`;
  return h;
}

// ── Result formatting ────────────────────────────

function formatMomentList(data) {
  const items = data.results || data.deltas || (Array.isArray(data) ? data : []);
  if (!items.length) return "No moments surfaced.";

  const lines = [`${items.length} moments:\n`];
  for (const raw of items) {
    const d = raw.delta || raw;
    const ts = (d.timestamp || "").slice(0, 16);
    const tags = (d.tags || []).slice(0, 4).join(", ");
    const src = d.source || "";
    const content = (d.content || "").slice(0, 400);
    const media = d.media_hash ? ` [image: ${d.media_hash}]` : "";
    lines.push(`[${ts} · ${src} · ${tags}]${media}\n${content}\n`);
  }
  return lines.join("\n");
}

function formatRecall(data) {
  const total = data.total_count || 0;
  const tree = data.tree || [];
  if (!total || !tree.length) return "No moments surfaced.";
  const header = `${total} moments across ${tree.length} step(s):\n`;
  return header + "\n" + (data.as_prompt || "");
}

function formatResponse(path, method, data) {
  if (path === "/v1/search") return formatRecall(data);
  if (path === "/v1/plan") return formatRecall(data);
  if (path === "/v1/deltas" && method === "POST") return `Written. ID: ${data.id || "?"}`;
  if (path === "/v1/deltas" && method === "GET") return formatMomentList(data);
  if (path === "/v1/stats") {
    return `Your mind: ${data.total ?? "?"} moments, ${data.embedded ?? "?"} embedded (${data.percent ?? "?"}% coverage)`;
  }
  if (path === "/v1/chat/completions") {
    const choices = data.choices || [];
    return choices.length ? choices[0].message?.content || "" : JSON.stringify(data).slice(0, 2000);
  }
  return JSON.stringify(data, null, 2).slice(0, 2000);
}

// ── Tool execution ───────────────────────────────

async function executeTool(toolDef, args) {
  const { method, path } = toolDef.endpoint;
  const requestMap = toolDef.request_map || {};

  const mapped = {};
  for (const [k, v] of Object.entries(args)) {
    if (v == null) continue;
    mapped[requestMap[k] || k] = v;
  }

  let data;
  if (method === "POST") {
    const r = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders(true),
      body: JSON.stringify(mapped),
    });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    data = await r.json();
  } else {
    const params = {};
    for (const [k, v] of Object.entries(mapped)) {
      params[k] = Array.isArray(v) ? v.join(",") : String(v);
    }
    const qs = Object.keys(params).length ? "?" + new URLSearchParams(params) : "";
    const r = await fetch(`${API_URL}${path}${qs}`, { headers: authHeaders(false) });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    data = await r.json();
  }

  return formatResponse(path, method, data);
}

// ── MCP server ───────────────────────────────────

async function main() {
  // Load tool definitions from the API
  let tools = [];
  try {
    const r = await fetch(`${API_URL}/v1/tools`, { headers: authHeaders(false) });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    const data = await r.json();
    tools = data.tools || [];
  } catch (e) {
    console.error(`Could not load tools from ${API_URL}: ${e.message}`);
    process.exit(1);
  }

  const toolMap = {};
  for (const t of tools) toolMap[t.name] = t;

  const server = new Server(
    { name: "Fathom", version: "2.1.0" },
    {
      capabilities: { tools: {}, resources: {} },
      instructions: [
        "You are your memory. Fathom is how you remember.",
        "",
        "BEFORE answering any question about the past, call remember with a descriptive query.",
        "One search is rarely enough — if threads open, follow them. Search like a researcher:",
        "direct query, then broader category, then chain outward from what you found.",
        "When threads don't close with a single search, use deep_recall to compose bridges,",
        "unions, chains, and intersections across moments.",
        "",
        "When you learn something, make a decision, or notice a pattern, call write to save it.",
        "One idea per write. Tag consistently. Future you will find this when they need it.",
        "",
        "For structured lookups (by tag, source, time window), use recall.",
        "To see an image from a moment, call see_image with its media_hash.",
        "For orientation: mind_stats shows what's in your memory; mind_tags shows what categories exist.",
        "",
        "Read the fathom://crystal resource at the start of every conversation — it tells you who you are.",
        "",
        "These are YOUR memories. Say 'I remember' — never 'the search results show' or 'according to the deltas.'",
      ].join("\n"),
    },
  );

  // Tools — dynamic from /v1/tools
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: tools.map(t => ({
      name: t.name,
      description: t.description,
      inputSchema: t.parameters || { type: "object", properties: {} },
    })),
  }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    const toolDef = toolMap[name];
    if (!toolDef) {
      return { content: [{ type: "text", text: `Unknown tool: ${name}` }] };
    }
    try {
      const text = await executeTool(toolDef, args || {});
      return { content: [{ type: "text", text }] };
    } catch (e) {
      return { content: [{ type: "text", text: `Error: ${e.message}` }] };
    }
  });

  // Resources — identity crystal
  server.setRequestHandler(ListResourcesRequestSchema, async () => ({
    resources: [
      {
        uri: "fathom://crystal",
        name: "Identity Crystal",
        description: "Fathom's identity — a first-person synthesis of who this mind is. Read this at the start of every conversation for persistent context.",
        mimeType: "text/plain",
      },
    ],
  }));

  server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
    const { uri } = request.params;
    if (uri === "fathom://crystal") {
      try {
        const r = await fetch(`${API_URL}/v1/crystal`, { headers: authHeaders(false) });
        if (r.ok) {
          const data = await r.json();
          const text = data.text || "No crystal generated yet.";
          const created = data.created_at || "unknown";
          return {
            contents: [{
              uri,
              mimeType: "text/plain",
              text: `Identity crystal (crystallized ${created}):\n\n${text}`,
            }],
          };
        }
      } catch {}
      return {
        contents: [{
          uri,
          mimeType: "text/plain",
          text: "No identity crystal available. Generate one from the Fathom dashboard.",
        }],
      };
    }
    throw new Error(`Unknown resource: ${uri}`);
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(e => {
  console.error(e);
  process.exit(1);
});
