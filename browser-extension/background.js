// Follow Me — background service worker.
//
// Owns capture state, paints the toolbar badge to show the ring-light,
// listens for navigation + content-script hints, and uploads screenshots
// to the consumer-fathom api with an authoritative TTL.

import {
  DEFAULTS,
  MODE,
  getRuntime,
  isBlocked,
  loadSettings,
  setRuntime,
  shortTitleFrom
} from "./lib/config.js";
import { dataUrlToBlob, uploadScreenshot } from "./lib/capture.js";

const BADGE_COLOR_ACTIVE = "#6366f1";
const BADGE_COLOR_CAPTURING = "#f59e0b";
const BADGE_COLOR_UNCONFIGURED = "#ef4444";
const CAPTURE_COOLDOWN_MS = 1500; // per-tab rate limit
const MAX_RECENTS = 5;

const lastCaptureAt = new Map(); // tabId -> timestamp ms
const scrollTimers = new Map(); // tabId -> setTimeout handle
const scrollBaseline = new Map(); // tabId -> last captured scroll fraction

// ── Capture eligibility ──────────────────────────────────────────────────────

async function isTabEligible(tab, settings) {
  const runtime = await getRuntime();
  if (runtime.mode === MODE.OFF) return false;
  if (runtime.mode === MODE.THIS_TAB && !runtime.activeTabs.includes(tab.id)) {
    return false;
  }
  if (tab.incognito) return false;
  if (isBlocked(tab.url, settings.blocklist)) return false;
  return true;
}

// ── Badge paint ──────────────────────────────────────────────────────────────

async function paintBadgeForTab(tabId) {
  const runtime = await getRuntime();
  const settings = await loadSettings();

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return;
  }
  if (!tab) return;

  const active =
    runtime.mode === MODE.FOLLOW_ME ||
    (runtime.mode === MODE.THIS_TAB && runtime.activeTabs.includes(tabId));
  const blocked = tab.url ? isBlocked(tab.url, settings.blocklist) : true;

  if (!active || blocked) {
    await chrome.action.setBadgeText({ tabId, text: "" });
    return;
  }
  if (!settings.apiToken) {
    await chrome.action.setBadgeBackgroundColor({ tabId, color: BADGE_COLOR_UNCONFIGURED });
    await chrome.action.setBadgeText({ tabId, text: "KEY" });
    return;
  }
  await chrome.action.setBadgeBackgroundColor({ tabId, color: BADGE_COLOR_ACTIVE });
  await chrome.action.setBadgeText({ tabId, text: "ON" });
}

async function flashBadgeCapturing(tabId) {
  await chrome.action.setBadgeBackgroundColor({ tabId, color: BADGE_COLOR_CAPTURING });
  await chrome.action.setBadgeText({ tabId, text: "REC" });
  setTimeout(() => paintBadgeForTab(tabId), 800);
}

// ── Capture pipeline ─────────────────────────────────────────────────────────

async function captureTab(tab, reason) {
  const settings = await loadSettings();
  if (!(await isTabEligible(tab, settings))) return null;
  if (!settings.apiToken) {
    console.warn("[follow-me] no apiToken set, skipping capture");
    return null;
  }

  const now = Date.now();
  const last = lastCaptureAt.get(tab.id) || 0;
  if (now - last < CAPTURE_COOLDOWN_MS) return null;
  lastCaptureAt.set(tab.id, now);

  await flashBadgeCapturing(tab.id);

  let dataUrl;
  try {
    dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
      format: "png"
    });
  } catch (err) {
    console.warn("[follow-me] captureVisibleTab failed:", err);
    return null;
  }

  let result;
  try {
    const blob = await dataUrlToBlob(dataUrl);
    result = await uploadScreenshot({
      apiUrl: settings.apiUrl,
      apiToken: settings.apiToken,
      blob,
      tabId: tab.id,
      url: tab.url,
      title: tab.title,
      reason,
      ttlSeconds: settings.ttlSeconds
    });
  } catch (err) {
    console.warn("[follow-me] upload failed:", err);
    return null;
  }

  const runtime = await getRuntime();
  const recents = [
    {
      id: result.id,
      mediaHash: result.media_hash,
      url: tab.url,
      title: shortTitleFrom(tab.url, tab.title),
      reason,
      at: new Date().toISOString(),
      expiresAt: new Date(Date.now() + settings.ttlSeconds * 1000).toISOString()
    },
    ...(runtime.recents || [])
  ].slice(0, MAX_RECENTS);
  await setRuntime({ recents });

  return result;
}

