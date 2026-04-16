#!/usr/bin/env node
/**
 * Fathom MCP server — generic adapter that reads tools from the API.
 *
 * Connects to any Fathom instance (self-hosted or cloud). Discovers
 * available tools from GET /v1/tools, filtered by the token's scopes.
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
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

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

function formatResults(data) {
  const items = data.results || data.deltas || (Array.isArray(data) ? data : []);
  if (!items.length) return "No results.";

  const lines = [`${items.length} results:\n`];
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

function formatResponse(path, method, data) {
  if (path === "/v1/search") return formatResults(data);
  if (path === "/v1/deltas" && method === "POST") return `Written. ID: ${data.id || "?"}`;
  if (path === "/v1/deltas" && method === "GET") return formatResults(data);
  if (path === "/v1/stats") {
    return `Lake: ${data.total ?? "?"} deltas, ${data.embedded ?? "?"} embedded (${data.percent ?? "?"}% coverage)`;
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

  // Build lookup
  const toolMap = {};
  for (const t of tools) toolMap[t.name] = t;

  const server = new Server(
    { name: "Fathom", version: "0.1.0" },
    { capabilities: { tools: {} } },
  );

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

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(e => {
  console.error(e);
  process.exit(1);
});
