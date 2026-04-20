import { MODE, getRuntime, loadSettings, saveSettings } from "./lib/config.js";

const $ = (sel) => document.querySelector(sel);

function fmtRelative(iso) {
  const then = new Date(iso).getTime();
  const delta = Date.now() - then;
  const minutes = Math.round(delta / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return `${hours}h ago`;
}

function setStatus(text, { warn = false } = {}) {
  const el = $("#status-text");
  el.textContent = text;
  el.style.color = warn ? "var(--warn)" : "var(--muted)";
}

async function render() {
  const settings = await loadSettings();
  const runtime = await getRuntime();

  for (const radio of document.querySelectorAll("input[name=mode]")) {
    radio.checked = radio.value === runtime.mode;
  }

  const ttlMin = Math.round(settings.ttlSeconds / 60);
  $("#ttl-slider").value = String(ttlMin);
  $("#ttl-label").textContent = String(ttlMin);

  if (!settings.apiToken) {
    setStatus("paste an API token in Settings →", { warn: true });
  }

  const list = $("#recents-list");
  list.innerHTML = "";
  if (!runtime.recents || runtime.recents.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "no recent captures";
    list.appendChild(li);
  } else {
    for (const r of runtime.recents) {
      const li = document.createElement("li");

      const left = document.createElement("div");
      left.className = "title";
      left.textContent = r.title || r.url || "(untitled)";
      left.title = r.url;

      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = `${r.reason} · ${fmtRelative(r.at)}`;

      const btn = document.createElement("button");
      btn.className = "revoke";
      btn.textContent = "×";
      btn.title = "Hide from recents";
      btn.addEventListener("click", async () => {
        await chrome.runtime.sendMessage({ type: "runtime.revokeRecent", id: r.id });
        render();
      });

      li.appendChild(left);
      li.appendChild(meta);
      li.appendChild(btn);
      list.appendChild(li);
    }
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  await render();

  for (const radio of document.querySelectorAll("input[name=mode]")) {
    radio.addEventListener("change", async (e) => {
      const mode = e.target.value;
      await chrome.runtime.sendMessage({ type: "runtime.setMode", mode });
      setStatus(`mode: ${mode.replace("_", " ")}`);
      render();
    });
  }

  $("#ttl-slider").addEventListener("input", (e) => {
    $("#ttl-label").textContent = e.target.value;
  });

  $("#ttl-slider").addEventListener("change", async (e) => {
    const minutes = parseInt(e.target.value, 10);
    await saveSettings({ ttlSeconds: minutes * 60 });
    setStatus(`TTL: ${minutes}m`);
  });

  $("#capture-now").addEventListener("click", async () => {
    $("#capture-now").disabled = true;
    setStatus("capturing…");
    const resp = await chrome.runtime.sendMessage({ type: "capture.manual" });
    if (resp?.ok) {
      setStatus("captured");
    } else if (resp?.reason === "blocked") {
      setStatus("this page is on the blocklist", { warn: true });
    } else if (resp?.reason === "no-token") {
      setStatus("paste an API token in Settings →", { warn: true });
    } else {
      setStatus("capture failed — check Settings → API URL/token", { warn: true });
    }
    $("#capture-now").disabled = false;
    render();
  });

  $("#open-options").addEventListener("click", (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });
});

void MODE;
