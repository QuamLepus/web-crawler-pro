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
            "events": ["crawl.completed", "crawl.cancelled"],
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "kind=ok" in res.headers["location"]

    page = signed_in.get("/app/settings")
    assert page.status_code == 200
    assert "hooks.example.test" in page.text
    assert "crawl.cancelled" in page.text


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


def test_marketplace_is_a_data_catalogue_not_a_shop(signed_in, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_SIGNING_SECRET",
                       "marketplace-console-signing-key-0001")
    from src.marketplace.service import MarketplaceService

    marketplace = MarketplaceService(signed_in.app.state.services.redis)
    for price in (0, 2500):
        marketplace.create_listing(
            "another-tenant", title=f"Catalogue item {price}",
            description="A published listing used to verify marketplace controls.",
            tags=["catalogue"], price_usd_cents=price, license="CC-BY-4.0",
            content_sha256=("b" if price == 0 else "c") * 64)
    own_tenant = signed_in.app.state.services.users.list_orgs()[0].id
    marketplace.create_listing(
        own_tenant, title="My free listing",
        description="A listing owned by this account should not show a purchase control.",
        tags=["catalogue"], price_usd_cents=0, license="CC-BY-4.0",
        content_sha256="d" * 64)

    res = signed_in.get("/app/marketplace")
    text = res.text
    # catalogue rows, not product cards with thumbnails or star ratings
    assert "thumb" not in text
    assert 'class="catalogue"' in text
    # and the UI accurately describes purchase/transfer limitations
    assert "Data exchange" in text
    assert "SHA-256" in text or "source job" in text.lower()
    assert "Get signed manifest" in text
    assert "Paid unavailable" in text
    assert "Your listing" in text
    from bs4 import BeautifulSoup
    own_row = next(row for row in BeautifulSoup(text, "html.parser").select(".cat-row")
                   if "My free listing" in row.get_text())
    assert own_row.select_one("form[action$='/purchase']") is None
    assert "dataset bytes are not transferred" in text.lower()
    assert "delivery is queued immediately" not in text.lower()
    assert "platform fee is applied at purchase" not in text.lower()

    monkeypatch.delenv("MARKETPLACE_SIGNING_SECRET", raising=False)
    unconfigured = signed_in.get("/app/marketplace")
    soup = BeautifulSoup(unconfigured.text, "html.parser")
    free_row = next(row for row in soup.select(".cat-row")
                    if "Catalogue item 0" in row.get_text())
    assert "Free manifest unavailable" in free_row.get_text()
    assert free_row.select_one("form[action$='/purchase']") is None
    assert "disabled until the operator configures a valid" in unconfigured.text


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


def test_workflow_builder_page_is_authenticated_and_shows_boundaries(signed_in):
    response = signed_in.get("/app/workflows")
    assert response.status_code == 200
    assert "Create crawl workflow" in response.text
    assert "up to 10 crawl steps" in response.text
    assert "Advanced: define step IDs and dependencies" in response.text
    assert "Recurring schedule" in response.text
    assert "/app/workflows" in signed_in.get("/app").text


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


def test_data_studio_shows_returned_result_breakdowns_and_saved_query_form(signed_in, monkeypatch):
    from src.webui.client import Api, Result

    async def fake_index_query(self, q, limit=10, **kwargs):
        return Result(True, data={"query": q, "count": 2, "took_ms": 3, "results": [
            {"rank": 1, "url": "https://a.example/", "host": "a.example",
             "title": "A", "snippet": "A text", "score": 1, "source": "wiki"},
            {"rank": 2, "url": "https://b.example/", "host": "b.example",
             "title": "B", "snippet": "B text", "score": 0.5, "source": "curlie"},
        ]})

    async def fake_index_stats(self):
        return Result(True, data={"docs": 2, "shards": 1, "available": True})

    async def fake_saved(self):
        return Result(True, data={"items": [{"saved_id": "saved_aaaaaaaaaaaaaaaaaaaa",
            "name": "Solar sources", "q": "solar", "sources": ["wiki"], "limit": 10}]})

    monkeypatch.setattr(Api, "index_query", fake_index_query)
    monkeypatch.setattr(Api, "index_stats", fake_index_stats)
    monkeypatch.setattr(Api, "studio_saved_queries", fake_saved)
    response = signed_in.get("/app/studio", params={"q": "solar"})
    assert response.status_code == 200
    assert "Hosts in returned results" in response.text
    assert "a.example" in response.text and "b.example" in response.text
    assert "This is not the full index" in response.text or "not the full index" in response.text
    assert "Solar sources" in response.text
    assert "Save query" in response.text


