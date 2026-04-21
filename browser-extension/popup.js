import {
  MODE,
  getRuntime,
  hostnameOf,
  loadSettings,
  saveSettings,
  setRuntime
} from "./lib/config.js";

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

function setStatus(text, kind = "") {
  const el = $("#status-text");
  el.textContent = text || "";
  el.classList.remove("warn", "ok");
  if (kind) el.classList.add(kind);
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab || null;
}

function paintStartStop(mode) {
  const btn = $("#start-stop");
  if (mode === MODE.OFF) {
    btn.textContent = "Start capture";
    btn.classList.remove("stop");
    btn.classList.add("start");
  } else {
    btn.textContent = "Stop capture";
    btn.classList.remove("start");
    btn.classList.add("stop");
  }
}

function paintToggle(preferred) {
  for (const b of document.querySelectorAll(".toggle-btn")) {
    const on = b.dataset.mode === preferred;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  }
}

async function render() {
  const settings = await loadSettings();
  const runtime = await getRuntime();

  paintStartStop(runtime.mode);
  paintToggle(runtime.preferredMode);

  if (!settings.apiToken) {
    setStatus("paste an API token in settings →", "warn");
  } else if (runtime.mode === MODE.OFF) {
    setStatus("paused");
  } else {
    setStatus(runtime.mode === MODE.FOLLOW_ME ? "following all tabs" : "capturing this tab", "ok");
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

async function setMode(nextMode) {
  await chrome.runtime.sendMessage({ type: "runtime.setMode", mode: nextMode });
}

async function onStartStop() {
  const runtime = await getRuntime();
  if (runtime.mode === MODE.OFF) {
    await setMode(runtime.preferredMode);
  } else {
    await setMode(MODE.OFF);
  }
  render();
}

async function onToggle(e) {
  const next = e.currentTarget.dataset.mode;
  if (!next) return;
  await setRuntime({ preferredMode: next });
  const runtime = await getRuntime();
  if (runtime.mode !== MODE.OFF) {
    await setMode(next);
  }
  render();
}

async function onCaptureNow() {
  const btn = $("#capture-now");
  btn.disabled = true;
  setStatus("capturing…");
  const resp = await chrome.runtime.sendMessage({ type: "capture.manual" });
  if (resp?.ok) {
    setStatus("captured", "ok");
  } else if (resp?.reason === "blocked") {
    setStatus("this page is on the blocklist", "warn");
  } else if (resp?.reason === "no-token") {
    setStatus("paste an API token in settings →", "warn");
  } else {
    setStatus("capture failed — check settings", "warn");
  }
  btn.disabled = false;
  render();
}

async function onBlockPage() {
  const tab = await activeTab();
  if (!tab || !tab.url) {
    setStatus("no active page", "warn");
    return;
  }
  const host = hostnameOf(tab.url);
  if (host === "unknown") {
    setStatus("can't read hostname", "warn");
    return;
  }
  const settings = await loadSettings();
  const blocklist = Array.isArray(settings.blocklist) ? settings.blocklist.slice() : [];
  const already = blocklist.some((h) => (h || "").trim().toLowerCase() === host);
  if (already) {
    setStatus(`${host} already blocked`);
    return;
  }
  blocklist.push(host);
  await saveSettings({ blocklist });
  setStatus(`blocked ${host}`, "ok");
}

function onOpenOptions(e) {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
}

document.addEventListener("DOMContentLoaded", async () => {
  await render();

  $("#start-stop").addEventListener("click", onStartStop);
  for (const b of document.querySelectorAll(".toggle-btn")) {
    b.addEventListener("click", onToggle);
  }
  $("#capture-now").addEventListener("click", onCaptureNow);
  $("#block-page").addEventListener("click", onBlockPage);
  $("#open-options").addEventListener("click", onOpenOptions);
});
