/**
 * Home Assistant — machine-local entity polling.
 *
 * Watches Home Assistant entity states by ID and writes one delta per state
 * change. Machine-local because most HA installs live on the user's LAN —
 * the consumer API runs elsewhere and can't see `http://ha.local:8123`.
 * Moving HA to the agent also means the home-automation signal and the
 * machine hosting it are the same trust boundary, which is how the user
 * already thinks about it.
 *
 * Config shape (inside ~/.fathom/agent.json plugins.homeassistant):
 *   {
 *     "enabled": true,
 *     "instances": [
 *       {
 *         "id": "print-farm",
 *         "name": "Print Farm",
 *         "url": "http://ha.example.com:8123",
 *         "token": "eyJ...",               // long-lived access token
 *         "entities": ["sensor.foo_state", ...],
 *         "interval_ms": 300000             // 5 min default
 *       }
 *     ]
 *   }
 *
 * Multiple instances are supported — a household with two HA servers is
 * unusual but not unheard-of, and forcing one-per-machine would constrain
 * the model for no benefit.
 *
 * Dedup: each entity's state is only written when it differs from the last
 * value we saw for that entity, per-instance. State is persisted in
 * ~/.fathom/homeassistant-state.json so restarts don't re-emit a wave of
 * no-op deltas.
 */

import { createHash } from "crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "fs";
import { homedir, hostname } from "os";
import { dirname, join } from "path";

const STATE_PATH = join(homedir(), ".fathom", "homeassistant-state.json");
const DEFAULT_INTERVAL_MS = 5 * 60 * 1000;

// Capability descriptor for future dashboard registration (phase 4 will wire
// this into heartbeats so the UI can list "what this machine can do").
export const SOURCE_CAPABILITIES = {
  kind: "homeassistant",
  display_name: "Home Assistant",
  description: "Poll entity states from a Home Assistant instance on your LAN.",
  instance_shape: {
    id: { type: "string", required: true, help: "stable identifier, e.g. 'print-farm'" },
    name: { type: "string", required: true, help: "human label" },
    url: { type: "url", required: true, help: "Home Assistant base URL" },
    token: { type: "secret", required: true, help: "long-lived access token" },
    entities: { type: "string[]", required: true, help: "entity_id list to poll" },
    interval_ms: { type: "number", required: false, help: "poll interval (default 5min)" },
  },
};

function loadState() {
  try {
    return JSON.parse(readFileSync(STATE_PATH, "utf8"));
  } catch {
    return { instances: {} };
  }
}

function saveState(state) {
  mkdirSync(dirname(STATE_PATH), { recursive: true });
  writeFileSync(STATE_PATH, JSON.stringify(state, null, 2));
}

async function pollInstance(instance, state, pusher, host) {
  const url = (instance.url || "").replace(/\/$/, "");
  const token = instance.token || "";
  const entities = instance.entities || [];
  if (!url || !token || !entities.length) return;

  let allStates;
  try {
    const r = await fetch(`${url}/api/states`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(15000),
    });
    if (!r.ok) throw new Error(`${r.status}`);
    allStates = await r.json();
  } catch (e) {
    console.error(`  homeassistant[${instance.id}]: poll failed: ${e.message}`);
    return;
  }

  const byId = new Map(allStates.map((s) => [s.entity_id, s]));
  const instanceState = state.instances[instance.id] || {};

  for (const eid of entities) {
    const s = byId.get(eid);
    if (!s) continue;

    const stateVal = s.state;
    // Hash the state value so floating-point spam or whitespace doesn't
    // masquerade as change. The prior-state key is per-entity-per-instance.
    const hash = createHash("md5").update(`${eid}:${stateVal}`).digest("hex").slice(0, 12);
    if (instanceState[eid] === hash) continue;
    instanceState[eid] = hash;

    const attrs = s.attributes || {};
    const friendly = attrs.friendly_name || eid;
    const unit = attrs.unit_of_measurement || "";
    const content = unit ? `${friendly}: ${stateVal} ${unit}` : `${friendly}: ${stateVal}`;
    const domain = eid.includes(".") ? eid.split(".")[0] : "entity";

    pusher?.push?.({
      content,
      tags: [
        "homeassistant",
        domain,
        eid,
        `instance:${instance.id}`,
        `host:${host}`,
      ],
      source: "homeassistant",
    });
  }

  state.instances[instance.id] = instanceState;
}

export default {
  name: "HomeAssistant",
  icon: "⌂",
  description: "Poll Home Assistant entities and push state changes to the lake.",
  defaults: {
    instances: [],
  },

  start(config, pusher) {
    const instances = Array.isArray(config.instances) ? config.instances : [];
    if (!instances.length) {
      console.log("  homeassistant: no instances configured — idle");
      return { stop() {} };
    }

    const state = loadState();
    state.instances = state.instances || {};
    const host = config.host || hostname();

    console.log(`  homeassistant: ${instances.length} instance${instances.length === 1 ? "" : "s"} configured`);
    for (const inst of instances) {
      const entityCount = Array.isArray(inst.entities) ? inst.entities.length : 0;
      console.log(`    · ${inst.id} → ${inst.url} (${entityCount} entities, every ${Math.round((inst.interval_ms || DEFAULT_INTERVAL_MS) / 1000)}s)`);
    }

    const timers = [];
    for (const inst of instances) {
      const interval = inst.interval_ms || DEFAULT_INTERVAL_MS;
      const tick = async () => {
        await pollInstance(inst, state, pusher, host);
        saveState(state);
      };
      timers.push(setInterval(tick, interval));
      tick();  // fire one immediately
    }

    return {
      stop() {
        for (const t of timers) clearInterval(t);
        saveState(state);
      },
    };
  },
};