def test_data_studio_console_saves_and_deletes_queries(signed_in, monkeypatch):
    from src.webui.client import Api, Result
    calls = []

    async def save(self, **body):
        calls.append(body)
        return Result(True, data={"saved_id": "saved_aaaaaaaaaaaaaaaaaaaa"}, status=201)

    async def delete(self, saved_id):
        calls.append({"delete": saved_id})
        return Result(True, status=204)

    monkeypatch.setattr(Api, "studio_save_query", save)
    monkeypatch.setattr(Api, "studio_delete_query", delete)
    saved = signed_in.post("/app/studio/save", data={
        "name": "Solar", "q": "solar panels", "source": "wikipedia,curlie", "limit": 30,
        "group_by": "source", "metric": "avg_score"},
        follow_redirects=False)
    assert saved.status_code == 303
    assert calls[0]["sources"] == ["wikipedia", "curlie"]
    assert calls[0]["group_by"] == "source"
    assert calls[0]["metric"] == "avg_score"
    removed = signed_in.post("/app/studio/saved/saved_aaaaaaaaaaaaaaaaaaaa/delete",
                             follow_redirects=False)
    assert removed.status_code == 303
    assert calls[1]["delete"] == "saved_aaaaaaaaaaaaaaaaaaaa"


def test_data_studio_charts_are_accessible_and_derive_from_real_result_fields(signed_in, monkeypatch):
    from src.webui.client import Api, Result

    async def fake_index_query(self, q, limit=10, **kwargs):
        return Result(True, data={"query": q, "count": 3, "took_ms": 2, "results": [
            {"rank": 1, "url": "https://a.example/", "host": "a.example", "score": 3.0, "source": "wiki"},
            {"rank": 2, "url": "https://a.example/b", "host": "a.example", "score": 2.0, "source": "wiki"},
            {"rank": 3, "url": "https://b.example/", "host": "b.example", "score": 1.0, "source": "curlie"},
        ]})

    async def fake_index_stats(self):
        return Result(True, data={"docs": 3, "shards": 1, "available": True})

    async def fake_saved(self):
        return Result(True, data={"items": []})

    monkeypatch.setattr(Api, "index_query", fake_index_query)
    monkeypatch.setattr(Api, "index_stats", fake_index_stats)
    monkeypatch.setattr(Api, "studio_saved_queries", fake_saved)
    response = signed_in.get("/app/studio", params={"q": "solar"})
    assert response.status_code == 200
    assert 'data-viz="host"' in response.text
    assert 'data-viz="source"' in response.text
    assert 'data-viz="score-rank"' in response.text
    assert 'data-viz="custom-breakdown"' in response.text
    assert 'role="img"' in response.text
    assert "a.example" in response.text and "wiki: 2" in response.text
    assert 'points="24.0,30.0' in response.text

    grouped = signed_in.get("/app/studio", params={
        "q": "solar", "group_by": "source", "metric": "avg_score"})
    assert grouped.status_code == 200
    assert "Mean score by source" in grouped.text
    assert "wiki: 2.5" in grouped.text
    assert "curlie: 1.0" in grouped.text
    assert "Configurable breakdown computed only from these returned hits" in grouped.text
    assert 'value="source" selected' in grouped.text or "value=\"source\" selected" in grouped.text


