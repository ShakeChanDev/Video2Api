"""Playwright 反检测兜底脚本。"""

from __future__ import annotations

from typing import List

BASE_STEALTH_SCRIPT = r"""
(() => {
  const defineGetter = (obj, key, value) => {
    try {
      Object.defineProperty(obj, key, {
        configurable: true,
        get: () => value
      });
    } catch (e) {}
  };

  defineGetter(Navigator.prototype, "webdriver", false);

  if (!navigator.languages || navigator.languages.length === 0) {
    defineGetter(Navigator.prototype, "languages", ["en-US", "en"]);
  }

  if (!navigator.plugins || navigator.plugins.length === 0) {
    const fakePlugins = {
      length: 3,
      0: {name: "Chrome PDF Plugin", filename: "internal-pdf-viewer"},
      1: {name: "Chrome PDF Viewer", filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai"},
      2: {name: "Native Client", filename: "internal-nacl-plugin"},
      item: function(i) { return this[i] || null; },
      namedItem: function() { return null; }
    };
    defineGetter(Navigator.prototype, "plugins", fakePlugins);
  }

  if (!window.chrome) {
    try {
      window.chrome = { runtime: {} };
    } catch (e) {}
  }

  const perms = navigator.permissions;
  if (perms && typeof perms.query === "function") {
    const originalQuery = perms.query.bind(perms);
    perms.query = (parameters) => {
      if (parameters && parameters.name === "notifications") {
        return Promise.resolve({ state: Notification.permission });
      }
      return originalQuery(parameters);
    };
  }
})();
"""


MOBILE_CREATE_STAGE_SCRIPT = r"""
(() => {
  const defineGetter = (obj, key, value) => {
    try {
      Object.defineProperty(obj, key, {
        configurable: true,
        get: () => value
      });
    } catch (e) {}
  };

  defineGetter(Navigator.prototype, "platform", "iPhone");
  defineGetter(Navigator.prototype, "maxTouchPoints", 5);
  defineGetter(Navigator.prototype, "hardwareConcurrency", 8);

  try {
    if (!("ontouchstart" in window)) {
      Object.defineProperty(window, "ontouchstart", {
        configurable: true,
        value: null
      });
    }
  } catch (e) {}
})();
"""


def get_stealth_init_scripts(stage: str = "default") -> List[str]:
    scripts: List[str] = [BASE_STEALTH_SCRIPT]
    if str(stage or "").strip().lower() == "create":
        scripts.append(MOBILE_CREATE_STAGE_SCRIPT)
    return scripts
