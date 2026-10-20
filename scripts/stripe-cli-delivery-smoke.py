"""Exercise a Stripe CLI webhook delivery against a local API in test mode.

The Stripe CLI trigger creates a test-fixture event (not a completed Checkout
Session); the CLI forwards it over HTTP and signs it with its ephemeral
webhook secret. The separate pytest integration test exercises Checkout API
creation against a real test Price.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1:8000"
WEBHOOK_PATH = "/api/v1/billing/stripe/webhook"


def request_json(path: str, *, method: str = "GET", body: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"API {method} {path} returned HTTP {exc.code}: {detail}") from exc


def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    secret_key = os.environ.get("STRIPE_TEST_SECRET_KEY", "")
    price_id = os.environ.get("STRIPE_TEST_PRICE_ID", "")
    if not secret_key.startswith("sk_test_"):
        raise RuntimeError("refusing Stripe CLI smoke: STRIPE_TEST_SECRET_KEY must use sk_test_")
    if not price_id.startswith("price_"):
        raise RuntimeError("STRIPE_TEST_PRICE_ID must identify a Stripe Price")

    root = Path(__file__).resolve().parents[1]
    api_process: subprocess.Popen[bytes] | None = None
    listener_process: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryDirectory(prefix="wcp-stripe-cli-") as temp_dir:
        temp = Path(temp_dir)
        listener_log_path = temp / "stripe-listen.log"
        api_log_path = temp / "api.log"
        with listener_log_path.open("wb") as listener_log:
            listener_process = subprocess.Popen(
                [
                    "stripe", "listen", "--api-key", secret_key,
                    "--skip-update", "--forward-to", BASE_URL + WEBHOOK_PATH,
                    "--events", "checkout.session.completed",
                ],
                cwd=root,
                stdout=listener_log,
                stderr=subprocess.STDOUT,
            )
        try:
            signing_secret = ""
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if listener_process.poll() is not None:
                    raise RuntimeError("Stripe CLI listener exited before becoming ready")
                text = listener_log_path.read_text(errors="replace")
                match = re.search(r"webhook signing secret is (whsec_[-_A-Za-z0-9]+)", text)
                if match:
                    signing_secret = match.group(1)
                    break
                time.sleep(1)
            if not signing_secret:
                raise RuntimeError("Stripe CLI did not report a webhook signing secret")

            server_env = os.environ.copy()
            server_env.update({
                "REDIS_URL": os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                "STRIPE_SECRET_KEY": secret_key,
                "STRIPE_PRICE_PRO": price_id,
                "STRIPE_WEBHOOK_SECRET": signing_secret,
            })
            with api_log_path.open("wb") as api_log:
                api_process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "src.api.main:app",
                     "--host", "127.0.0.1", "--port", "8000"],
                    cwd=root,
                    env=server_env,
                    stdout=api_log,
                    stderr=subprocess.STDOUT,
                )
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if api_process.poll() is not None:
                    raise RuntimeError("API server exited before its health check passed")
                try:
                    request_json("/healthz")
                    break
                except (HTTPError, URLError, TimeoutError, OSError):
                    time.sleep(1)
            else:
                raise RuntimeError("API server did not become healthy")

            suffix = uuid.uuid4().hex[:12]
            signup = request_json("/auth/signup", method="POST", body={
                "org_name": f"Stripe CLI smoke {suffix}",
                "email": f"stripe-cli-{suffix}@example.invalid",
                "password": f"T3st-{suffix}-Password!",
            })
            api_key = request_json(
                "/keys", method="POST", body={"name": "Stripe CLI delivery smoke"},
                headers={"Authorization": f"Bearer {signup['access_token']}"},
            )["key"]

            trigger_args = [
                "stripe", "trigger", "checkout.session.completed",
                "--api-key", secret_key,
                "--override", f"checkout_session:metadata.tenant={signup['org_id']}",
                "--override", "checkout_session:metadata.plan=pro",
            ]
            result = subprocess.run(
                trigger_args, cwd=root, capture_output=True, text=True,
                timeout=240, check=False,
            )
            if result.returncode:
                raise RuntimeError("Stripe CLI could not trigger the test-mode event")

            headers = {"X-API-Key": api_key}
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                usage = request_json("/v1/billing/usage", headers=headers)
                if usage.get("plan") == "pro":
                    print("Stripe CLI forwarded a signed test-mode event; tenant plan is Pro.")
                    return 0
                time.sleep(2)
            raise RuntimeError("Stripe CLI event did not activate the test tenant")
        finally:
            stop_process(api_process)
            stop_process(listener_process)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never print credentials, webhook secrets, or raw Stripe CLI output.
        print(f"Stripe CLI delivery smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