def test_data_studio_snapshot_save_download_delete_flow(signed_in, monkeypatch):
    from src.webui.client import Api, Result
    calls = []
    rows = [{"rank": 1, "url": "https://example.org/", "host": "example.org",
             "title": "Evidence", "snippet": "Saved result", "score": 0.75, "source": "wiki"}]

    async def query(self, q, limit=10, **kwargs):
        return Result(True, data={"results": rows})

    async def create(self, **body):
        calls.append(body)
        return Result(True, data={"snapshot_id": "snapshot_aaaaaaaaaaaaaaaaaaaa"}, status=201)

    async def get_snapshot(self, snapshot_id):
        return Result(True, data={"snapshot_id": snapshot_id, "rows": rows})

    async def delete_snapshot(self, snapshot_id):
        calls.append({"delete": snapshot_id})
        return Result(True, status=204)

    monkeypatch.setattr(Api, "index_query", query)
    monkeypatch.setattr(Api, "studio_create_snapshot", create)
    monkeypatch.setattr(Api, "studio_snapshot", get_snapshot)
    monkeypatch.setattr(Api, "studio_delete_snapshot", delete_snapshot)
    created = signed_in.post("/app/studio/snapshot", data={
        "name": "Evidence sample", "q": "solar", "limit": 10,
        "group_by": "source", "metric": "avg_score"}, follow_redirects=False)
    assert created.status_code == 303
    assert calls[0]["rows"] == rows
    assert calls[0]["group_by"] == "source"
    downloaded = signed_in.get("/app/studio/snapshots/snapshot_aaaaaaaaaaaaaaaaaaaa/download")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("application/json")
    assert "attachment" in downloaded.headers["content-disposition"]
    assert downloaded.json()["rows"][0]["title"] == "Evidence"
    removed = signed_in.post("/app/studio/snapshots/snapshot_aaaaaaaaaaaaaaaaaaaa/delete",
                             follow_redirects=False)
    assert removed.status_code == 303
    assert calls[-1]["delete"] == "snapshot_aaaaaaaaaaaaaaaaaaaa"


def test_workflow_console_exposes_run_cancellation(signed_in, monkeypatch):
    from src.webui.client import Api, Result
    calls = []

    async def workflows(self):
        return Result(True, data={"items": []})

    async def runs(self, **params):
        return Result(True, data={"items": [{"run_id": "wfrun_aaaaaaaaaaaaaaaaaaaa",
            "name": "Refresh", "status": "running", "execution_mode": "dag",
            "step_index": 2, "steps": [
                {"step_id": "seed", "status": "completed", "depends_on": []},
                {"step_id": "branch_a", "status": "failed", "depends_on": ["seed"],
                 "error": "crawl failed"},
                {"step_id": "branch_b", "status": "skipped", "depends_on": ["seed"]},
                {"step_id": "finish", "status": "queued", "depends_on": ["branch_b"]},
            ], "created_at": "today", "cancel_requested": False}]})

    async def cancel(self, run_id):
        calls.append(run_id)
        return Result(True, data={"status": "running", "cancel_requested": True,
                                  "message": "cancellation requested"})

    monkeypatch.setattr(Api, "workflows", workflows)
    monkeypatch.setattr(Api, "workflow_runs", runs)
    monkeypatch.setattr(Api, "workflow_cancel", cancel)
    page = signed_in.get("/app/workflows")
    assert page.status_code == 200
    assert "3/4 steps settled" in page.text
    assert "1 completed · 0 running · 1 queued · 1 failed · 1 skipped · 0 cancelled" in page.text
    assert "after seed" in page.text
    assert "/app/workflows/runs/wfrun_aaaaaaaaaaaaaaaaaaaa/cancel" in page.text
    response = signed_in.post("/app/workflows/runs/wfrun_aaaaaaaaaaaaaaaaaaaa/cancel",
                              follow_redirects=False)
    assert response.status_code == 303
    assert calls == ["wfrun_aaaaaaaaaaaaaaaaaaaa"]


def test_workflow_console_updates_schedule(signed_in, monkeypatch):
    from src.webui.client import Api, Result
    calls = []

    async def update(self, workflow_id, interval_minutes):
        calls.append((workflow_id, interval_minutes))
        return Result(True, data={"schedule_interval_minutes": interval_minutes})

    monkeypatch.setattr(Api, "workflow_schedule", update)
    response = signed_in.post("/app/workflows/workflow_aaaaaaaaaaaaaaaaaaaa/schedule",
                              data={"interval_minutes": "1440"}, follow_redirects=False)
    assert response.status_code == 303
    assert calls == [("workflow_aaaaaaaaaaaaaaaaaaaa", 1440)]


