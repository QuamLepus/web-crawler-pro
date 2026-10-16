"""Tests for the server-rendered web console (``src/webui``).

The console is a client of the public API, so these tests drive it the way a
browser does: sign up, carry the cookie, follow redirects, submit forms. That
is the only way to catch the class of bug this layer is prone to — a template
that renders fine until an endpoint returns a list instead of a dict.

Everything runs in-process (``ASGITransport``), so no port is ever bound.
"""

from __future__ import annotations

import pytest

pytest.importorskip("jinja2", reason="jinja2 is required for the web console")

import fakeredis
from fastapi.testclient import TestClient

from src.api.main import create_api_app
from src.webui.app import NAV, PLANS, webhook_events

PASSWORD = "correct-horse-1"


@pytest.fixture()
def app():
    return create_api_app(
        fakeredis.FakeStrictRedis(decode_responses=True), enable_graphql=False
    )


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def signed_in(client):
    """A client holding a valid session cookie for a brand-new tenant."""
    res = client.post(
        "/signup",
        data={
            "org_name": "Console Test Org",
            "email": "console@example.test",
            "password": PASSWORD,
        },
    )
    assert res.status_code == 200, res.text
    assert "wcp_session" in client.cookies
    return client


# --------------------------------------------------------------------------- #
# public pages
# --------------------------------------------------------------------------- #
def test_landing_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    # every nav destination and every plan name is reachable from the landing page
    assert "Sign in" in res.text


@pytest.mark.parametrize("path", ["/", "/pricing", "/login", "/signup"])
def test_public_pages_are_html(client, path):
    res = client.get(path)
    assert res.status_code == 200
    assert res.text.lstrip().startswith("<!doctype html")


def test_static_assets_are_served_locally(client):
    """No CDN: the console must ship its own CSS and JS."""
    for asset in ("/static/app.css", "/static/app.js"):
        res = client.get(asset)
        assert res.status_code == 200, asset
        assert len(res.content) > 500, asset
    css = client.get("/static/app.css").text
    # a design system, not a bootstrap import
    assert "--brand-500" in css


def test_console_assets_never_reference_a_cdn(signed_in):
    res = signed_in.get("/app")
    for host in ("cdn.", "cdnjs", "unpkg", "jsdelivr", "googleapis"):
        assert host not in res.text, f"console must not depend on {host}"


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def test_console_pages_require_a_session(client):
    for path in (
        "/app",
        "/app/search",
        "/app/crawl",
        "/app/keys",
        "/app/billing",
        "/app/settings",
        "/app/docs",
        "/app/index",
        "/app/marketplace",
        "/app/discovery",
    ):
        res = client.get(path, follow_redirects=False)
        assert res.status_code == 303, path
        assert res.headers["location"] == "/login"


def test_signup_then_dashboard(signed_in):
    res = signed_in.get("/app")
    assert res.status_code == 200
    assert "Dashboard" in res.text


def test_login_round_trip(client, signed_in):
    client.post("/logout")
    res = client.post(
        "/login", data={"email": "console@example.test", "password": PASSWORD}
    )
    assert res.status_code == 200
    assert client.get("/app").status_code == 200


def test_bad_password_is_reported_not_crashed(client, signed_in):
    client.post("/logout")
    res = client.post(
        "/login", data={"email": "console@example.test", "password": "wrong-password"}
    )
    assert res.status_code == 200
    assert "wcp_session" not in client.cookies


