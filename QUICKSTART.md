# Quickstart

Self-host Fathom on a single Linux machine. About five minutes from clone to running.

## Prerequisites

- Docker or Podman with the compose plugin
- Git
- An API key from one of: Google AI Studio (Gemini), OpenAI, or a local Ollama install

## Install

```bash
git clone https://github.com/myrakrusemark/consumer-fathom.git
cd consumer-fathom
cp .env.example .env
```

Open `.env` and set `LLM_API_KEY` (the key for your LLM provider — Gemini, OpenAI, etc). If you want a provider other than Gemini, change `LLM_PROVIDER` too (`openai` or `ollama`).

## Run

```bash
docker compose up -d
```

First boot builds three images and pulls postgres. Give it a minute or two. When it's done, the stack is listening on:

| URL | What |
|---|---|
| http://localhost:8201 | API and UI. This is where you go. |
| http://localhost:4246 | Delta-store (the lake's HTTP API) |
| http://localhost:4260 | Source-runner (external source poller) |

Everything is bound to 127.0.0.1 by default.

## Verify

```bash
curl http://localhost:4246/health   # {"status":"ok"}
curl http://localhost:8201/v1/stats # delta counts, should start at zero
```

Then open `http://localhost:8201` in a browser and say hello.

## From here, the dashboard drives

Everything else happens inside the dashboard. Pair a local agent, connect an MCP host (Claude Code, Claude Desktop, Cursor), wire up hooks, add sources to poll, mint API tokens. The dashboard walks you through each step and hands you the commands to run when something has to happen on your host.

If you prefer the terminal, the Node tools in `agent/`, `cli/`, `mcp-node/`, and `connect/` are the same flows unwrapped. They all talk to the API at `http://localhost:8201`.

## Pair your first agent

In the dashboard, scroll to the **Agent** section. If nothing is connected yet, you'll see a short intro followed by three tiles: Linux, Mac, Windows. Pick the one that matches the machine you want to pair. It can be the same box you're running the server on, or a different one on your network.

A modal opens asking you to name the machine. Pick something short and memorable (laptop, home-server, studio). Letters, numbers, dots, dashes, and underscores are all fine. That name is how this host will appear everywhere in the dashboard from here on.

The modal then shows a pre-filled install command with a single-use pair code, good for ten minutes:

```
npx fathom-agent init --pair-code pair_<short-lived-code>
```

Copy it, open a terminal on the machine you're pairing, paste, and run. Node 20 or newer is required (that's what `npx` needs). The agent installs, redeems the pair code for a long-lived API key, and writes it to `~/.fathom/agent.json`. Then it starts heartbeating. The dashboard is watching for that heartbeat, and it closes the modal the moment it arrives. No refresh needed.

From there, `fathom-agent run` keeps the agent alive in the foreground. For something that survives reboots, use `fathom-agent install` instead. That drops a systemd user unit on Linux, a launchd plist on Mac, or a helper script on Windows.

Need to pair another machine later? Re-open the same tile and mint a new code. If you re-pair a host that's already connected, the old key rotates out automatically, so you don't end up with stale credentials lying around.

### What a paired agent unlocks

- **Routines.** Scheduled prompts that fire into a local Claude Code session on that machine. Write a prompt, pick a cron schedule, and the agent runs it. Requires [kitty](https://sw.kovidgoyal.net/kitty/) (the terminal) and the `claude` CLI on PATH — the agent spawns a kitty window per fire and injects the prompt via kitty's remote-control protocol. No `kitty.conf` setup needed; the agent passes the remote-control flags inline per spawn.
- **Local sources.** Plugins for things only a local process can see: a notes vault, Home Assistant, system health, kitty config, whatever else you wire up.
- **Presence.** The dashboard now knows when the machine is online, and that signal feeds the lake alongside everything else.

## Updating

```bash
git pull
docker compose build
docker compose up -d
```

## Teardown

```bash
docker compose down            # stop, keep data
docker compose down -v         # stop and drop the lake (deletes pgdata volume)
rm -rf data/                   # drop delta-store media and source-runner state
```

## Troubleshooting

**`connection refused` on port 8201.** Give the API another 10 seconds. It waits for postgres and delta-store to come up. `docker compose logs api` will tell you what it's waiting on.

**`401 Unauthorized` from the UI.** You set `DELTA_API_KEY` but the api container and delta-store container have different values. They need to match, or both need to be blank.

**Gemini quota errors.** The free tier is fine for trying it out but rate-limits aggressively. If you hit the ceiling, grab an OpenAI key and set `LLM_PROVIDER=openai`.

**Podman on SELinux systems.** If bind mounts fail with permission errors, add `:z` to each volume mount in `docker-compose.yml`, or run `chcon -Rt container_file_t data/`.
