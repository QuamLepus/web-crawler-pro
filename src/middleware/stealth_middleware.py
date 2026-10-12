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

    def __init__(self, headless: bool = True, proxy: dict | None = None):
        self.headless = headless or os.getenv("HEADLESS", "1") == "1"
        self.proxy = proxy  # {"server": "http://host:port", "username": ..., "password": ...}

    # ------------------------------------------------------------------ #
    def fetch(self, url: str) -> PageResult:
        for method in (self._fetch_curl_cffi, self._fetch_playwright,
                       self._fetch_uc):
            try:
                return method(url)
            except ImportError:
                logger.debug("%s not installed, trying next strategy", method.__name__)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s failed for %s: %s", method.__name__, url, exc)
        raise RuntimeError(f"All stealth fetch strategies failed for {url}")

    # -- 1. TLS fingerprinting ------------------------------------------ #
    def _fetch_curl_cffi(self, url: str) -> PageResult:
        from curl_cffi import requests as cc_requests  # lazy import

        # Impersonate a real Chrome TLS/JA3 + HTTP2 fingerprint
        resp = cc_requests.get(
            url,
            impersonate="chrome124",
            proxies={"http": self.proxy["server"]} if self.proxy else None,
            timeout=cfg.REQUEST_TIMEOUT,
        )
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

        with sync_playwright() as p:
            launch_kwargs: dict = {
                "headless": self.headless,
                "args": CHROME_DISABLE_STRICT,
            }
            if self.proxy:
                launch_kwargs["proxy"] = self.proxy
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=cfg.USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            context.add_init_script(self._STEALTH_JS)
            page = context.new_page()
            resp = page.goto(url, wait_until="domcontentloaded",
                             timeout=cfg.REQUEST_TIMEOUT * 1000)
            html = page.content()
            status = resp.status if resp else 200
            browser.close()
            return PageResult(url=url, html=html, status=status)

    # -- 3. undetected-chromedriver --------------------------------------- #
    def _fetch_uc(self, url: str) -> PageResult:
        import undetected_chromedriver as uc  # lazy import
        from selenium.webdriver.chrome.options import Options

        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        for flag in CHROME_DISABLE_STRICT:
            opts.add_argument(flag)
        opts.add_argument(f"--user-agent={cfg.USER_AGENT}")
        opts.add_argument(
            f"--window-size={random.randint(1280, 1920)},"
            f"{random.randint(800, 1080)}"
        )
        driver = uc.Chrome(options=opts)
        try:
            driver.get(url)
            return PageResult(url=url, html=driver.page_source, status=200)
        finally:
            driver.quit()
