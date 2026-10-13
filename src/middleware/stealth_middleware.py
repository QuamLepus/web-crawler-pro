"""Stealth browser middleware — Playwright (with stealth patches) and
undetected-chromedriver fallback.

Used when plain HTTP fetching is blocked (JS-rendered pages, Cloudflare
challenge pages). All heavy optional dependencies are imported lazily so
the base install stays lightweight.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass

from src.config import get_config
from src.middleware.success_monitor import Outcome, classify as classify_outcome
from src.utils.logger import setup_logger

logger = setup_logger(__name__)
cfg = get_config()

# Common Chrome flags that trip automation detectors — removed by default.
CHROME_DISABLE_STRICT = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


@dataclass
class PageResult:
    url: str
    html: str
    status: int = 200


class StealthFetcher:
    """Fetch pages through a stealth headless browser.

    Strategy order:
      1. curl_cffi (TLS fingerprint impersonation — cheapest, no browser)
      2. playwright + stealth init scripts
      3. undetected-chromedriver (sync fallback)
    """

    def __init__(self, headless: bool = True, proxy: dict | None = None,
                 fingerprint=None, monitor=None):
        self.headless = headless or os.getenv("HEADLESS", "1") == "1"
        self.proxy = proxy  # {"server": "http://host:port", "username": ..., "password": ...}
        # Coherent browser identity (TLS profile + UA + canvas/webgl patches).
        # When None a fresh one is drawn per fetch unless STEALTH_STICKY=1.
        self.fingerprint = fingerprint
        # Success-Rate Monitor for auto strategy escalation.
        if monitor is None:
            from src.middleware.success_monitor import get_monitor

            monitor = get_monitor()
        self.monitor = monitor

    # ------------------------------------------------------------------ #
    def _fp(self):
        from src.middleware.fingerprinting import make_fingerprint

        sticky = os.getenv("STEALTH_STICKY", "0") == "1"
        if self.fingerprint is None or not sticky:
            self.fingerprint = make_fingerprint()
        return self.fingerprint

    def fetch(self, url: str) -> PageResult:
        """Fetch `url` via the Stealth Engine.

        Strategy ladder (cheap → expensive), each outcome reported to the
        Success-Rate Monitor which auto-switches when a strategy degrades:

          http_plain → curl_cffi(TLS spoof) → playwright_stealth → uc_stealth
        """
        if os.getenv("WCP_OFFLINE", "0") == "1":
            # Offline / CI guard: never hit the network from unit tests.
            raise RuntimeError(
                f"All stealth fetch strategies failed for {url} (offline mode)")

        ladder = {
            "http_plain": self._fetch_plain,
            "curl_cffi": self._fetch_curl_cffi,
            "playwright_stealth": self._fetch_playwright,
            "uc_stealth": self._fetch_uc,
        }
        tried: list[str] = []
        last_exc: Exception | None = None
        while len(tried) < len(ladder):
            strat = self.monitor.best_strategy()
            if strat not in ladder or strat in tried:
                strat = next((s for s in ladder if s not in tried), None)
                if strat is None:
                    break
            tried.append(strat)
            try:
                result = ladder[strat](url)
            except ImportError as exc:
                # dependency absent — permanent skip, don't pollute stats
                last_exc = exc
                continue
            except Exception as exc:  # noqa: BLE001
                snippet = getattr(exc, "page", "") or ""
                outcome = classify_outcome(exception=exc, html_snippet=str(snippet))
                self.monitor.record(strat, outcome)
                last_exc = exc
                logger.warning("stealth %s failed for %s: %s", strat, url, exc)
                continue
            self.monitor.record(strat, Outcome.SUCCESS)
            return result
        raise RuntimeError(f"All stealth fetch strategies failed for {url}"
                           + (f" (last error: {last_exc})" if last_exc else ""))

    # -- 0. plain HTTP with fingerprint-consistent headers ---------------- #
    def _fetch_plain(self, url: str) -> PageResult:
        import requests

        fp = self._fp()
        resp = requests.get(url, timeout=get_config().REQUEST_TIMEOUT,
                            headers=fp.requests_headers(),
                            proxies=self.proxy or None)
        resp.raise_for_status()
        return PageResult(url=url, html=resp.text, status=resp.status_code)

    # -- 1. TLS fingerprinting ------------------------------------------ #
    def _fetch_curl_cffi(self, url: str) -> PageResult:
        from curl_cffi import requests as cc_requests  # lazy import

        from src.middleware.success_monitor import Outcome

        fp = self._fp()
        # Impersonate a real browser TLS/JA3 + HTTP2 fingerprint coherent
        # with the advertised User-Agent (profile chosen at runtime from the
        # versions curl_cffi actually ships — see fingerprinting.py).
        resp = cc_requests.get(
            url,
            impersonate=fp.tls_profile,
            headers={k: v for k, v in fp.requests_headers().items()
                     if k != "User-Agent"},
            proxies={k: v for k, v in (self.proxy or {}).items()
                     if k in ("http", "https")} or None,
            timeout=cfg.REQUEST_TIMEOUT,
        )
        if resp.status_code in (401, 403, 407, 429, 503):
            self.monitor.record("curl_cffi", Outcome.BLOCKED)
            raise RuntimeError(f"blocked (HTTP {resp.status_code}) by {url}")
        resp.raise_for_status()
        return PageResult(url=url, html=resp.text, status=resp.status_code)

    # -- 2. Playwright + stealth ------------------------------------------- #
    _STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    window.chrome = { runtime: {} };
    """

    def _fetch_playwright(self, url: str) -> PageResult:
        from playwright.sync_api import sync_playwright  # lazy import

        from src.middleware.fingerprinting import (playwright_context_kwargs,
                                                   stealth_js)

        fp = self._fp()
        with sync_playwright() as p:
            launch_kwargs: dict = {
                "headless": self.headless,
                "args": CHROME_DISABLE_STRICT,
            }
            if self.proxy:
                launch_kwargs["proxy"] = self.proxy
            browser = p.chromium.launch(**launch_kwargs)
            # Context options drawn from the coherent fingerprint cluster …
            context = browser.new_context(**playwright_context_kwargs(fp))
            # … and in-page patches (canvas/webgl/audio/noise) matching it.
            context.add_init_script(stealth_js(fp))
            page = context.new_page()
            resp = page.goto(url, wait_until="domcontentloaded",
                             timeout=cfg.REQUEST_TIMEOUT * 1000)
            html = page.content()
            status = resp.status if resp else 200
            browser.close()
            if status in (401, 403, 407, 429, 503):
                raise RuntimeError(f"blocked (HTTP {status}) by {url}")
            return PageResult(url=url, html=html, status=status)

    # -- 3. undetected-chromedriver --------------------------------------- #
    def _fetch_uc(self, url: str) -> PageResult:
        import undetected_chromedriver as uc  # lazy import
        from selenium.webdriver.chrome.options import Options

        from src.middleware.fingerprinting import stealth_js

        fp = self._fp()
        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        for flag in CHROME_DISABLE_STRICT:
            opts.add_argument(flag)
        opts.add_argument(f"--user-agent={fp.user_agent}")
        opts.add_argument(
            f"--window-size={fp.viewport[0]},{fp.viewport[1]}"
        )
        driver = uc.Chrome(options=opts)
        try:
            # CDP-level stealth patches consistent with this session's FP
            driver.execute_cdp_cmd("Page.enable", {})
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                                   {"source": stealth_js(fp)})
            driver.get(url)
            return PageResult(url=url, html=driver.page_source, status=200)
        finally:
            driver.quit()
