// Screenshot capture + upload to consumer-fathom's /v1/media/upload.

import { hostnameOf } from "./config.js";

export async function dataUrlToBlob(dataUrl) {
  const res = await fetch(dataUrl);
  return await res.blob();
}

export async function uploadScreenshot({
  apiUrl,
  apiToken,
  blob,
  tabId,
  url,
  title,
  reason,
  ttlSeconds,
  expires
}) {
  const host = hostnameOf(url);
  const now = new Date();

  const content = JSON.stringify({
    url,
    title: title || "",
    reason,
    tabId,
    capturedAt: now.toISOString()
  });

  const tags = [
    "follow-me",
    "browse",
    "screenshot",
    `reason:${reason}`,
    `tab:${tabId}`,
    `host:${host}`
  ].join(",");

  const form = new FormData();
  form.append("file", blob, `follow-me-${Date.now()}.png`);
  form.append("content", content);
  form.append("tags", tags);
  form.append("source", `browser-extension:${host}`);
  if (expires !== false && ttlSeconds) {
    const expiresAt = new Date(now.getTime() + ttlSeconds * 1000).toISOString();
    form.append("expires_at", expiresAt);
  }

  const headers = {};
  if (apiToken) headers["Authorization"] = `Bearer ${apiToken}`;

  const endpoint = `${apiUrl.replace(/\/$/, "")}/v1/media/upload`;
  const resp = await fetch(endpoint, {
    method: "POST",
    headers,
    body: form
  });

  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`Upload failed ${resp.status}: ${text.slice(0, 200)}`);
  }
  return await resp.json();
}
