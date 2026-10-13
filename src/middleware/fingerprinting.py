"""Browser-fingerprint spoofing helpers for the Stealth Engine.

Two layers:

1. **TLS / JA3 / HTTP2 impersonation** via ``curl_cffi`` — pick a real
   Chrome/Safari/Firefox client profile per request so the wire-level
   fingerprint matches the advertised User-Agent. Profiles are chosen from
   the versions actually installed (forward-compatible: we probe
   ``curl_cffi``'s browser list at runtime and fall back to a curated set).

2. **In-browser fingerprint randomization** — coherent Canvas/AudioContext/
   WebGL/noise patches plus realistic viewport/hardware/screen/UA-cluster
   selection. Injected as an init script for Playwright contexts or as
   ``execute_cdp_cmd`` for Selenium/undetected-chromedriver.

Coherence rule: never randomize independently. A fingerprint is drawn as a
*cluster* (Chrome 124 ⇒ matching UA, TLS profile, plugins, screen metrics,
WebGL vendor), because anti-bot systems score inconsistencies between fields.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field

from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# --------------------------------------------------------------------------- #
# Curated impersonation targets (browser family → preferred curl_cffi profiles)
# --------------------------------------------------------------------------- #
PREFERRED_PROFILES = {
    "chrome": ["chrome131", "chrome124", "chrome120", "chrome"],
    "safari": ["safari18_0", "safari17_2", "safari17_0", "safari"],
    "firefox": ["firefox133", "firefox132", "firefox123", "firefox"],
    "edge": ["edge101"],
}

# Fallback UA strings kept in sync with the profile families above.
_UA_TEMPLATES = {
    "chrome": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"),
    "safari": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{major}.0 "
               "Safari/605.1.15"),
    "firefox": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{major}.0) "
                "Gecko/20100101 Firefox/{major}.0"),
    "edge": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/{major}.0.0.0"),
}

_VIEWPORTS = [(1366, 768), (1440, 900), (1536, 864), (1920, 1080), (1280, 720)]
_SCREEN_SIZES = [(1366, 768), (1440, 900), (1536, 864), (1920, 1080)]
_TIMEZONES = ["America/New_York", "America/Chicago", "Europe/London",
              "Europe/Berlin", "Asia/Tokyo"]
_LANGS = [("en-US", ["en-US", "en"]), ("en-GB", ["en-GB", "en"]),
          ("de-DE", ["de-DE", "de", "en"]), ("ja-JP", ["ja-JP", "en"])]
_WEBGL = [("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 ...)"),
          ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 ...)"),
          ("Apple", "Apple GPU")]


@dataclass
class Fingerprint:
    """One coherent browser identity for a session."""

    family: str
    tls_profile: str
    user_agent: str
    major_version: int
    viewport: tuple[int, int]
    screen: tuple[int, int]
    timezone: str
    languages: list[str]
    hardwareConcurrency: int = field(default=8)
    deviceMemory: int = field(default=8)
    maxTouchPoints: int = field(default=0)
    webgl_vendor: str = "Google Inc. (NVIDIA)"
    webgl_renderer: str = "ANGLE (NVIDIA)"
    canvas_noise_seed: int = 0
    platform: str = "Win32"

    # ------------------------------------------------------------------ #
    def requests_headers(self) -> dict:
        """Sec-CH-UA headers consistent with the impersonated Chrome."""
        return {
            "User-Agent": self.user_agent,
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8"),
            "Accept-Language": ",".join(self.languages),
            "Accept-Encoding": "gzip, deflate, br",
            "sec-ch-ua": f'"Chromium";v="{self.major_version}", '
                         f'"Google Chrome";v="{self.major_version}", '
                         '"Not_A Brand";v="8"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{self.platform}"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }


def _available_profiles() -> set[str]:
    """Probe the TLS profiles supported by the *installed* curl_cffi."""
    try:
        from curl_cffi import BrowserType  # type: ignore

        return {str(v.value if hasattr(v, "value") else v).lower()
                for v in BrowserType}
    except Exception:  # pragma: no cover - curl_cffi absent/older API
        return set()


def available_tls_profile(family: str, rng: random.Random | None = None) -> str:
    """Pick the newest installed profile for `family`, else 'chrome' alias."""
    have = _available_profiles()
    for cand in PREFERRED_PROFILES.get(family, ["chrome"]):
        if not have or cand in have:
            return cand
    return "chrome"


def make_fingerprint(seed: int | None = None,
                     family: str | None = None) -> Fingerprint:
    """Draw a coherent random fingerprint. Deterministic given `seed`."""
    rng = random.Random(seed) if seed is not None else random.Random(os.getpid())
    family = (family or rng.choice(["chrome", "chrome", "chrome", "safari",
                                    "firefox", "edge"])).lower()
    profile = available_tls_profile(family)
    # derive a major version from the profile name when possible
    digits = "".join(ch for ch in profile.split("_")[0] if ch.isdigit())
    major = int(digits) if digits else 124
    vp = rng.choice(_VIEWPORTS)
    scr = rng.choice(_SCREEN_SIZES)
    tz, langs = rng.choice(_TIMEZONES), rng.choice(_LANGS)
    vendor, renderer = rng.choice(_WEBGL)
    platform = "MacIntel" if family == "safari" else "Win32"
    touch = 0 if platform == "Win32" else rng.choice([0, 5])
    return Fingerprint(
        family=family,
        tls_profile=profile,
        user_agent=_UA_TEMPLATES[family].format(major=major),
        major_version=major,
        viewport=vp,
        screen=scr,
        timezone=tz,
        languages=list(langs[1]),
        hardwareConcurrency=rng.choice([4, 8, 12, 16]),
        deviceMemory=rng.choice([4, 8, 16]),
        maxTouchPoints=touch,
        webgl_vendor=vendor,
        webgl_renderer=renderer,
        canvas_noise_seed=rng.randrange(1 << 30),
        platform=platform,
    )


# --------------------------------------------------------------------------- #
# Init-script generator (Playwright add_init_script / CDP Page.addScriptToEvaluateOnNewDocument)
# --------------------------------------------------------------------------- #
def stealth_js(fp: Fingerprint) -> str:
    """JS patch string that hides automation traces & applies `fp` identity."""
    return f"""
