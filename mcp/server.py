"""Fathom MCP server — generic adapter that reads tools from the API.

Connects to any Fathom instance (self-hosted or cloud). Discovers
available tools from GET /v1/tools, filtered by the token's scopes.

Reads FATHOM_API_URL and FATHOM_API_KEY from environment.
Run: python server.py
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

API_URL = os.environ.get("FATHOM_API_URL", "http://localhost:8201")
API_KEY = os.environ.get("FATHOM_API_KEY", "")

server = Server("Fathom")

# Tool definitions loaded from the API at startup
_tools: dict[str, dict] = {}


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def _client() -> httpx.Client:
    return httpx.Client(base_url=API_URL, headers=_headers(), timeout=30)


def _format_results(data, key: str = "results") -> str:
    """Format API response into readable text for the LLM."""
    items = data if isinstance(data, list) else data.get(key, data.get("deltas", []))
    if not items:
        return "No results."

    lines = [f"{len(items)} results:\n"]
    for raw in items:
        d = raw.get("delta", raw) if isinstance(raw, dict) and "delta" in raw else raw
        ts = (d.get("timestamp") or "")[:16]
        tags = ", ".join((d.get("tags") or [])[:4])
        src = d.get("source", "")
        content = (d.get("content") or "")[:400]
        media = f" [image: {d['media_hash']}]" if d.get("media_hash") else ""
        lines.append(f"[{ts} · {src} · {tags}]{media}\n{content}\n")
    return "\n".join(lines)


def _execute_tool(tool_def: dict, args: dict) -> str:
    """Execute a tool by calling its endpoint on the consumer API."""
    endpoint = tool_def["endpoint"]
    method = endpoint["method"]
    path = endpoint["path"]

    # Map tool argument names to API parameter names
    request_map = tool_def.get("request_map", {})
    mapped = {}
    for arg_name, value in args.items():
        if value is None:
            continue
        api_name = request_map.get(arg_name, arg_name)
        mapped[api_name] = value

    with _client() as c:
        if method == "POST":
            r = c.post(path, json=mapped)
        elif method == "GET":
            params = {}
            for k, v in mapped.items():
                if isinstance(v, list):
                    params[k] = ",".join(str(i) for i in v)
                elif v is not None:
                    params[k] = v
            r = c.get(path, params=params)
        else:
            return f"Unsupported method: {method}"

        r.raise_for_status()
        data = r.json()

    # Format based on endpoint type
    if path == "/v1/search":
        return _format_results(data)
    elif path == "/v1/deltas" and method == "POST":
        return f"Written. ID: {data.get('id', '?')}"
    elif path == "/v1/deltas" and method == "GET":
        return _format_results(data)
    elif path == "/v1/stats":
        return (
            f"Lake: {data.get('total', '?')} deltas, "
            f"{data.get('embedded', '?')} embedded "
            f"({data.get('percent', '?')}% coverage)"
        )
    elif path == "/v1/chat/completions":
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return json.dumps(data, indent=2)[:2000]
    else:
        return json.dumps(data, indent=2)[:2000]


def _load_tools():
    """Fetch tool definitions from the API."""
    global _tools
    try:
        with _client() as c:
            r = c.get("/v1/tools")
            r.raise_for_status()
            data = r.json()
        for t in data.get("tools", []):
            _tools[t["name"]] = t
    except Exception as e:
        print(f"Warning: could not load tools from {API_URL}: {e}", flush=True)


# ── MCP handlers ──────────────────────────────────


@server.list_tools()
async def list_tools() -> list[Tool]:
    if not _tools:
        _load_tools()
    return [
        Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t.get("parameters", {"type": "object", "properties": {}}),
        )
        for t in _tools.values()
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if not _tools:
        _load_tools()
    tool_def = _tools.get(name)
    if not tool_def:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    try:
        result = _execute_tool(tool_def, arguments)
    except Exception as e:
        result = f"Error: {e}"
    return [TextContent(type="text", text=result)]


# ── Main ──────────────────────────────────────────


async def main():
    _load_tools()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