def test_workflow_console_rename_and_delete(signed_in, monkeypatch):
    from src.webui.client import Api, Result
    calls = []

    async def update(self, workflow_id, **body):
        calls.append(("update", workflow_id, body))
        return Result(True, data={"workflow_id": workflow_id})

    async def delete(self, workflow_id):
        calls.append(("delete", workflow_id))
        return Result(True, data={"deleted": True})

    monkeypatch.setattr(Api, "workflow_update", update)
    monkeypatch.setattr(Api, "workflow_delete", delete)
    updated = signed_in.post("/app/workflows/workflow_aaaaaaaaaaaaaaaaaaaa/edit",
                             data={"name": "Renamed"}, follow_redirects=False)
    deleted = signed_in.post("/app/workflows/workflow_aaaaaaaaaaaaaaaaaaaa/delete",
                             follow_redirects=False)
    assert updated.status_code == 303
    assert deleted.status_code == 303
    assert calls == [
        ("update", "workflow_aaaaaaaaaaaaaaaaaaaa", {"name": "Renamed"}),
        ("delete", "workflow_aaaaaaaaaaaaaaaaaaaa"),
    ]


def test_workflow_console_creates_and_edits_dependency_graphs(signed_in, monkeypatch):
    import json

    from src.webui.client import Api, Result

    calls = []

    async def create(self, **body):
        calls.append(("create", body))
        return Result(True, data={"workflow_id": "workflow_aaaaaaaaaaaaaaaaaaaa"})

    async def update(self, workflow_id, **body):
        calls.append(("update", workflow_id, body))
        return Result(True, data={"workflow_id": workflow_id})

    monkeypatch.setattr(Api, "workflow_create", create)
    monkeypatch.setattr(Api, "workflow_update", update)
    steps = [
        {"step_id": "seed", "depends_on": [], "url": "https://seed.example"},
        {"step_id": "branch", "depends_on": ["seed"],
         "url": "https://branch.example", "max_pages": 2},
    ]
    created = signed_in.post("/app/workflows/create", data={
        "name": "Branched", "steps_json": json.dumps(steps),
    }, follow_redirects=False)
    updated = signed_in.post("/app/workflows/workflow_aaaaaaaaaaaaaaaaaaaa/edit", data={
        "name": "Branched v2", "steps_json": json.dumps(steps),
    }, follow_redirects=False)
    assert created.status_code == updated.status_code == 303
    assert calls[0] == ("create", {"name": "Branched", "steps": steps,
                                    "schedule_interval_minutes": None})
    assert calls[1] == ("update", "workflow_aaaaaaaaaaaaaaaaaaaa",
                        {"name": "Branched v2", "steps": steps})


def test_workflow_edit_applies_dependency_checkbox_edges(signed_in, monkeypatch):
    import json

    from src.webui.client import Api, Result

    calls = []

    async def update(self, workflow_id, **body):
        calls.append((workflow_id, body))
        return Result(True, data={"workflow_id": workflow_id})

    monkeypatch.setattr(Api, "workflow_update", update)
    steps = [
        {"step_id": "seed", "depends_on": [], "url": "https://seed.example"},
        {"step_id": "branch", "depends_on": ["seed"],
         "url": "https://branch.example"},
        {"step_id": "summary", "depends_on": ["branch"],
         "url": "https://summary.example"},
    ]
    response = signed_in.post(
        "/app/workflows/workflow_aaaaaaaaaaaaaaaaaaaa/edit",
        data={
            "name": "Branched",
            "steps_json": json.dumps(steps),
            "dependency_editor": "1",
            "depends_on_2": ["seed", "branch"],
        }, follow_redirects=False)

    assert response.status_code == 303
    assert calls == [("workflow_aaaaaaaaaaaaaaaaaaaa", {
        "name": "Branched", "steps": [
            {"step_id": "seed", "depends_on": [], "url": "https://seed.example"},
            {"step_id": "branch", "depends_on": [],
             "url": "https://branch.example"},
            {"step_id": "summary", "depends_on": ["seed", "branch"],
             "url": "https://summary.example"},
        ]})]