def test_logout_clears_the_session(signed_in):
    signed_in.post("/logout")
    res = signed_in.get("/app", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login"


def test_short_password_is_rejected_by_the_api(client):
    res = client.post(
        "/signup",
        data={"org_name": "Too Short", "email": "x@y.test", "password": "abc"},
    )
    assert res.status_code == 200  # form re-render, not a 500
    assert "wcp_session" not in client.cookies


# --------------------------------------------------------------------------- #
# every console page
# --------------------------------------------------------------------------- #
PAGES = [
    "/app",
    "/app/search",
    "/app/crawl",
    "/app/discovery",
    "/app/index",
    "/app/marketplace",
    "/app/keys",
    "/app/billing",
    "/app/settings",
    "/app/docs",
]


@pytest.mark.parametrize("path", PAGES)
def test_every_console_page_renders(signed_in, path):
    res = signed_in.get(path)
    assert res.status_code == 200, (path, res.text[:400])
    assert "<!doctype html" in res.text.lower()


def test_search_page_with_a_query(signed_in):
    res = signed_in.get("/app/search", params={"q": "solar panels"})
    assert res.status_code == 200
    assert "solar panels" in res.text


def test_search_page_is_google_shaped_when_empty(signed_in):
    """An empty search shows a hero, not a table of nothing."""
    res = signed_in.get("/app/search")
    assert res.status_code == 200
    assert 'class="search-hero"' in res.text


def test_sidebar_lists_every_nav_destination(signed_in):
    res = signed_in.get("/app")
    for group in NAV:
        for item in group["pages"]:
            assert item["href"] in res.text, item


# --------------------------------------------------------------------------- #
# forms
# --------------------------------------------------------------------------- #
def test_create_a_crawl_job_and_open_it(signed_in):
    res = signed_in.post(
        "/app/crawl",
        data={
            "url": "https://example.com",
            "max_pages": "10",
            "priority": "normal",
            "obey_robots": "on",
            "stealth": "",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    location = res.headers["location"]
    assert location.startswith("/app/crawl/job_")
    job_id = location.split("/app/crawl/")[1].split("?")[0]

    detail = signed_in.get(f"/app/crawl/{job_id}")
    assert detail.status_code == 200
    assert job_id in detail.text
    assert "https://example.com" in detail.text


def test_crawl_form_reports_api_errors(signed_in):
    """A rejected URL is shown on the page instead of blowing up."""
    res = signed_in.post(
        "/app/crawl",
        data={
            "url": "javascript:alert(1)",
            "max_pages": "5",
            "priority": "normal",
            "obey_robots": "on",
        },
    )
    assert res.status_code == 200
    assert "wcp_session" in signed_in.cookies  # still signed in


def test_crawl_job_can_be_cancelled(signed_in):
    res = signed_in.post(
        "/app/crawl",
        data={
            "url": "https://example.com",
            "max_pages": "3",
            "priority": "normal",
            "obey_robots": "on",
        },
        follow_redirects=False,
    )
    job_id = res.headers["location"].split("/app/crawl/")[1].split("?")[0]
    res = signed_in.post(f"/app/crawl/{job_id}/cancel", follow_redirects=False)
    assert res.status_code == 303
    assert signed_in.get(f"/app/crawl/{job_id}").status_code == 200


def test_unknown_job_redirects_with_a_message(signed_in):
    res = signed_in.get("/app/crawl/job_does_not_exist", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/app/crawl?")


def test_api_key_is_shown_once_then_listed(signed_in):
    res = signed_in.post(
        "/app/keys",
        data={"name": "ci-pipeline", "scopes": ["read", "crawl"]},
        follow_redirects=False,
    )
    assert res.status_code == 303
    location = res.headers["location"]
    assert "issued=wcp_" in location

    page = signed_in.get(location)
    assert page.status_code == 200
    assert "ci-pipeline" in page.text


def test_webhook_subscribe_then_render(signed_in):
    res = signed_in.post(
        "/app/settings/webhooks",
        data={
            "url": "https://hooks.example.test/wcp",
            "description": "etl pipeline",
            "events": ["crawl.completed"],
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "kind=ok" in res.headers["location"]

    page = signed_in.get("/app/settings")
    assert page.status_code == 200
    assert "hooks.example.test" in page.text


def test_webhook_event_names_match_the_api_schema():
    """Copied lists drift; the console must read the enum."""
    from src.api.schemas import WebhookEvent

    assert set(webhook_events()) == {e.value for e in WebhookEvent}


def test_discovery_preview_does_not_crawl(signed_in):
    res = signed_in.post(
        "/app/discovery",
        data={
            "topic": "solar installers",
            "count": "3",
            "max_pages_per_site": "5",
            "preview": "1",
        },
    )
    assert res.status_code == 200
    assert "solar installers" in res.text


def test_discovery_job_can_be_created_and_cancelled(signed_in):
    res = signed_in.post(
        "/app/discovery",
        data={
            "topic": "solar installers",
            "count": "3",
            "max_pages_per_site": "5",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    job_id = res.headers["location"].split("/app/discovery/")[1].split("?")[0]

    assert signed_in.get(f"/app/discovery/{job_id}").status_code == 200

    res = signed_in.post(f"/app/discovery/{job_id}/cancel", follow_redirects=False)
    assert res.status_code == 303
    assert "kind=ok" in res.headers["location"]


def test_discovery_reject_without_a_selection_is_refused(signed_in):
    res = signed_in.post(
        "/app/discovery",
        data={
            "topic": "solar installers",
            "count": "3",
            "max_pages_per_site": "5",
        },
        follow_redirects=False,
    )
    job_id = res.headers["location"].split("/app/discovery/")[1].split("?")[0]

    res = signed_in.post(
        f"/app/discovery/{job_id}/reject", data={}, follow_redirects=False
    )
    assert res.status_code == 303
    assert "kind=err" in res.headers["location"]


def test_marketplace_listing_needs_a_hashed_job(signed_in):
    """The API refuses an unhashed listing; the console must show why."""
    res = signed_in.post(
        "/app/marketplace/datasets",
        data={
            "title": "UK solar installers",
            "description": "Clean installer list with addresses and phone numbers",
            "tags": "energy,uk",
            "price_usd": "49",
            "license": "CC-BY-4.0",
            "source_job_id": "job_nope",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "kind=err" in res.headers["location"]
    page = signed_in.get("/app/marketplace")
    assert page.status_code == 200


# --------------------------------------------------------------------------- #
# degradation
# --------------------------------------------------------------------------- #
def test_console_is_disabled_without_jinja2(monkeypatch):
    """A minimal deployment without jinja2 must still boot the API."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "jinja2":
            raise ImportError("no jinja2 here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    app = create_api_app(
        fakeredis.FakeStrictRedis(decode_responses=True), enable_graphql=False
    )
    client = TestClient(app)
    # the JSON index is back on "/", and the API is completely unaffected
    body = client.get("/").json()
    assert body["service"] == "WebCrawlerPro Public API"
    assert client.get("/healthz").status_code == 200


def test_console_can_be_switched_off_by_env(monkeypatch):
    monkeypatch.setenv("WCP_WEBUI", "0")
    app = create_api_app(
        fakeredis.FakeStrictRedis(decode_responses=True), enable_graphql=False
    )
    assert app.state.webui is False
    assert TestClient(app).get("/").json()["service"] == "WebCrawlerPro Public API"


def test_bare_api_deployment_has_no_console_routes(client):
    """With the console mounted, '/' is a page, not the JSON index."""
    res = client.get("/")
    assert res.headers["content-type"].startswith("text/html")


def test_pricing_page_lists_every_plan(client):
    res = client.get("/pricing")
    for plan in PLANS:
        assert plan["name"] in res.text


def test_poll_proxy_is_authenticated(client):
    """The browser's cookie cannot reach /api/v1, and the proxy must not be
    an open door either."""
    assert client.get("/app/poll/jobs/job_1").status_code == 401


def test_poll_proxy_returns_job_state(signed_in):
    res = signed_in.post(
        "/app/crawl",
        data={
            "url": "https://example.com",
            "max_pages": "2",
            "priority": "normal",
            "obey_robots": "on",
        },
        follow_redirects=False,
    )
    job_id = res.headers["location"].split("/app/crawl/")[1].split("?")[0]

    poll = signed_in.get(f"/app/poll/jobs/{job_id}")
    assert poll.status_code == 200
    assert poll.json()["job_id"] == job_id

    # the discovery reader works the same way
    res = signed_in.post(
        "/app/discovery",
        data={
            "topic": "solar installers",
            "count": "2",
            "max_pages_per_site": "2",
        },
        follow_redirects=False,
    )
    did = res.headers["location"].split("/app/discovery/")[1].split("?")[0]
    assert signed_in.get(f"/app/poll/discovery/{did}").status_code == 200
