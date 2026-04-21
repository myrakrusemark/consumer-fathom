// Follow Me — content script.
// Reports scroll progress and blur events to the background service worker.
// Doesn't know about settings or blocklist — background decides.

(function () {
  if (window.__fathomFollowMeInjected) return;
  window.__fathomFollowMeInjected = true;

  function scrollFraction() {
    const doc = document.documentElement;
    const max = (doc.scrollHeight || 1) - window.innerHeight;
    if (max <= 0) return 0;
    return Math.max(0, Math.min(1, window.scrollY / max));
  }

  let scrollScheduled = false;
  window.addEventListener(
    "scroll",
    () => {
      if (scrollScheduled) return;
      scrollScheduled = true;
      requestAnimationFrame(() => {
        scrollScheduled = false;
        try {
          chrome.runtime.sendMessage({
            type: "capture.scroll",
            scrollFraction: scrollFraction()
          });
        } catch {
          // Service worker may be asleep; best-effort.
        }
      });
    },
    { passive: true }
  );

  window.addEventListener(
    "blur",
    () => {
      try {
        chrome.runtime.sendMessage({ type: "capture.blur" });
      } catch {
        // Best-effort.
      }
    },
    { passive: true }
  );
})();
