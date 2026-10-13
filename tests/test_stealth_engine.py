"""Stealth Engine tests — offline-safe, no network, no paid providers.

Covers: provider adapters (Bright Data / Oxylabs), fingerprint coherence &
TLS-profile selection, proxy pool integration with providers, CapSolver
client (with injected transport), and the Success-Rate Monitor's automatic
strategy switching.
"""

import time

import pytest


# --------------------------------------------------------------------------- #
# Provider adapters
# --------------------------------------------------------------------------- #
def test_brightdata_gateway_url_shape(monkeypatch):
    monkeypatch.setenv("PROVIDER_BRIGHTDATA_USERNAME", "cust123")
    monkeypatch.setenv("PROVIDER_BRIGHTDATA_PASSWORD", "secretpw")
    monkeypatch.setenv("PROVIDER_BRIGHTDATA_ZONE", "web_res")

    from src.middleware.providers import BrightDataAdapter

    a = BrightDataAdapter()
    assert a.enabled
    p = a.gateway(tier="residential", country="US", session_id="abc", sticky_ttl_min=30)
    assert p.url.startswith("http://cust123-zone-web_res-session-abc-country-us-sessTime-30:")
    assert "@gate.brightdata.com:22225" in p.url
    assert p.tier == "residential"


def test_brightdata_mobile_zone_and_sanitization(monkeypatch):
    monkeypatch.setenv("PROVIDER_BRIGHTDATA_USERNAME", "cust")
    monkeypatch.setenv("PROVIDER_BRIGHTDATA_PASSWORD", "pw")
    monkeypatch.setenv("PROVIDER_BRIGHTDATA_MOBILE_ZONE", "My Mobile Zone!")

    from src.middleware.providers import BrightDataAdapter

    p = BrightDataAdapter().gateway(tier="mobile")
    # zone sanitized: spaces/punct -> underscore
    assert "zone-my_mobile_zone_" in p.url


def test_oxylabs_gateway_targets(monkeypatch):
    monkeypatch.setenv("PROVIDER_OXYLABS_USERNAME", "oxy_user")
    monkeypatch.setenv("PROVIDER_OXYLABS_PASSWORD", "oxy_pass")

    from src.middleware.providers import OxylabsAdapter

    a = OxylabsAdapter()
    res = a.gateway(tier="residential", country="de", sticky_ttl_min=10)
    mob = a.gateway(tier="mobile")
    dc = a.gateway(tier="datacenter")
    assert "gateway-geo.oxylabs.io:9731" in res.url
    assert "cc-DE-session-" in res.url and "sessTTL-10" in res.url
    assert "gateway-mobile.oxylabs.io:9731" in mob.url
    assert "gateway.oxylabs.io:8000" in dc.url


