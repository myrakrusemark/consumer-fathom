import { DEFAULTS, loadSettings, saveSettings } from "./lib/config.js";

const $ = (sel) => document.querySelector(sel);

function hydrate(settings) {
  $("#api-url").value = settings.apiUrl;
  $("#api-token").value = settings.apiToken;
  $("#ttl-seconds").value = String(settings.ttlSeconds);
  $("#scroll-debounce").value = String(settings.scrollDebounceMs);
  $("#scroll-threshold").value = String(settings.scrollThresholdPct);
  $("#blocklist").value = settings.blocklist.join("\n");
}

function readForm() {
  const blocklist = $("#blocklist")
    .value.split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
  return {
    apiUrl: $("#api-url").value.trim() || DEFAULTS.apiUrl,
    apiToken: $("#api-token").value.trim(),
    ttlSeconds: Math.max(60, parseInt($("#ttl-seconds").value, 10) || DEFAULTS.ttlSeconds),
    scrollDebounceMs: Math.max(
      200,
      parseInt($("#scroll-debounce").value, 10) || DEFAULTS.scrollDebounceMs
    ),
    scrollThresholdPct: Math.max(
      10,
      Math.min(100, parseInt($("#scroll-threshold").value, 10) || DEFAULTS.scrollThresholdPct)
    ),
    blocklist
  };
}

function flashStatus(text) {
  const el = $("#save-status");
  el.textContent = text;
  setTimeout(() => {
    if (el.textContent === text) el.textContent = "";
  }, 2500);
}

document.addEventListener("DOMContentLoaded", async () => {
  hydrate(await loadSettings());

  $("#save").addEventListener("click", async () => {
    await saveSettings(readForm());
    flashStatus("saved");
  });

  $("#reset").addEventListener("click", async () => {
    await chrome.storage.sync.clear();
    await chrome.storage.sync.set(DEFAULTS);
    hydrate(DEFAULTS);
    flashStatus("reset to defaults");
  });
});