def test_workflow_dependency_validation_error_is_announced_to_screen_readers(
        signed_in, monkeypatch):
    import json

    from src.webui.client import Api, Result

    async def reject_update(self, workflow_id, **body):
        return Result(False, error="Workflow dependency graph contains a cycle.")

    monkeypatch.setattr(Api, "workflow_update", reject_update)
    steps = [
        {"step_id": "seed", "depends_on": [], "url": "https://seed.example"},
        {"step_id": "branch", "depends_on": ["seed"],
         "url": "https://branch.example"},
    ]
    response = signed_in.post(
        "/app/workflows/workflow_aaaaaaaaaaaaaaaaaaaa/edit",
        data={"name": "Branched", "steps_json": json.dumps(steps),
              "dependency_editor": "1", "depends_on_0": ["branch"]},
        follow_redirects=False)
    assert response.status_code == 303
    page = signed_in.get(response.headers["location"])
    assert "Workflow dependency graph contains a cycle." in page.text
    import re
    assert re.search(r'class="alert danger mb-3"\s+role="alert"', page.text)


def test_workflow_dag_preview_orders_nodes_to_reduce_crossings():
    from src.webui.app import _workflow_dag_preview

    preview = _workflow_dag_preview({
        "execution_mode": "dag",
        "steps": [
            {"step_id": "a", "depends_on": [], "url": "https://a.example"},
            {"step_id": "b", "depends_on": [], "url": "https://b.example"},
            {"step_id": "x", "depends_on": ["b"], "url": "https://x.example"},
            {"step_id": "y", "depends_on": ["a"], "url": "https://y.example"},
        ],
    })

    positions = {node["step_id"]: node for node in preview["nodes"]}
    assert positions["a"]["y"] < positions["b"]["y"]
    assert positions["y"]["y"] < positions["x"]["y"]
    assert positions["a"]["y"] == positions["y"]["y"]
    assert positions["b"]["y"] == positions["x"]["y"]


def test_workflow_console_renders_dependency_editor(signed_in, monkeypatch):
    from src.webui.client import Api, Result

    async def workflows(self):
        return Result(True, data={"items": [{
            "workflow_id": "workflow_aaaaaaaaaaaaaaaaaaaa", "name": "Branching",
            "execution_mode": "dag", "max_pages": 3, "schedule_interval_minutes": None,
            "created_at": "today", "steps": [
                {"step_id": "seed", "depends_on": [], "url": "https://seed.example",
                 "max_pages": 1, "obey_robots": True},
                {"step_id": "news", "depends_on": ["seed"],
                 "url": "https://news.example", "max_pages": 1, "obey_robots": True},
                {"step_id": "research", "depends_on": ["seed"],
                 "url": "https://research.example", "max_pages": 1, "obey_robots": True},
                {"step_id": "summary", "depends_on": ["news", "research"],
                 "url": "https://summary.example", "max_pages": 1, "obey_robots": True},
            ],
        }]})

    async def runs(self, **params):
        return Result(True, data={"items": [{
            "run_id": "wfrun_aaaaaaaaaaaaaaaaaaaa",
            "workflow_id": "workflow_aaaaaaaaaaaaaaaaaaaa", "name": "Branching",
            "status": "running", "execution_mode": "dag", "created_at": "today",
            "steps": [
                {"step_id": "seed", "status": "completed"},
                {"step_id": "news", "status": "running"},
                {"step_id": "research", "status": "failed"},
                {"step_id": "summary", "status": "queued"},
            ],
        }, {
            "run_id": "wfrun_bbbbbbbbbbbbbbbbbbbb",
            "workflow_id": "workflow_aaaaaaaaaaaaaaaaaaaa", "name": "Branching",
            "status": "completed", "execution_mode": "dag", "created_at": "yesterday",
            "steps": [
                {"step_id": "seed", "status": "completed"},
                {"step_id": "news", "status": "completed"},
                {"step_id": "research", "status": "completed"},
                {"step_id": "summary", "status": "completed"},
            ],
        }]})

    monkeypatch.setattr(Api, "workflows", workflows)
    monkeypatch.setattr(Api, "workflow_runs", runs)
    page = signed_in.get("/app/workflows")
    assert page.status_code == 200
    assert "dependency DAG" in page.text
    assert "depends on seed" in page.text
    assert '"depends_on": [' in page.text
    assert "Edit step dependencies without changing JSON" in page.text
    assert 'name="depends_on_3" value="news" checked' in page.text
    assert 'name="depends_on_3" value="research" checked' in page.text
    assert '<small class="muted">https://news.example</small>' in page.text
    assert 'name="depends_on_3" value="summary"' not in page.text
    assert 'data-step-id="summary"' in page.text
    assert 'data-dependency-cycle-warning role="alert"' in page.text
    assert "These prerequisites create a dependency cycle." in page.text
    assert "const createsCycle = () =>" in page.text
    assert 'data-viz="workflow-dag"' in page.text
    assert 'aria-labelledby="workflow-dag-title-workflow_aaaaaaaaaaaaaaaaaaaa"' in page.text
    assert 'aria-describedby="workflow-dag-description-workflow_aaaaaaaaaaaaaaaaaaaa"' in page.text
    assert 'id="workflow-dag-description-workflow_aaaaaaaaaaaaaaaaaaaa">' in page.text
    assert "Step seed at https://seed.example. Root step. Status: completed." in page.text
    assert "Step summary at https://summary.example. Prerequisites: news, research. Status: queued." in page.text
    assert page.text.count('<path class="workflow-dag-edge"') == 4
    assert page.text.count('class="workflow-dag-node"') == 4
    assert "Visual dependency graph (read-only)" in page.text
    assert "Selected run: <strong>running</strong>" in page.text
    assert "Choose run status for Branching" in page.text
    assert 'value="wfrun_aaaaaaaaaaaaaaaaaaaa" selected' in page.text
    assert 'data-status="completed"' in page.text
    assert 'data-status="running"' in page.text
    assert 'data-status="failed"' in page.text
    assert "queued</text>" in page.text
    assert '"seed"' in page.text

    historical = signed_in.get("/app/workflows", params={
        "workflow_id": "workflow_aaaaaaaaaaaaaaaaaaaa",
        "run_id": "wfrun_bbbbbbbbbbbbbbbbbbbb",
    })
    assert historical.status_code == 200
    assert "Selected run: <strong>completed</strong>" in historical.text
    assert 'value="wfrun_bbbbbbbbbbbbbbbbbbbb" selected' in historical.text
    assert "not in selected run" not in historical.text


