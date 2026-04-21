// Shared config — settings keys, defaults, storage access.

export const DEFAULTS = {
  apiUrl: "http://localhost:8201",
  apiToken: "",
  ttlSeconds: 86400,
  expires: true,
  scrollDebounceMs: 2000,
  scrollThresholdPct: 80,
  // Hostname suffix-match. Entries match exact hostname and subdomains.
  blocklist: [
    "accounts.google.com",
    "mail.google.com",
    "login.microsoftonline.com",
    "1password.com",
    "lastpass.com",
    "bitwarden.com",
    "paypal.com",
    "venmo.com",
    "cash.app",
    "stripe.com",
    "coinbase.com",
    "kraken.com",
    "bankofamerica.com",
    "chase.com",
    "wellsfargo.com",
    "capitalone.com"
  ]
};

const SYNC_KEYS = [
  "apiUrl",
  "apiToken",
  "ttlSeconds",
  "expires",
  "scrollDebounceMs",
  "scrollThresholdPct",
  "blocklist"
];

export async function loadSettings() {
  const stored = await chrome.storage.sync.get(SYNC_KEYS);
  const merged = { ...DEFAULTS };
  for (const k of SYNC_KEYS) {
    if (stored[k] !== undefined) merged[k] = stored[k];
  }
  return merged;
}

export async function saveSettings(patch) {
  await chrome.storage.sync.set(patch);
}

const LOCAL_KEYS = ["enabled", "mode", "recents"];

export async function getRuntime() {
  const stored = await chrome.storage.local.get(LOCAL_KEYS);
  // Backward compat: migrate from the older mode-based state. Anything
  // other than "off" (follow_me, this_tab) counted as enabled.
  let enabled = stored.enabled;
  if (enabled === undefined && stored.mode !== undefined) {
    enabled = stored.mode !== "off";
  }
  return {
    enabled: enabled ?? false,
    recents: stored.recents ?? []
  };
}

export async function setRuntime(patch) {
  await chrome.storage.local.set(patch);
}

export function isBlocked(url, blocklist) {
  if (!url) return true;
  try {
    const u = new URL(url);
    if (!/^https?:$/.test(u.protocol)) return true;
    const host = u.hostname.toLowerCase();
    for (const raw of blocklist) {
      const pat = (raw || "").trim().toLowerCase();
      if (!pat) continue;
      if (host === pat) return true;
      if (host.endsWith("." + pat)) return true;
    }
    return false;
  } catch {
    return true;
  }
}

export function hostnameOf(url) {
  try {
    return new URL(url).hostname || "unknown";
  } catch {
    return "unknown";
  }
}

export function shortTitleFrom(url, title) {
  try {
    const u = new URL(url);
    const host = u.hostname;
    const path = u.pathname.length > 60 ? u.pathname.slice(0, 57) + "…" : u.pathname;
    const base = `${host}${path}`;
    return title ? `${base} — ${title}` : base;
  } catch {
    return title || url || "";
  }
}