def test_provider_disabled_without_credentials(monkeypatch):
    for var in ("PROVIDER_BRIGHTDATA_USERNAME", "PROVIDER_BRIGHTDATA_PASSWORD",
                "PROVIDER_OXYLABS_USERNAME", "PROVIDER_OXYLABS_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    from src.middleware.providers import BrightDataAdapter, get_provider

    assert not BrightDataAdapter().enabled
    assert BrightDataAdapter().get_proxies() == []
    monkeypatch.setenv("PROXY_PROVIDER", "nope")
    assert get_provider() is None


def test_get_provider_selection(monkeypatch):
    from src.middleware.providers import BrightDataAdapter, get_provider

    monkeypatch.setenv("PROXY_PROVIDER", "bright_data")
    assert isinstance(get_provider(), BrightDataAdapter)
    assert isinstance(get_provider("oxylabs").name, str)


# --------------------------------------------------------------------------- #
# Proxy pool <-> provider integration
# --------------------------------------------------------------------------- #
def test_proxy_pool_refreshes_from_provider(monkeypatch):
    for var in ("PROXY_DATACENTER", "PROXY_RESIDENTIAL", "PROXY_MOBILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PROVIDER_OXYLABS_USERNAME", "u")
    monkeypatch.setenv("PROVIDER_OXYLABS_PASSWORD", "p")

    from src.middleware.proxy_rotation import ProxyRotationMiddleware, ProxyTier

    mw = ProxyRotationMiddleware(provider="oxylabs")
    assert mw.adapter is not None and mw.adapter.enabled
    before = len(mw.proxies)
    pool = mw.get_proxy()          # empty static pool triggers refresh
    assert pool and pool["http"].startswith("http://")
    assert len(mw.proxies) > before
    tiers = {p.tier for p in mw.proxies}
    assert ProxyTier.MOBILE in tiers  # mobile tier included


def test_mobile_tier_weighted_highest():
    from src.middleware.proxy_rotation import ProxyTier, TIER_WEIGHTS

    assert TIER_WEIGHTS[ProxyTier.MOBILE] > TIER_WEIGHTS[ProxyTier.RESIDENTIAL] \
        > TIER_WEIGHTS[ProxyTier.DATACENTER]


# --------------------------------------------------------------------------- #
# Fingerprinting
# --------------------------------------------------------------------------- #
def test_fingerprint_coherence_and_determinism():
    from src.middleware.fingerprinting import make_fingerprint

    fp1 = make_fingerprint(seed=42)
    fp2 = make_fingerprint(seed=42)
    assert fp1.user_agent == fp2.user_agent
    assert fp1.tls_profile == fp2.tls_profile
    # UA family must match TLS profile family (coherent cluster)
    fam = {"chrome": "Chrome", "safari": "Safari", "firefox": "Firefox",
           "edge": "Edg"}[fp1.family]
    if fp1.family != "safari":
        assert fam in fp1.user_agent
    assert fp1.tls_profile.startswith(fp1.family[:min(3, len(fp1.family))]
                                      if fp1.family != "edge" else "edge")


def test_fingerprint_headers_consistent():
    from src.middleware.fingerprinting import make_fingerprint

    h = make_fingerprint(seed=7).requests_headers()
    assert h["User-Agent"]
    assert h["sec-ch-ua-mobile"] == "?0"
    assert '"Chromium";v="' in h["sec-ch-ua"]


def test_tls_profile_runtime_probe_is_safe():
    """available_tls_profile never raises even without curl_cffi installed."""
    from src.middleware.fingerprinting import available_tls_profile

    assert available_tls_profile("chrome")
    assert available_tls_profile("unknown-family") == "chrome"


def test_stealth_js_contains_key_patches():
    from src.middleware.fingerprinting import make_fingerprint, stealth_js

    js = stealth_js(make_fingerprint(seed=1))
    for marker in ("webdriver", "hardwareConcurrency", "UNMASKED_VENDOR_WEBGL",
                   "getImageData", "Function.prototype.toString"):
        assert marker in js


# --------------------------------------------------------------------------- #
# CapSolver
# --------------------------------------------------------------------------- #
class FakeTransport:
    def __init__(self, results):
        self.calls = []
        self.results = list(results)

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        return self.results.pop(0)


def test_capsolver_requires_key(monkeypatch):
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    from src.middleware.capsolver import CapSolverClient, CapSolverError

    c = CapSolverClient(api_key="")
    assert not c.enabled
    with pytest.raises(CapSolverError):
        c.create_task({"type": "ImageToTextTask", "body": "xx"})


def test_capsolver_turnstile_flow(monkeypatch):
    monkeypatch.setenv("CAPSOLVER_API_KEY", "k")
    from src.middleware.capsolver import API_CREATE, API_RESULT, CapSolverClient

    fake = FakeTransport([
        {"errorId": 0, "taskId": "t-1"},
        {"errorId": 0, "status": "processing"},
        {"errorId": 0, "status": "ready",
         "solution": {"token": "cf-token-xyz", "type": "CloudflareTurnstileTask"}},
    ])
    c = CapSolverClient(transport=fake, poll_interval=0.01)
    sol = c.solve_turnstile("https://shop.example/login", "0xSITEKEY")
    assert sol.token == "cf-token-xyz"
    assert sol.as_form_field()["cf-turnstile-response"] == "cf-token-xyz"
    assert fake.calls[0][0] == API_CREATE
    assert fake.calls[0][1]["task"]["websiteKey"] == "0xSITEKEY"
    assert any(url == API_RESULT for url, _ in fake.calls)


def test_capsolver_error_propagates(monkeypatch):
    from src.middleware.capsolver import CapSolverClient, CapSolverError

    fake = FakeTransport([{"errorId": 7, "errorMessage": "ERROR_KEY_DOES_NOT_EXIST"}])
    c = CapSolverClient(api_key="k", transport=fake)
    with pytest.raises(CapSolverError, match="ERROR_KEY_DOES_NOT_EXIST"):
        c.create_task({"type": "RecaptchaV2TaskProxyless"})


def test_capsolver_timeout(monkeypatch):
    from src.middleware.capsolver import CapSolverClient, CapSolverError

    class AlwaysProcessing:
        def __call__(self, url, payload):
            return {"errorId": 0, "status": "processing"}

    c = CapSolverClient(api_key="k", transport=AlwaysProcessing(), poll_interval=0.01)
    with pytest.raises(CapSolverError, match="timed out"):
        c.wait_result("t-9", timeout=0.05)


# --------------------------------------------------------------------------- #
# Success-Rate Monitor
# --------------------------------------------------------------------------- #
def test_monitor_starts_with_cheapest_strategy():
    from src.middleware.success_monitor import StrategyMonitor

    m = StrategyMonitor(strategies=["http_plain", "curl_cffi", "stealth"])
    # unproven ladder: first entry wins ties
    assert m.best_strategy() == "http_plain"


def test_monitor_auto_switch_on_degradation():
    from src.middleware.success_monitor import Outcome, StrategyMonitor

    m = StrategyMonitor(strategies=["http_plain", "curl_cffi"],
                        min_success_rate=0.5, min_samples=4)
    for _ in range(4):
        m.record("http_plain", Outcome.BLOCKED)
    assert m.best_strategy() == "curl_cffi"      # auto-promoted
    snap = m.snapshot()
    assert snap["strategies"]["http_plain"]["degraded"] is True
    assert snap["strategies"]["http_plain"]["success_rate"] == 0.0


def test_monitor_recovers_after_cooldown():
    from src.middleware.success_monitor import Outcome, StrategyMonitor

    m = StrategyMonitor(strategies=["a", "b"], min_success_rate=0.5,
                        min_samples=2, cooldown_seconds=0.05)
    m.record("a", Outcome.BLOCKED)
    m.record("a", Outcome.BLOCKED)
    assert m.best_strategy() == "b"
    time.sleep(0.06)
    # 'a' re-enters probation; 'b' still has better proven rate, but 'a'
    # becomes available again
    assert m.states["a"].available


def test_monitor_next_after_reports_and_switches():
    from src.middleware.success_monitor import Outcome, StrategyMonitor

    m = StrategyMonitor(strategies=["plain", "tls"], min_success_rate=0.5,
                        min_samples=2)
    nxt = m.next_after("plain", Outcome.BLOCKED)
    nxt = m.next_after("plain", Outcome.BLOCKED)
    assert nxt == "tls"
    assert m.snapshot()["switches"] >= 1


def test_classify_helpers():
    from src.middleware.success_monitor import Outcome, classify

    assert classify(status=200, html_snippet="<html>hello</html>") == Outcome.SUCCESS
    assert classify(status=429) == Outcome.BLOCKED
    assert classify(status=200, html_snippet="<title>Just a moment...</title>") \
        == Outcome.BLOCKED
    assert classify(exception=ConnectionError("boom")) == Outcome.ERROR
    assert classify(status=500) == Outcome.ERROR


def test_run_with_fallback_escalates_until_success():
    from src.middleware.success_monitor import StrategyMonitor

    m = StrategyMonitor(strategies=["plain", "tls", "browser"])

    def fetch(strategy):
        return {"plain": (403, "denied"), "tls": (200, "<html>ok</html>"),
                "browser": (200, "never")}[strategy]

    strat, status, body = m.run_with_fallback(fetch, "https://x.example")
    assert strat == "tls" and status == 200 and body == "<html>ok</html>"


def test_monitor_singleton_reset():
    from src.middleware import success_monitor as sm

    sm.reset_monitor()
    a = sm.get_monitor()
    b = sm.get_monitor()
    assert a is b
    sm.reset_monitor()
    assert sm.get_monitor() is not a


# --------------------------------------------------------------------------- #
# StealthFetcher wiring (offline-safe)
# --------------------------------------------------------------------------- #
def test_stealth_fetcher_records_outcomes(monkeypatch):
    """Injected fake strategies feed the monitor and auto-switch."""
    from src.middleware.stealth_middleware import StealthFetcher
    from src.middleware.success_monitor import Outcome, StrategyMonitor

    m = StrategyMonitor(strategies=["http_plain", "curl_cffi"])
    f = StealthFetcher(monitor=m)
    monkeypatch.delenv("WCP_OFFLINE", raising=False)

    calls = {"plain": 0, "cffi": 0}

    def fake_plain(url):
        calls["plain"] += 1
        raise RuntimeError("403 blocked")

    def fake_cffi(url):
        calls["cffi"] += 1
        return type("R", (), {})  # placeholder to check via monkey below

    monkeypatch.setattr(f, "_fetch_plain", fake_plain)
    # degrade plain fully so monitor prefers curl_cffi immediately
    for _ in range(8):
        m.record("http_plain", Outcome.BLOCKED)

    def fake_cffi_ok(url):
        from src.middleware.stealth_middleware import PageResult

        calls["cffi"] += 1
        return PageResult(url=url, html="<html>ok</html>", status=200)

    monkeypatch.setattr(f, "_fetch_curl_cffi", fake_cffi_ok)
    res = f.fetch("https://blocked.example")
    assert res.html == "<html>ok</html>"
    assert calls["cffi"] == 1
    assert m.states["curl_cffi"].stats.success_rate == 1.0
