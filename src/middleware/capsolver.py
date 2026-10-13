"""CapSolver integration — modern captcha/Antibot-js challenge solver.

CapSolver (https://capsolver.com) handles the challenge families that
2Captcha/Anti-Captcha struggle with:

    * CloudTurnstileTaskProxyless   – Cloudflare Turnstile
    * AntiCloudflareTaskProxyless   – CF interstitial / "Just a moment"
    * ReCaptchaV2TaskProxyless / V3 – Google reCAPTCHA
    * GeeTestTaskProxyless          – GeeTest slider/captcha
    * ImageToTextTask               – simple OCR captchas

Flow (uniform across task types):
    1. POST  https://api.capsolver.com/createTask   -> taskId
    2. POST  https://api.capsolver.com/getTaskResult (poll) -> solution
    3. map solution token onto the site's form field / widget callback

The client is dependency-light (stdlib urllib) and fully injectable: tests
feed a fake ``transport`` callable, production uses real HTTPS. When no
``CAPSOLVER_API_KEY`` is configured :attr:`enabled` is False and callers
should fall back to :class:`src.middleware.captcha_solver.CaptchaSolver`.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from src.utils.logger import setup_logger

logger = setup_logger(__name__)

API_CREATE = "https://api.capsolver.com/createTask"
API_RESULT = "https://api.capsolver.com/getTaskResult"
API_BALANCE = "https://api.capsolver.com/getBalance"

# Which env var holds the key (kept distinct from legacy CAPTCHA_API_KEY)
KEY_ENV = "CAPSOLVER_API_KEY"


class CapSolverError(RuntimeError):
    pass


@dataclass
class Solution:
    task_id: str
    token: str                 # generic solve token (cf-turnstile-response,
                               # g-recaptcha-response, ...)
    task_type: str
    raw: dict

    def as_form_field(self, domain: str = "") -> dict:
        """Field names commonly expected when POSTing the solved token."""
        return {
            "g-recaptcha-response": self.token,
            "cf-turnstile-response": self.token,
            "captchaResponse": self.token,
        }


# transport: callable(url, payload_dict) -> response_dict  (injectable)
Transport = Callable[[str, dict], dict]


def _http_transport(url: str, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "replace"))


class CapSolverClient:
    """Thin, testable wrapper around the CapSolver REST API."""

    def __init__(self, api_key: str | None = None, client_secret: str | None = None,
                 transport: Transport | None = None, poll_interval: float = 5.0):
        self.api_key = api_key or os.getenv(KEY_ENV, "")
        self.client_secret = client_secret or os.getenv("CAPSOLVER_CLIENT_SECRET", "")
        self._transport = transport or _http_transport
        self.poll_interval = poll_interval

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _post(self, url: str, payload: dict) -> dict:
        body = {"clientKey": self.api_key, "task": payload["task"]}
        if "taskId" in payload:
            body = {"clientKey": self.api_key, "taskId": payload["taskId"]}
        if self.client_secret:
            body["clientSecret"] = self.client_secret
        try:
            resp = self._transport(url, body)
        except (urllib.error.URLError, OSError) as exc:
            raise CapSolverError(f"capsolver network error: {exc}") from exc
        if resp.get("errorId", 0) != 0:
            raise CapSolverError(
                f"capsolver error {resp.get('errorId')}: {resp.get('errorMessage')}")
        return resp

    # ------------------------------------------------------------------ #
    def create_task(self, task: dict) -> str:
        if not self.enabled:
            raise CapSolverError(f"{KEY_ENV} not configured")
        resp = self._post(API_CREATE, {"task": task})
        task_id = resp.get("taskId", "")
        if not task_id:
            raise CapSolverError(f"capsolver returned no taskId: {resp}")
        logger.debug("capsolver task created: %s (%s)", task_id, task.get("type"))
        return task_id

    def wait_result(self, task_id: str, timeout: int = 120) -> Solution:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(min(self.poll_interval, max(0.1, deadline - time.time())))
            resp = self._post(API_RESULT, {"taskId": task_id})
            status = resp.get("status")
            if status == "ready":
                sol = resp.get("solution", {})
                token = (sol.get("token") or sol.get("gRecaptchaResponse")
                         or sol.get("solveInfo") or json.dumps(sol))
                return Solution(task_id=task_id, token=token,
                                task_type=resp.get("solution", {}).get("type", ""),
                                raw=sol)
            if status != "processing":
                raise CapSolverError(f"unexpected capsolver status: {resp}")
        raise CapSolverError(f"capsolver task {task_id} timed out after {timeout}s")

    def solve(self, task: dict, timeout: int = 120) -> Solution:
        return self.wait_result(self.create_task(task), timeout=timeout)

    # -- typed helpers ----------------------------------------------------- #
    def solve_turnstile(self, site_url: str, sitekey: str, action: str = "",
                        timeout: int = 120) -> Solution:
        return self.solve({
            "type": "CloudflareTurnstileTaskProxyless",
            "websiteURL": site_url,
            "websiteKey": sitekey,
            "metadata": {"action": action} if action else {},
        }, timeout=timeout)

    def solve_cloudflare(self, site_url: str, timeout: int = 180) -> Solution:
        return self.solve({
            "type": "AntiCloudflareTaskProxyless",
            "websiteURL": site_url,
        }, timeout=timeout)

    def solve_recaptcha_v2(self, site_url: str, sitekey: str,
                           invisible: bool = False, timeout: int = 120) -> Solution:
        return self.solve({
            "type": "RecaptchaV2TaskProxyless",
            "websiteURL": site_url,
            "websiteKey": sitekey,
            "isInvisible": invisible,
        }, timeout=timeout)

    def solve_ocr(self, base64_image: str, timeout: int = 60) -> Solution:
        return self.solve({
            "type": "ImageToTextTask",
            "body": base64_image,
        }, timeout=timeout)

    # ------------------------------------------------------------------ #
    def balance(self) -> float:
        if not self.enabled:
            return 0.0
        body = {"clientKey": self.api_key}
        if self.client_secret:
            body["clientSecret"] = self.client_secret
        try:
            return float(self._transport(API_BALANCE, body).get("balance", 0.0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("capsolver balance failed: %s", exc)
            return 0.0