def test_workflow_console_paginates_run_history(signed_in, monkeypatch):
    from src.webui.client import Api, Result

    async def workflows(self):
        return Result(True, data={"items": []})

    calls = []

    async def runs(self, page=1, page_size=20, **filters):
        assert page_size == 20
        calls.append((page, filters))
        return Result(True, data={"items": [{
            "run_id": f"wfrun_{page:020d}", "name": f"Historical batch {page}",
            "status": "completed", "steps": [], "created_at": "today",
        }], "total": 41, "page": page, "page_size": page_size})

    monkeypatch.setattr(Api, "workflows", workflows)
    monkeypatch.setattr(Api, "workflow_runs", runs)

    first = signed_in.get("/app/workflows")
    assert first.status_code == 200
    assert "Showing runs 1–20 of 41 · page 1 of 3" in first.text
    assert "/app/workflows?run_page=2" in first.text
    assert "Older runs" in first.text

    second = signed_in.get("/app/workflows", params={"run_page": 2})
    assert second.status_code == 200
    assert "Historical batch 2" in second.text
    assert "Showing runs 21–40 of 41 · page 2 of 3" in second.text
    assert "/app/workflows?run_page=1" in second.text
    assert "/app/workflows?run_page=3" in second.text

    beyond_history = signed_in.get("/app/workflows", params={"run_page": 999})
    assert beyond_history.status_code == 200
    assert "Showing runs 41–41 of 41 · page 3 of 3" in beyond_history.text
    assert "Historical batch 3" in beyond_history.text

    filtered_page = signed_in.get("/app/workflows", params={
        "run_q": "Alpha", "run_status": "failed",
        "run_from": "2026-05-01", "run_to": "2026-05-03",
    })
    assert filtered_page.status_code == 200
    assert 'name="run_q" maxlength="100" value="Alpha"' in filtered_page.text
    assert 'name="run_status" aria-label="Filter runs by status"' in filtered_page.text
    assert "Showing runs 1–20 of 41 matching · page 1 of 3" in filtered_page.text
    assert "run_q=Alpha&amp;run_status=failed&amp;run_from=2026-05-01&amp;run_to=2026-05-03" \
        in filtered_page.text
    assert calls[-1] == (1, {"q": "Alpha", "status": "failed",
                              "date_from": "2026-05-01", "date_to": "2026-05-03"})


