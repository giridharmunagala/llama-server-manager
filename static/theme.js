"use strict";

// Runs before first paint so a saved theme does not flash the default one.
(() => {
  let theme = "light";
  try {
    theme = localStorage.getItem("theme") || "light";
  } catch {
    // Storage may be unavailable; keep the default.
  }
  document.documentElement.dataset.theme = theme === "dark" ? "dark" : "light";
})();