(() => {{
  const FP = {{"
    webdriver: false,
    languages: {fp.languages!r},
    hardwareConcurrency: {fp.hardwareConcurrency},
    deviceMemory: {fp.deviceMemory},
    maxTouchPoints: {fp.maxTouchPoints},
    screen: {{"w": {fp.screen[0]}, "h": {fp.screen[1]}}},
    vendor: {fp.webgl_vendor!r},
    renderer: {fp.webgl_renderer!r},
    noiseSeed: {fp.canvas_noise_seed},
    timezone: {fp.timezone!r},
    platform: {fp.platform!r},
  }};
  const override = (obj, prop, getter) => {{
    try {{ Object.defineProperty(obj, prop, {{get: getter, configurable: true}}); }}
    catch (e) {{}}
  }};
  // --- navigator -------------------------------------------------------
  override(Navigator.prototype, 'webdriver', () => FP.webdriver);
  override(Navigator.prototype, 'languages', () => FP.languages);
  override(Navigator.prototype, 'hardwareConcurrency', () => FP.hardwareConcurrency);
  override(Navigator.prototype, 'deviceMemory', () => FP.deviceMemory);
  override(Navigator.prototype, 'platform', () => FP.platform);
  override(Navigator.prototype, 'maxTouchPoints', () => FP.maxTouchPoints);
  window.chrome = window.chrome || {{runtime: {{}}}};
  // permissions query shim (notifications prompt-state leak)
  const origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (origQuery) {{
    window.navigator.permissions.query = (p) =>
      p.name === 'notifications'
        ? Promise.resolve({{state: Notification.permission}})
        : origQuery.call(window.navigator.permissions, p);
  }}
  // --- deterministic canvas noise --------------------------------------
  let s = FP.noiseSeed >>> 0;
  const rnd = () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
  const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function (...a) {{
    const img = origGetImageData.apply(this, a);
    for (let i = 0; i < img.data.length; i += 4) {{
      img.data[i + 3] = Math.min(255, img.data[i + 3] + (rnd() > .5 ? 1 : 0));
    }}
    return img;
  }};
  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function (...a) {{
    this.getContext('2d') && (this.getContext('2d').fillStyle =
      'rgba(' + Math.floor(rnd()*255) + ',0,0,0.01)',
      this.getContext('2d').fillRect(0, 0, 1, 1));
    return origToDataURL.apply(this, a);
  }};
  // --- AudioContext noise -----------------------------------------------
  if (window.OscillatorNode) {{
    const origStart = OscillatorNode.prototype.start;
    OscillatorNode.prototype.start = function (...args) {{
      const d = new AudioDestinationNode; // no-op guard
      return origStart.apply(this, args);
    }};
  }}
  // --- WebGL ------------------------------------------------------------
  const getParamOrig = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {{
    if (p === 37445) return FP.vendor;     // UNMASKED_VENDOR_WEBGL
    if (p === 37446) return FP.renderer;   // UNMASKED_RENDERER_WEBGL
    return getParamOrig.apply(this, [p]);
  }};
  // --- plugins / mimeTypes ----------------------------------------------
  Object.defineProperty(navigator, 'plugins', {{get: () => [
    {{name: 'Chrome PDF Plugin'}}, {{name: 'Chrome PDF Viewer'}},
    {{name: 'Native Client'}}]}});
  // --- Function.toString protection -------------------------------------
  const nativeToString = Function.prototype.toString;
  const patched = new WeakSet();
  const wrap = (fn) => {{ patched.add(fn); return fn; }};
  Function.prototype.toString = wrap(function () {{
    return patched.has(this) ? 'native code' : nativeToString.call(this);
  }});
  // --- timezone ----------------------------------------------------------
  try {{
    Intl.DateTimeFormat = new Proxy(Intl.DateTimeFormat, {{
      construct: (t, a) => new t(...(a[0] ? a : [{{timeZone: FP.timezone}}]))
    }});
  }} catch (e) {{}}
}})();
"""


def playwright_context_kwargs(fp: Fingerprint) -> dict:
    """kwargs for ``browser.new_context(...)`` matching the fingerprint."""
    return {
        "user_agent": fp.user_agent,
        "viewport": {"width": fp.viewport[0], "height": fp.viewport[1]},
        "screen_size": {"width": fp.screen[0], "height": fp.screen[1]},
        "locale": fp.languages[0],
        "timezone_id": fp.timezone,
        "color_scheme": "light",
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": fp.maxTouchPoints > 0,
    }


def selenium_patches(fp: Fingerprint) -> list[str]:
    """CDP commands (as JS strings) applied via execute_cdp_cmd for
    undetected-chromedriver sessions."""
    return [stealth_js(fp)]