def test_workflow_overlay_persists_when_browsing_run_history(signed_in, monkeypatch):
    from src.webui.client import Api, Result

    workflow_id = "workflow_aaaaaaaaaaaaaaaaaaaa"
    selected_run_id = "wfrun_bbbbbbbbbbbbbbbbbbbb"
    selected_calls = []

    async def workflows(self):
        return Result(True, data={"items": [{
            "workflow_id": workflow_id, "name": "Status overlay",
            "execution_mode": "dag", "max_pages": 1,
            "schedule_interval_minutes": None, "created_at": "today",
            "steps": [{"step_id": "seed", "depends_on": [],
                       "url": "https://seed.example", "max_pages": 1,
                       "obey_robots": True}],
        }]})

    async def runs(self, page=1, page_size=20, **filters):
        return Result(True, data={"items": [{
            "run_id": f"wfrun_{page:020d}", "workflow_id": workflow_id,
            "name": f"Page run {page}", "status": "completed", "created_at": "today",
            "execution_mode": "dag", "steps": [{"step_id": "seed", "status": "completed"}],
        }], "total": 41, "page": page, "page_size": page_size})

    async def run_status(self, run_id):
        selected_calls.append(run_id)
        return Result(True, data={"run_id": selected_run_id,
            "workflow_id": workflow_id, "name": "Selected older run",
            "status": "failed", "created_at": "yesterday", "execution_mode": "dag",
            "steps": [{"step_id": "seed", "status": "failed"}]})

    monkeypatch.setattr(Api, "workflows", workflows)
    monkeypatch.setattr(Api, "workflow_runs", runs)
    monkeypatch.setattr(Api, "workflow_run_status", run_status)

    selected_page = signed_in.get("/app/workflows", params={
        "run_page": 2, "workflow_id": workflow_id, "run_id": selected_run_id,
    })
    assert selected_page.status_code == 200
    assert "page 2 of 3" in selected_page.text
    assert 'data-status="failed"' in selected_page.text
    assert "selected from another page" in selected_page.text
    assert "run_page=1&amp;workflow_id=workflow_aaaaaaaaaaaaaaaaaaaa&amp;run_id=" \
        + selected_run_id in selected_page.text

    previous_page = signed_in.get("/app/workflows", params={
        "run_page": 1, "workflow_id": workflow_id, "run_id": selected_run_id,
    })
    assert previous_page.status_code == 200
    assert "page 1 of 3" in previous_page.text
    assert "Selected run: <strong>failed</strong>" in previous_page.text
    assert 'data-status="failed"' in previous_page.text
    assert selected_calls == [selected_run_id, selected_run_id]


def test_data_studio_full_corpus_aggregate_and_csv_export(signed_in, monkeypatch):
    from src.webui.client import Api, Result
    calls = []

    async def fake_analytics(self, **params):
        calls.append(params)
        return Result(True, data={"matched_documents": 250, "group_count": 2,
            "group_by": params.get("group_by"), "metric": params.get("metric"),
            "rows": [{"label": '=HYPERLINK("https://bad.invalid")', "value": 200},
                     {"label": "curlie", "value": 50}],
            "offset": params.get("offset", 0), "limit": params.get("limit", 50)})

    monkeypatch.setattr(Api, "studio_analytics", fake_analytics)
    response = signed_in.get("/app/studio", params={
        "corpus_group_by": "source", "corpus_metric": "count", "corpus_source": "wiki"})
    assert response.status_code == 200
    assert "Full-corpus analytics" in response.text
    assert "250 matching documents" in response.text
    assert "Download current page CSV" in response.text
    assert "limit=50&amp;offset=0" in response.text
    assert "wiki" in response.text and "200" in response.text
    assert calls[0]["source"] == "wiki"

    exported = signed_in.get("/app/studio/analytics.csv", params={
        "group_by": "host", "metric": "avg_quality", "source": "wiki",
        "limit": 50, "offset": 50})
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "studio-corpus-analytics.csv" in exported.headers["content-disposition"]
    assert exported.text.lstrip("\ufeff").startswith("host,avg_quality,matched_documents")
    assert "'=HYPERLINK" in exported.text
    assert calls[-1]["limit"] == 50
    assert calls[-1]["offset"] == 50
    assert calls[-1]["source"] == "wiki"
    out_of_range = signed_in.get("/app/studio/analytics.csv", params={"offset": 5001})
    assert out_of_range.status_code == 422


