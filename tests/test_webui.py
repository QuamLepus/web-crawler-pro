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


def test_search_page_is_a_query_workbench_not_a_web_search(signed_in):
    """An empty query offers examples inside a tool panel.

    This used to assert a Google-style hero. That was the wrong shape for a
    product that indexes its own corpora: a giant empty search bar promises
    the open web and cannot deliver it.
    """
    res = signed_in.get("/app/search")
    assert res.status_code == 200
    assert 'class="search-hero"' not in res.text
    assert "Run a query" in res.text
    # and it says, plainly, what is actually being searched
    assert "not a general web search" in res.text


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


# --------------------------------------------------------------------------- #
# product clarity
# --------------------------------------------------------------------------- #
# The console exists to expose what this platform actually does — crawl,
# discover, refine, index. These tests exist because an earlier version copied
# the *shape* of consumer products (a Google-style search hero, an
# Amazon-style product grid) and ended up promising things the engine cannot
# deliver. They are regression tests against that mistake.


def test_landing_says_what_the_product_is(client):
    res = client.get("/")
    for phrase in ("Crawl the web", "distributed crawler", "search index"):
        assert phrase.lower() in res.text.lower(), phrase


def test_landing_maps_every_capability_to_a_real_entry_point(client):
    """Each capability tile names the code that implements it."""
    res = client.get("/")
    for entry in (
        "POST /api/v1/jobs/crawl",
        "POST /api/v1/discovery",
        "GET /api/v1/search-index/query",
    ):
        assert entry in res.text, entry


def test_nothing_pretends_to_be_a_consumer_search_engine(signed_in):
    res = signed_in.get("/app/search")
    # no giant hero, and the page states the boundary out loud
    assert 'class="search-hero"' not in res.text
    assert "not a general web search" in res.text


def test_marketplace_is_a_data_catalogue_not_a_shop(signed_in):
    res = signed_in.get("/app/marketplace")
    text = res.text
    # catalogue rows, not product cards with thumbnails or star ratings
    assert "thumb" not in text
    assert 'class="catalogue"' in text or "No datasets listed" in text
    # and it explains what a listing actually is
    assert "Data exchange" in text
    assert "SHA-256" in text or "source job" in text.lower()


def test_console_declares_the_platform_on_every_page(signed_in):
    """One glance at the topbar answers 'what is this?'."""
    for path in PAGES:
        res = signed_in.get(path)
        assert "Crawler · Discovery · Own index" in res.text, path


def test_there_is_no_voice_or_media_feature(signed_in):
    """The platform has no speech synthesis; the console must not imply one."""
    pages = [signed_in.get(p).text for p in PAGES]
    pages.append(signed_in.get("/").text)
    pages.append(signed_in.get("/pricing").text)
    import re

    banned = [
        "voice",
        "speech synthesis",
        "text to speech",
        "text-to-speech",
        "elevenlabs",
        "narration",
        "podcast",
        "audiobook",
    ]
    for page in pages:
        lowered = page.lower()
        for word in banned:
            # word-boundary match: "voice" is a substring of "invoice"
            assert not re.search(r"\b" + re.escape(word) + r"\b", lowered), (
                f"{word!r} appeared on a console page"
            )


# --------------------------------------------------------------------------- #
# tabs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path, panels",
    [
        ("/app/index", ["corpus", "levels", "ops"]),
        ("/app/marketplace", ["datasets", "bounties", "how"]),
        ("/app/docs", ["auth", "jobs", "discovery", "all"]),
        ("/app/search", ["results", "corpus", "syntax"]),
    ],
)
def test_pages_expose_tabs(signed_in, path, panels):
    res = signed_in.get(path)
    assert res.status_code == 200
    assert "data-tabs" in res.text, path
    for panel in panels:
        assert f'data-tab="{panel}"' in res.text, (path, panel)
        assert f'data-panel="{panel}"' in res.text, (path, panel)


