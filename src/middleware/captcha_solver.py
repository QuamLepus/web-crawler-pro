"""CAPTCHA solving integration (2Captcha / Anti-Captcha).

Both providers share the same flow:
  1. submit(sitekey, url, [invisible]) -> captcha id
  2. poll in_balance/in_progress until solved -> token
  3. feed token back into the form / grecaptcha callback

The module is provider-agnostic; pick one via CAPTCHA_PROVIDER env var.
"""

from __future__ import annotations

import os
import time

import requests

from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class CaptchaSolverError(RuntimeError):
    pass


class CaptchaSolver:
    PROVIDERS = {
        "2captcha": {
            "submit": "http://2captcha.com/in.php",
            "result": "http://2captcha.com/res.php",
        },
        "anti-captcha": {
            "submit": "http://api.anti-captcha.com/createTask",
            "result": "http://api.anti-captcha.com/getTaskResult",
        },
    }

    def __init__(self, api_key: str | None = None, provider: str | None = None):
        self.api_key = api_key or os.getenv("CAPTCHA_API_KEY", "")
        self.provider = provider or os.getenv("CAPTCHA_PROVIDER", "2captcha")
        if self.provider not in self.PROVIDERS:
            raise CaptchaSolverError(f"Unknown captcha provider: {self.provider}")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------ #
    def solve_recaptcha_v2(self, sitekey: str, page_url: str,
                           invisible: bool = False, timeout: int = 120) -> str:
        """Solve a reCAPTCHA v2 and return the response token."""
        if not self.enabled:
            raise CaptchaSolverError("CAPTCHA_API_KEY not configured")

        if self.provider == "2captcha":
            captcha_id = self._submit_2captcha(sitekey, page_url, invisible)
            return self._poll_2captcha(captcha_id, timeout)
        return self._solve_anticaptcha(sitekey, page_url, invisible, timeout)

    # -- 2captcha --------------------------------------------------------- #
    def _submit_2captcha(self, sitekey: str, page_url: str, invisible: bool) -> str:
        payload = {
            "key": self.api_key,
            "method": "recaptcha" + ("invisible" if invisible else ""),
            "googlekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        }
        r = requests.post(self.PROVIDERS["2captcha"]["submit"], data=payload, timeout=30)
        data = r.json()
        if data.get("status") != 1:
            raise CaptchaSolverError(f"2captcha submit failed: {data}")
        return data["request"]

    def _poll_2captcha(self, captcha_id: str, timeout: int) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            r = requests.get(
                self.PROVIDERS["2captcha"]["result"],
                params={"key": self.api_key, "action": "get", "id": captcha_id, "json": 1},
                timeout=30,
            ).json()
            if r.get("status") == 1:
                logger.info("captcha solved in budget")
                return r["request"]
            if r.get("request") != "CAPCHA_NOT_READY":
                raise CaptchaSolverError(f"2captcha error: {r}")
        raise CaptchaSolverError("captcha solving timed out")

    # -- anti-captcha ------------------------------------------------------- #
    def _solve_anticaptcha(self, sitekey: str, page_url: str,
                           invisible: bool, timeout: int) -> str:
        task = {
            "clientKey": self.api_key,
            "task": {
                "type": "RecaptchaV2Task" + ("Invisible" if invisible else "Proxyless"),
                "websiteURL": page_url,
                "websiteKey": sitekey,
                "isInvisible": invisible,
            },
        }
        task_id = requests.post(self.PROVIDERS["anti-captcha"]["submit"],
                                json=task, timeout=30).json()["taskId"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            res = requests.post(
                self.PROVIDERS["anti-captcha"]["result"],
                json={"clientKey": self.api_key, "taskId": task_id},
                timeout=30,
            ).json()
            if res.get("status") == "ready":
                return res["solution"]["gRecaptchaResponse"]
            if res.get("errorId"):
                raise CaptchaSolverError(f"anti-captcha error: {res}")
        raise CaptchaSolverError("captcha solving timed out")