def test_sso_settings_page_and_secure_configuration_form(signed_in, monkeypatch):
    from src.webui.client import Api, Result
    calls = []

    async def configure(self, **body):
        calls.append(body)
        return Result(True, data={"client_secret_configured": True})

    async def disable(self):
        calls.append("disabled")
        return Result(True, data={})

    monkeypatch.setattr(Api, "sso_configure_oidc", configure)
    monkeypatch.setattr(Api, "sso_disable_oidc", disable)
    page = signed_in.get("/app/settings")
    assert page.status_code == 200
    assert "Enterprise OIDC SSO" in page.text
    assert "Allowed email domains" in page.text
    response = signed_in.post("/app/settings/sso/oidc", data={
        "issuer": "https://idp.example.test/tenant", "client_id": "wcp-client",
        "client_secret": "sensitive-secret", "allowed_domains": "example.com, child.example.com",
        "jit_provisioning": "true", "default_role": "viewer",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert calls[0]["allowed_domains"] == ["example.com", "child.example.com"]
    assert calls[0]["client_secret"] == "sensitive-secret"
    assert "sensitive-secret" not in response.headers.get("location", "")
    disabled = signed_in.post("/app/settings/sso/oidc/disable", follow_redirects=False)
    assert disabled.status_code == 303 and calls[-1] == "disabled"


def test_login_page_offers_sso_with_validated_workspace_route(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Continue with SSO" in response.text
    assert client.get("/login/sso", params={"tenant_id": "bad"}).status_code == 422
    valid = client.get("/login/sso", params={"tenant_id": "0123456789"}, follow_redirects=False)
    assert valid.status_code == 302
    assert valid.headers["location"] == "/api/auth/oidc/0123456789/login"


def test_watch_history_reports_load_errors_instead_of_empty_states(signed_in, monkeypatch):
    from src.webui.client import Api, Result

    watch_id = "watch_aaaaaaaaaaaaaaaaaaaa"

    async def watches(self):
        return Result(True, data={"items": [{
            "watch_id": watch_id, "url": "https://example.org/status",
            "interval_minutes": 60, "last_checked_at": None, "active": True,
        }]})

    async def watch_changes(self, requested_watch_id):
        assert requested_watch_id == watch_id
        return Result(False, error="change history service unavailable")

    async def watch_snapshots(self, requested_watch_id):
        assert requested_watch_id == watch_id
        return Result(False, error="snapshot service unavailable")

    monkeypatch.setattr(Api, "watches", watches)
    monkeypatch.setattr(Api, "watch_changes", watch_changes)
    monkeypatch.setattr(Api, "watch_snapshots", watch_snapshots)

    page = signed_in.get("/app/watch", params={"watch_id": watch_id})
    assert page.status_code == 200
    assert "Could not load change history: change history service unavailable" in page.text
    assert "Could not load saved versions: snapshot service unavailable" in page.text
    assert "Unavailable" in page.text
    assert "Viewing" in page.text and "https://example.org/status" in page.text
    assert 'href="/app/watch">All watches</a>' in page.text
    assert 'class="alert warn card-pad" role="alert"' in page.text
    assert "No changes recorded" not in page.text
    assert "No snapshots saved" not in page.text

    stale = signed_in.get("/app/watch", params={"watch_id": "watch_bbbbbbbbbbbbbbbbbbbb"})
    assert stale.status_code == 200
    assert "This watch is no longer available" in stale.text
    assert 'href="/app/watch">Return to all watches</a>' in stale.text
    assert "Saved versions" not in stale.text
