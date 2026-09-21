importScripts("https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.sw.js");

// Minimal fetch handler so the browser recognizes this as a real, installable
// PWA service worker (no offline caching needed for an internal dashboard).
self.addEventListener("fetch", function (event) {
  // Pass every request straight through to the network.
});