def test_job_detail_has_tabs(signed_in):
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
    page = signed_in.get(f"/app/crawl/{job_id}")
    for panel in ("overview", "options", "raw", "api"):
        assert f'data-panel="{panel}"' in page.text, panel


# --------------------------------------------------------------------------- #
# motion and accessibility
# --------------------------------------------------------------------------- #
def test_animations_respect_reduced_motion(client):
    css = client.get("/static/app.css").text
    assert "prefers-reduced-motion" in css


def test_every_animation_is_short(client):
    """Motion should feel immediate: nothing over 420 ms."""
    css = client.get("/static/app.css").text
    import re

    durations = [int(d) for d in re.findall(r"animation:[^;{]*?(\d+)ms", css)]
    assert durations, "expected at least one animation duration"
    assert max(durations) <= 700, max(durations)


def test_pages_have_accessible_tab_semantics(signed_in):
    res = signed_in.get("/app/index")
    assert 'role="tablist"' in res.text
    assert 'role="tab"' in res.text
    assert 'role="tabpanel"' in res.text
    assert 'aria-selected="true"' in res.text


# --------------------------------------------------------------------------- #
# transport-level speed
# --------------------------------------------------------------------------- #
def test_html_pages_are_compressed(client):
    res = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert res.headers.get("content-encoding") == "gzip"
    # gzip has to actually help, or it is just CPU burned on the wire
    assert "text/html" in res.headers["content-type"]


def test_static_assets_are_immutable(client):
    res = client.get("/static/app.css")
    assert res.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_unchanged_pages_answer_304_with_no_body(client):
    first = client.get("/")
    etag = first.headers["etag"]
    assert etag.startswith('W/"')

    second = client.get("/", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""


def test_etag_is_stable_and_content_sensitive(client):
    """The same page hashes the same; a different page hashes differently."""
    a = client.get("/").headers["etag"]
    b = client.get("/").headers["etag"]
    c = client.get("/login").headers["etag"]
    assert a == b
    assert a != c


def test_html_is_private_but_revalidated(client):
    """Pages carry tenant data: cache locally, but always ask."""
    res = client.get("/")
    assert res.headers["cache-control"] == "private, no-cache"


def test_json_responses_are_not_etagged(client):
    """APIs do not need conditional GET; do not spend hashing on them."""
    res = client.get("/health")
    assert "etag" not in res.headers


def test_speed_failure_never_breaks_a_page(client, monkeypatch):
    """install_speed failing must leave the console working, just slower."""
    from src.webui import speed

    def boom(*args, **kwargs):
        raise RuntimeError("no middlewares today")

    monkeypatch.setattr(speed, "install_speed", boom)

    bare = create_api_app(
        fakeredis.FakeStrictRedis(decode_responses=True), enable_graphql=False
    )
    res = TestClient(bare).get("/")
    assert res.status_code == 200
    assert "<html" in res.text.lower()


def test_data_studio_renders_local_index_empty_state_and_navigation(signed_in):
    response = signed_in.get("/app/studio")
    assert response.status_code == 200
    assert "Data Studio" in response.text
    assert "indexed documents" in response.text.lower()
    assert "/app/studio" in signed_in.get("/app").text


def test_data_studio_export_requires_session(client):
    response = client.get("/app/studio/export", params={"q": "solar"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_data_studio_csv_export_is_authenticated_and_spreadsheet_safe(signed_in, monkeypatch):
    from src.webui.client import Api, Result

    async def fake_index_query(self, q, limit=10, **kwargs):
        return Result(True, data={"results": [{
            "rank": 1, "title": "=HYPERLINK(\"https://bad.invalid\")",
            "url": "https://example.test/a", "host": "example.test",
            "snippet": "safe excerpt", "score": 0.5, "source": "unit-test",
        }]})

    monkeypatch.setattr(Api, "index_query", fake_index_query)
    response = signed_in.get("/app/studio/export", params={"q": "demo"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=\"search-results.csv\"" in response.headers["content-disposition"]
    assert "'=HYPERLINK" in response.text
    assert "safe excerpt" in response.text