// ── Triggers ─────────────────────────────────────────────────────────────────

chrome.webNavigation.onCompleted.addListener(async (details) => {
  if (details.frameId !== 0) return;
  try {
    const tab = await chrome.tabs.get(details.tabId);
    await paintBadgeForTab(tab.id);
    await captureTab(tab, "navigation");
    scrollBaseline.set(tab.id, 0);
  } catch (err) {
    console.warn("[follow-me] navigation trigger failed:", err);
  }
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  await paintBadgeForTab(tabId);
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
  if (changeInfo.status === "complete" || changeInfo.url) {
    await paintBadgeForTab(tabId);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  lastCaptureAt.delete(tabId);
  scrollTimers.delete(tabId);
  scrollBaseline.delete(tabId);
  getRuntime().then((rt) => {
    if (rt.activeTabs.includes(tabId)) {
      setRuntime({ activeTabs: rt.activeTabs.filter((id) => id !== tabId) });
    }
  });
});

// Content-script messages: scroll + blur hints, manual capture via popup.

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg?.type === "capture.blur") {
      if (sender.tab) await captureTab(sender.tab, "blur");
      sendResponse({ ok: true });
      return;
    }

    if (msg?.type === "capture.scroll") {
      if (!sender.tab) {
        sendResponse({ ok: false });
        return;
      }
      const settings = await loadSettings();
      const tabId = sender.tab.id;
      const fraction = Math.max(0, Math.min(1, msg.scrollFraction || 0));
      const baseline = scrollBaseline.get(tabId) || 0;
      const threshold = settings.scrollThresholdPct / 100;
      const enoughProgress = fraction - baseline >= threshold;

      if (scrollTimers.has(tabId)) clearTimeout(scrollTimers.get(tabId));
      const fire = async () => {
        scrollTimers.delete(tabId);
        let tab;
        try {
          tab = await chrome.tabs.get(tabId);
        } catch {
          return;
        }
        await captureTab(tab, "scroll");
        scrollBaseline.set(tabId, fraction);
      };
      if (enoughProgress) {
        fire();
      } else {
        scrollTimers.set(tabId, setTimeout(fire, settings.scrollDebounceMs));
      }
      sendResponse({ ok: true });
      return;
    }

    if (msg?.type === "capture.manual") {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (tab) {
        const settings = await loadSettings();
        if (tab.incognito || isBlocked(tab.url, settings.blocklist)) {
          sendResponse({ ok: false, reason: "blocked" });
          return;
        }
        if (!settings.apiToken) {
          sendResponse({ ok: false, reason: "no-token" });
          return;
        }
        lastCaptureAt.delete(tab.id);
        const result = await captureTab(tab, "manual");
        sendResponse({ ok: !!result, result });
      } else {
        sendResponse({ ok: false, reason: "no-tab" });
      }
      return;
    }

    if (msg?.type === "runtime.setMode") {
      const next = msg.mode;
      if (![MODE.OFF, MODE.THIS_TAB, MODE.FOLLOW_ME].includes(next)) {
        sendResponse({ ok: false });
        return;
      }
      const runtime = await getRuntime();
      let activeTabs = runtime.activeTabs;
      if (next === MODE.OFF) activeTabs = [];
      if (next === MODE.THIS_TAB) {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        activeTabs = tab ? [tab.id] : [];
      }
      await setRuntime({ mode: next, activeTabs });
      const allTabs = await chrome.tabs.query({});
      for (const t of allTabs) await paintBadgeForTab(t.id);
      sendResponse({ ok: true });
      return;
    }

    if (msg?.type === "runtime.revokeRecent") {
      const runtime = await getRuntime();
      const recents = (runtime.recents || []).filter((r) => r.id !== msg.id);
      await setRuntime({ recents });
      sendResponse({ ok: true });
      return;
    }
  })();
  return true;
});

chrome.runtime.onInstalled.addListener(async () => {
  const existing = await chrome.storage.sync.get(Object.keys(DEFAULTS));
  const patch = {};
  for (const [k, v] of Object.entries(DEFAULTS)) {
    if (existing[k] === undefined) patch[k] = v;
  }
  if (Object.keys(patch).length) await chrome.storage.sync.set(patch);
});
