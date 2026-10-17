"""The console: server-rendered pages over the public API.

Run the API and open it in a browser and you get this:

    uvicorn src.api.main:app --host 0.0.0.0 --port 8000

Design rules that shaped this module:

* **No build step, no CDN.** A crawler that has to work offline should have a
  UI that works offline. One stylesheet, one script, system fonts.
* **Server-rendered.** Pages work with JavaScript switched off; the script
  only adds polling, toasts and the command palette.
* **A real API client.** Every page calls the same documented endpoints a
  customer would, so the UI cannot drift away from the API.
* **Degrade, never explode.** An unreachable scheduler shows an empty state,
  not a stack trace.
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.webui.client import Api
from src.webui.session import clear_session, current_token, set_session

logger = logging.getLogger("wcp.webui")

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

router = APIRouter(include_in_schema=False)

# --------------------------------------------------------------------------- #
# navigation — one source of truth for the sidebar, the palette and the footer
# --------------------------------------------------------------------------- #
NAV: list[dict] = [
    {"group": "Overview", "pages": [
        {"href": "/app", "icon": "▦", "label": "Dashboard", "key": "overview"},
    ]},
    {"group": "Intelligence", "pages": [
        {"href": "/app/research", "icon": "✧", "label": "Ask indexed sources", "key": "research"},
        {"href": "/app/research/agent", "icon": "◎", "label": "Research reports", "key": "research_agent"},
        {"href": "/app/discovery", "icon": "✦", "label": "Topic discovery", "key": "discovery"},
        {"href": "/app/watch", "icon": "◉", "label": "Web monitoring", "key": "watch"},
        {"href": "/app/graph", "icon": "◇", "label": "Knowledge graph", "key": "graph"},
    ]},
    {"group": "Data Engine", "pages": [
        {"href": "/app/crawl", "icon": "🕷", "label": "Crawl jobs", "key": "crawl"},
        {"href": "/app/search", "icon": "🔍", "label": "Query the index", "key": "search"},
        {"href": "/app/index", "icon": "🗂", "label": "Own search index", "key": "index"},
        {"href": "/app/marketplace", "icon": "🛒", "label": "Data exchange", "key": "marketplace"},
    ]},
    {"group": "Developers & settings", "pages": [
        {"href": "/app/keys", "icon": "🔑", "label": "API keys", "key": "keys"},
        {"href": "/app/billing", "icon": "💳", "label": "Billing & usage", "key": "billing"},
        {"href": "/app/settings", "icon": "⚙", "label": "Settings", "key": "settings"},
        {"href": "/app/docs", "icon": "📘", "label": "API reference", "key": "docs"},
    ]},
]

PLANS = [
    {
        "id": "free",
        "name": "Free",
        "price": 0,
        "blurb": "Kick the tyres.",
        "features": [
            "1,000 pages / month",
            "5 concurrent jobs",
            "Community support",
            "Own index: level 0",
        ],
    },
    {
        "id": "starter",
        "name": "Starter",
        "price": 49,
        "blurb": "One serious project.",
        "features": [
            "250,000 pages / month",
            "25 concurrent jobs",
            "Stealth + residential proxy",
            "Email support",
        ],
    },
    {
        "id": "pro",
        "name": "Pro",
        "price": 199,
        "blurb": "Most teams.",
        "best": True,
        "features": [
            "2,000,000 pages / month",
            "100 concurrent jobs",
            "Semantic reranking + RAG",
            "Webhooks & exports",
            "Priority support",
        ],
    },
    {
        "id": "enterprise",
        "name": "Enterprise",
        "price": None,
        "blurb": "Bring your own cloud.",
        "features": [
            "Unlimited pages",
            "Unlimited concurrency",
            "VPC / on-prem deployment",
            "SSO + audit log",
            "Dedicated engineer",
        ],
    },
]

FEATURES = [
    (
        "🕷",
        "Respectful by default",
        (
            "robots.txt, per-host politeness, adaptive rate limits and a frontier "
            "that never hammers one site."
        ),
    ),
    (
        "✦",
        "Topic discovery",
        (
            "Describe what you want in a sentence. Get a ranked shortlist of the "
            "right websites, with the reasoning shown."
        ),
    ),
    (
        "🔍",
        "Your own search index",
        (
            "BM25 over Curlie, Wikipedia and Common Crawl — no Google, no SerpAPI, "
            "no per-query bill, works unplugged."
        ),
    ),
    (
        "🧠",
        "Semantic reranking",
        (
            "BM25 shortlists, an encoder reorders. Turn it on with one flag; it "
            "turns itself off if the model is missing."
        ),
    ),
    (
        "🛡",
        "Stealth & proxies",
        (
            "TLS fingerprints, rotating residential egress and CAPTCHA-aware pacing "
            "for the sites that need it."
        ),
    ),
    (
        "🛒",
        "Data marketplace",
        (
            "Sell what you crawled, post a bounty for what you do not have, deliver "
            "signed and verifiable."
        ),
    ),
    (
        "🔗",
        "Webhooks & exports",
        (
            "JSONL, CSV, Parquet or straight into your warehouse. Push events "
            "instead of polling."
        ),
    ),
    (
        "📊",
        "Quotas you can see",
        (
            "Usage, overage and invoices in one place — no surprise bill at the end "
            "of the month."
        ),
    ),
]


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _env():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        {
            "bytes": human_bytes,
            "num": human_num,
            "ago": human_ago,
            "pct": human_pct,
            "money": human_money,
            "stars": human_stars,
            "rows": as_rows,
        }
    )
    env.globals.update(nav=NAV, now=time.time)
    return env


_ENV = None


def _templates():
    global _ENV
    if _ENV is None:
        _ENV = _env()
    return _ENV


def human_bytes(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n < 1:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = min(int(math.log(n, 1024)), len(units) - 1)
    return (
        f"{n / 1024**i:,.0f} {units[i]}" if i == 0 else f"{n / 1024**i:,.1f} {units[i]}"
    )


def human_num(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:,.0f}"


def human_ago(value) -> str:
    if not value:
        return "—"
    try:
        if isinstance(value, (int, float)):
            when = datetime.fromtimestamp(float(value), tz=UTC)
        else:
            when = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    delta = (datetime.now(UTC) - when).total_seconds()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)}d ago"
    return when.strftime("%d %b %Y")


def human_pct(value) -> str:
    try:
        return f"{float(value) * 100 if float(value) <= 1 else float(value):.0f}%"
    except (TypeError, ValueError):
        return "—"


def human_money(cents) -> str:
    try:
        return f"${int(cents) / 100:,.2f}"
    except (TypeError, ValueError):
        return "—"


def as_rows(value) -> list:
    """Normalise a list endpoint's payload into a list of rows.

    List endpoints answer ``{"items": [...], "total": n}``, but a couple
    answer with a bare list, and Jinja's ``is sequence`` test is true for a
    dict too — which silently turns a for-loop into an iteration over the
    dict's keys. One filter, used everywhere, keeps that class of bug out.
    """
    if isinstance(value, dict):
        for key in (
            "items",
            "results",
            "data",
            "datasets",
            "bounties",
            "candidates",
            "levels",
        ):
            found = value.get(key)
            if isinstance(found, list):
                return found
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def human_stars(value) -> str:
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return ""
    full = round(rating)
    return "★" * full + "☆" * (5 - full)


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    """Render a page with the context every template expects."""
    token = current_token(request)
    base = str(request.base_url).rstrip("/")
    ctx.setdefault("request", request)
    ctx.setdefault("page", "")
    ctx.setdefault("title", "Web Intelligence OS")
    ctx.setdefault("token", token)
    ctx.setdefault("api", Api(base_url=base, token=token, app=_asgi_app(request)))
    ctx.setdefault("path", request.url.path)
    ctx.setdefault("query", dict(request.query_params))
    ctx.setdefault("errors", [])
    ctx.setdefault("flash", request.query_params.get("flash", ""))
    ctx.setdefault("flash_kind", request.query_params.get("kind", "ok"))
    body = _templates().get_template(template).render(**ctx)
    return HTMLResponse(body)


def _redirect(url: str, flash: str = "", kind: str = "ok") -> RedirectResponse:
    if flash:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}flash={flash}&kind={kind}"
    return RedirectResponse(url, status_code=303)


def _asgi_app(request: Request):
    """The API application, when this console is mounted right on top of it.

    ``mount_webui`` records it, so page handlers can call the API in-process
    instead of over loopback. ``None`` when the console is pointed at a
    remote API, in which case :class:`Api` falls back to real HTTP.
    """
    return getattr(request.app.state, "webui_target", None)


def _api(request: Request, token: str = "") -> Api:
    """A client for the API, scoped to this request's caller."""
    return Api(
        base_url=str(request.base_url).rstrip("/"), token=token, app=_asgi_app(request)
    )


def _anon(request: Request) -> Api:
    """A client with no credentials, for sign-in and sign-up."""
    return Api.anonymous(str(request.base_url).rstrip("/"), _asgi_app(request))


async def _identity(request: Request) -> dict:
    """Who is browsing? Empty dict when signed out or the session died."""
    token = current_token(request)
    if not token:
        return {}
    res = await _api(request, token).me()
    return res.data if res.ok and isinstance(res.data, dict) else {}


async def _guard(request: Request):
    """Return ``(api, identity)`` or a redirect to the sign-in page."""
    token = current_token(request)
    if not token:
        return None, None, _redirect("/login")
    api = _api(request, token)
    me = await api.me()
    if not me.ok:
        response = _redirect("/login?flash=Session+expired&kind=warn")
        clear_session(response)
        return None, None, response
    return api, (me.data if isinstance(me.data, dict) else {}), None


# --------------------------------------------------------------------------- #
# public pages
# --------------------------------------------------------------------------- #
@router.get("/", response_class=HTMLResponse, name="landing")
async def landing(request: Request):
    return render(
        request,
        "landing.html",
        page="landing",
        title="Web Intelligence OS — crawl, discover, own your index",
        features=FEATURES,
        plans=PLANS,
    )


@router.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request):
    return render(
        request,
        "pricing.html",
        page="pricing",
        plans=PLANS,
        title="Pricing — Web Intelligence OS",
    )


# --------------------------------------------------------------------------- #
# sign in / out
# --------------------------------------------------------------------------- #
@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if current_token(request):
        return _redirect("/app")
    return render(
        request,
        "login.html",
        page="login",
        mode="login",
        title="Sign in — Web Intelligence OS",
    )


@router.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, email: str = Form(""), password: str = Form("")):
    api = _anon(request)
    res = await api.login(email.strip(), password)
    if not res.ok:
        return render(
            request,
            "login.html",
            page="login",
            mode="login",
            errors=[res.error or "Invalid email or password"],
            email=email,
            title="Sign in — Web Intelligence OS",
        )
    token = res.get("access_token", "")
    response = _redirect("/app", "Welcome back", "ok")
    set_session(response, token)
    return response


@router.get("/signup", response_class=HTMLResponse)
async def signup_get(request: Request):
    if current_token(request):
        return _redirect("/app")
    return render(
        request,
        "login.html",
        page="signup",
        mode="signup",
        title="Create your account — Web Intelligence OS",
    )


@router.post("/signup", response_class=HTMLResponse)
async def signup_post(
    request: Request,
    org_name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
):
    api = _anon(request)
    res = await api.signup(org_name.strip() or "My org", email.strip(), password)
    if not res.ok:
        return render(
            request,
            "login.html",
            page="signup",
            mode="signup",
            errors=[res.error or "Could not create the account"],
            email=email,
            org_name=org_name,
            title="Create your account — Web Intelligence OS",
        )
    response = _redirect("/app", "Workspace created — you are in", "ok")
    set_session(response, res.get("access_token", ""))
    return response


@router.post("/logout")
async def logout(request: Request):
    response = _redirect("/", "Signed out", "ok")
    clear_session(response)
    return response


# --------------------------------------------------------------------------- #
# console
# --------------------------------------------------------------------------- #
@router.get("/app", response_class=HTMLResponse, name="dashboard")
async def dashboard(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    jobs, usage, index, keys = await _gather(
        api.jobs(limit=8), api.usage(), api.index_stats(), api.keys()
    )
    return render(
        request,
        "dashboard.html",
        page="dashboard",
        me=me,
        jobs=jobs,
        usage=usage,
        index=index,
        keys=keys,
        title="Dashboard — Web Intelligence OS",
    )


@router.get("/app/search", response_class=HTMLResponse, name="search")
async def search_page(
    request: Request,
    q: str = Query("", max_length=2048),
    limit: int = Query(10, ge=1, le=100),
    page: int = Query(1, ge=1),
    source: str = Query(""),
    rerank: str = Query(""),
    collapse: str = Query("1"),
):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect

    results: dict = {}
    if q:
        res = await api.index_query(
            q,
            limit=limit,
            sources=source,
            rerank=rerank,
            collapse_host=(collapse == "1"),
        )
        if res.ok:
            results = res.data if isinstance(res.data, dict) else {"results": []}
        else:
            results = {"_error": res.error}
    stats = (await api.index_stats()).data or {}
    return render(
        request,
        "search.html",
        page="search",
        me=me,
        q=q,
        results=results,
        stats=stats,
        limit=limit,
        page_no=page,
        source=source,
        rerank=rerank,
        collapse=collapse,
        title=(f"{q} — Search" if q else "Search — Web Intelligence OS"),
    )


def _sanitize_research_result(response):
    if not response.ok or not isinstance(response.data, dict):
        return {"_error": response.error}
    result = response.data
    for citation in result.get("citations", []):
        url = citation.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            citation["url"] = None
    return result


@router.get("/app/research", response_class=HTMLResponse, name="research")
async def research_page(request: Request, question: str = Query("", max_length=4096)):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    result = None
    if question:
        response = await api.ask(question)
        result = _sanitize_research_result(response)
    return render(request, "research.html", page="research", me=me,
                   question=question, result=result,
                   title="AI Research — Web Intelligence OS")


@router.get("/app/graph", response_class=HTMLResponse, name="graph")
async def graph_page(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    graph = await api.graph(limit=200)
    data = graph.data if graph.ok and isinstance(graph.data, dict) else {"nodes": [], "edges": []}
    return render(request, "graph.html", page="graph", me=me, graph=data,
                  error=(graph.error if not graph.ok else ""),
                  title="Knowledge graph — Web Intelligence OS")


@router.post("/app/graph/entities", response_class=HTMLResponse)
async def graph_create_entity(request: Request, name: str = Form("", max_length=256),
                              entity_type: str = Form("Entity", max_length=80),
                              aliases: str = Form("", max_length=2048)):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    result = await api.graph_entity(name=name.strip(), entity_type=entity_type.strip() or "Entity",
                                    aliases=[a.strip() for a in aliases.split(",") if a.strip()])
    return _redirect("/app/graph", "Entity added" if result.ok else result.error,
                     "ok" if result.ok else "danger")


@router.post("/app/graph/relations", response_class=HTMLResponse)
async def graph_create_relation(request: Request, source_id: str = Form(""),
                                target_id: str = Form(""), relation: str = Form("", max_length=80)):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    result = await api.graph_relation(source_id=source_id, target_id=target_id,
                                      relation=relation.strip())
    return _redirect("/app/graph", "Relationship added" if result.ok else result.error,
                     "ok" if result.ok else "danger")


@router.get("/app/research/agent", response_class=HTMLResponse, name="research_agent")
async def research_agent_page(request: Request, research_id: str = Query("", max_length=64)):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    jobs = await api.research_list()
    selected = None
    if research_id:
        fetched = await api.research(research_id)
        selected = fetched.data if fetched.ok else {"error": fetched.error}
    return render(request, "research_agent.html", page="research_agent", me=me,
                  jobs=jobs.get("items", []) if jobs.ok else [],
                  selected=selected, research_id=research_id,
                  title="Research reports — Web Intelligence OS")


@router.post("/app/research/agent", response_class=HTMLResponse, name="research_agent_create")
async def research_agent_create(request: Request, topic: str = Form("", max_length=512),
                                sources: int = Form(5, ge=1, le=20),
                                max_pages_per_source: int = Form(3, ge=1, le=10)):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    result = await api.research_create(topic=topic.strip(), sources=sources,
                                       max_pages_per_source=max_pages_per_source)
    if not result.ok:
        return _redirect("/app/research/agent", result.error, "danger")
    return _redirect("/app/research/agent?research_id=" + result.get("research_id", ""),
                      "Research job queued", "ok")


@router.get("/app/poll/research/{research_id}", name="poll_research")
async def poll_research(request: Request, research_id: str):
    api, _me, redirect = await _guard(request)
    if redirect:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    result = await api.research(research_id)
    return JSONResponse(result.data if result.ok else {"error": result.error},
                        status_code=result.status if not result.ok else 200)


@router.post("/app/research", response_class=HTMLResponse, name="research_submit")
async def research_submit(request: Request, question: str = Form("", max_length=4096)):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    question = question.strip()
    if len(question) < 3:
        return render(request, "research.html", page="research", me=me,
                      question=question, result={"_error": "Enter at least three characters."},
                      title="AI Research — Web Intelligence OS")
    response = await api.ask(question)
    result = _sanitize_research_result(response)
    return render(request, "research.html", page="research", me=me,
                  question=question, result=result,
                  title="AI Research — Web Intelligence OS")


@router.get("/app/watch", response_class=HTMLResponse, name="watch")
async def watch_page(request: Request, watch_id: str = Query("", max_length=64)):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    watches_result = await api.watches()
    items = watches_result.get("items", []) if watches_result.ok else []
    events = []
    if watch_id:
        event_result = await api.watch_changes(watch_id)
        events = event_result.get("items", []) if event_result.ok else []
    return render(request, "watch.html", page="watch", me=me, watches=items,
                  selected_watch=watch_id, events=events,
                  error=(watches_result.error if not watches_result.ok else ""),
                  title="Web Monitoring — Web Intelligence OS")


@router.post("/app/watch", response_class=HTMLResponse, name="watch_create")
async def watch_create(request: Request, url: str = Form("", max_length=4096),
                       interval_minutes: int = Form(60, ge=15, le=10080)):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    result = await api.watch_create(url.strip(), interval_minutes)
    if not result.ok:
        return _redirect("/app/watch", result.error, "danger")
    return _redirect("/app/watch?watch_id=" + result.get("watch_id", ""),
                      "Watch created. Use Run now for the first snapshot; later checks follow the interval.", "ok")


@router.post("/app/watch/{watch_id}/run", response_class=HTMLResponse)
async def watch_run(request: Request, watch_id: str):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    result = await api.watch_run(watch_id)
    return _redirect("/app/watch?watch_id=" + watch_id,
                      "Watch crawl queued" if result.ok else result.error,
                      "ok" if result.ok else "danger")


@router.post("/app/watch/{watch_id}/delete", response_class=HTMLResponse)
async def watch_delete(request: Request, watch_id: str):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    result = await api.watch_delete(watch_id)
    return _redirect("/app/watch", "Watch stopped" if result.ok else result.error,
                      "ok" if result.ok else "danger")


@router.get("/app/crawl", response_class=HTMLResponse, name="crawl")
async def crawl_list(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    jobs = await api.jobs(limit=50)
    return render(
        request,
        "crawl.html",
        page="crawl",
        me=me,
        jobs=(jobs.data if jobs.ok else None),
        error=jobs.error,
        title="Crawl jobs — Web Intelligence OS",
    )


@router.post("/app/crawl", response_class=HTMLResponse)
async def crawl_create(
    request: Request,
    url: str = Form(""),
    max_pages: int = Form(10),
    priority: str = Form("normal"),
    stealth: str = Form(""),
    obey_robots: str = Form("on"),
):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    res = await api.crawl(
        url.strip(),
        max_pages=max(1, int(max_pages)),
        priority=priority,
        stealth=bool(stealth),
        obey_robots=bool(obey_robots),
    )
    if not res.ok:
        jobs = await api.jobs(limit=50)
        return render(
            request,
            "crawl.html",
            page="crawl",
            me=me,
            jobs=(jobs.data if jobs.ok else None),
            errors=[res.error],
            url=url,
            title="Crawl jobs — Web Intelligence OS",
        )
    job_id = res.get("job_id", "")
    return _redirect(
        f"/app/crawl/{job_id}" if job_id else "/app/crawl", "Crawl queued", "ok"
    )


@router.get("/app/crawl/{job_id}", response_class=HTMLResponse)
async def crawl_detail(request: Request, job_id: str):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    job = await api.job(job_id)
    if not job.ok:
        return _redirect("/app/crawl", job.error or "Job not found", "err")
    return render(
        request,
        "crawl_detail.html",
        page="crawl",
        me=me,
        job=job.data,
        job_id=job_id,
        title=f"Job {job_id[:12]} — Web Intelligence OS",
    )


@router.post("/app/crawl/{job_id}/cancel")
async def crawl_cancel(request: Request, job_id: str):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    res = await api.cancel_job(job_id)
    return _redirect(
        f"/app/crawl/{job_id}",
        "Cancellation requested" if res.ok else res.error,
        "ok" if res.ok else "err",
    )


def _has_discovery_provider() -> bool:
    """Is any discovery provider actually able to reach the network?

    Discovery is the one feature that genuinely needs an outbound search. With
    no provider configured the endpoint still answers — with an empty
    shortlist, which reads like a bug unless the page says otherwise. So the
    form warns instead of pretending.
    """
    import os

    configured = any(
        os.getenv(k)
        for k in (
            "BRAVE_API_KEY",
            "SERPAPI_KEY",
            "TAVILY_API_KEY",
            "SERPER_API_KEY",
            "GOOGLE_CSE_KEY",
        )
    )
    own_index = os.getenv("WCP_DISCOVERY_PROVIDER", "").lower() in (
        "own-index",
        "own_index",
        "ownindex",
        "internal",
    )
    offline = os.getenv("WCP_OFFLINE", "").lower() in ("1", "true", "yes")
    return (configured or own_index) and not offline


@router.get("/app/discovery", response_class=HTMLResponse, name="discovery")
async def discovery_list(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    jobs = await api.discovery_list()
    return render(
        request,
        "discovery.html",
        page="discovery",
        me=me,
        jobs=(jobs.data if jobs.ok else None),
        error=jobs.error,
        has_provider=_has_discovery_provider(),
        title="Topic discovery — Web Intelligence OS",
    )


@router.post("/app/discovery", response_class=HTMLResponse)
async def discovery_create(
    request: Request,
    topic: str = Form(""),
    count: int = Form(10),
    max_pages_per_site: int = Form(20),
    auto: str = Form(""),
    seeds: str = Form(""),
    preview: str = Form(""),
):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    seed_list = [s.strip() for s in seeds.replace("\n", ",").split(",") if s.strip()]
    body = {
        "topic": topic.strip(),
        "count": max(1, min(int(count), 500)),
        "max_pages_per_site": max(1, min(int(max_pages_per_site), 10000)),
        "auto": bool(auto),
        "seeds": seed_list,
    }

    if preview:  # dry run: show the shortlist, crawl nothing
        res = await api.discovery_preview(**body)
        jobs = await api.discovery_list()
        data = res.data if res.ok and isinstance(res.data, dict) else {}
        return render(
            request,
            "discovery.html",
            page="discovery",
            me=me,
            jobs=(jobs.data if jobs.ok else None),
            preview_results=data.get("candidates") or [],
            preview_queries=data.get("queries") or [],
            preview_providers=data.get("providers") or [],
            errors=[] if res.ok else [res.error],
            topic=topic,
            count=count,
            has_provider=_has_discovery_provider(),
            title="Topic discovery — Web Intelligence OS",
        )

    res = await api.discovery_create(**body)
    if not res.ok:
        jobs = await api.discovery_list()
        return render(
            request,
            "discovery.html",
            page="discovery",
            me=me,
            jobs=(jobs.data if jobs.ok else None),
            errors=[res.error],
            topic=topic,
            has_provider=_has_discovery_provider(),
            title="Topic discovery — Web Intelligence OS",
        )
    job_id = res.get("job_id", "")
    return _redirect(
        f"/app/discovery/{job_id}" if job_id else "/app/discovery",
        "Discovery started",
        "ok",
    )


@router.get("/app/discovery/{job_id}", response_class=HTMLResponse)
async def discovery_detail(request: Request, job_id: str):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    job, cands, results = await _gather(
        api.discovery(job_id),
        api.discovery_candidates(job_id),
        api.discovery_results(job_id),
    )
    if not job.ok:
        return _redirect("/app/discovery", job.error or "Job not found", "err")
    return render(
        request,
        "discovery_detail.html",
        page="discovery",
        me=me,
        job=job.data,
        job_id=job_id,
        candidates=(cands.data if cands.ok else None),
        results=(results.data if results.ok else None),
        title=f"Discovery — {job_id[:12]}",
    )


@router.post("/app/discovery/{job_id}/approve")
async def discovery_approve(
    request: Request, job_id: str, domains: Annotated[list[str] | None, Form()] = None
):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    res = await api.discovery_approve(job_id, [d for d in (domains or []) if d])
    return _redirect(
        f"/app/discovery/{job_id}",
        "Approved — crawling now" if res.ok else res.error,
        "ok" if res.ok else "err",
    )


@router.post("/app/discovery/{job_id}/reject")
async def discovery_reject(
    request: Request, job_id: str, domains: Annotated[list[str] | None, Form()] = None
):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    picked = [d for d in (domains or []) if d]
    if not picked:
        return _redirect(
            f"/app/discovery/{job_id}", "Tick at least one candidate first", "err"
        )
    results = await _gather(*(api.discovery_reject(job_id, d) for d in picked))
    removed = sum(1 for r in results if r.ok)
    first_error = next((r.error for r in results if not r.ok), "")
    return _redirect(
        f"/app/discovery/{job_id}",
        (f"Removed {removed} of {len(picked)} candidates" if removed else first_error),
        "ok" if removed else "err",
    )


@router.post("/app/discovery/{job_id}/cancel")
async def discovery_cancel(request: Request, job_id: str):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    res = await api.discovery_cancel(job_id)
    return _redirect(
        f"/app/discovery/{job_id}",
        "Cancelled" if res.ok else res.error,
        "ok" if res.ok else "err",
    )


@router.get("/app/index", response_class=HTMLResponse, name="index")
async def index_page(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    stats, levels = await _gather(api.index_stats(), api.index_levels())
    return render(
        request,
        "index.html",
        page="index",
        me=me,
        stats=(stats.data if stats.ok else {}),
        stats_error=stats.error,
        levels=levels,
        title="Own search index — Web Intelligence OS",
    )


@router.get("/app/marketplace", response_class=HTMLResponse, name="marketplace")
async def marketplace(request: Request, q: str = Query(""), tag: str = Query("")):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    datasets, bounties, jobs = await _gather(
        api.datasets(q=q, tag=tag), api.bounties(), api.jobs(limit=50)
    )
    rows = (
        jobs.data.get("items", [])
        if jobs.ok and isinstance(jobs.data, dict)
        else (jobs.data if isinstance(jobs.data, list) else [])
    )
    return render(
        request,
        "marketplace.html",
        page="marketplace",
        me=me,
        q=q,
        datasets=(datasets.data if datasets.ok else None),
        bounties=(bounties.data if bounties.ok else None),
        jobs=rows,
        error=datasets.error,
        title="Marketplace — Web Intelligence OS",
    )


@router.post("/app/marketplace/datasets")
async def marketplace_create(
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    tags: str = Form(""),
    price_usd: str = Form("0"),
    source_job_id: str = Form(""),
    license: str = Form("CC-BY-4.0"),
):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    try:
        cents = int(float(price_usd) * 100)
    except ValueError:
        cents = 0
    res = await api.dataset_create(
        title=title.strip(),
        description=description.strip(),
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        price_usd_cents=max(0, cents),
        license=license,
        source_job_id=source_job_id.strip(),
    )
    return _redirect(
        "/app/marketplace",
        "Listing published" if res.ok else res.error,
        "ok" if res.ok else "err",
    )


@router.post("/app/marketplace/datasets/{dataset_id}/purchase")
async def marketplace_purchase(request: Request, dataset_id: str):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    res = await api.dataset_purchase(dataset_id)
    return _redirect(
        "/app/marketplace",
        "Purchased — delivery queued" if res.ok else res.error,
        "ok" if res.ok else "err",
    )


@router.get("/app/keys", response_class=HTMLResponse, name="keys")
async def keys_page(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    keys = await api.keys()
    issued = request.query_params.get("issued", "")
    return render(
        request,
        "keys.html",
        page="keys",
        me=me,
        keys=(keys.data if keys.ok else None),
        error=keys.error,
        issued=issued,
        title="API keys — Web Intelligence OS",
    )


@router.post("/app/keys")
async def keys_create(
    request: Request,
    name: str = Form("console-key"),
    scopes: Annotated[list[str] | None, Form()] = None,
):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    res = await api.create_key(
        name.strip() or "console-key", scopes or ["read", "crawl"]
    )
    if not res.ok:
        return _redirect("/app/keys", res.error, "err")
    return _redirect(
        f"/app/keys?issued={res.get('key', '')}",
        "Key created — copy it now, it is not stored in plain text",
    )


def _plan_cards(raw: dict) -> list[dict]:
    """Turn ``GET /billing/plans`` into the shape the billing page renders.

    The endpoint answers with ``{"tiers": {...}, "usage_based": {...}}``;
    iterating that dict directly would hand the template bare plan names, so
    we flatten it here and keep the template dumb.
    """
    tiers = (raw or {}).get("tiers") or {}
    cards = []
    for pid, tier in tiers.items():
        quotas = tier.get("quotas") or {}
        pages = quotas.get("requests_per_month")
        rates = tier.get("rates") or {}
        cards.append(
            {
                "id": pid,
                "name": tier.get("display_name") or pid.title(),
                "price": (tier.get("price_cents_month") or 0) / 100,
                "pages": pages,
                "concurrency": quotas.get("concurrent_jobs"),
                "features": list(tier.get("features") or []),
                "overage": rates.get("extra_request_cost", 0.0),
            }
        )
    order = {"free": 0, "starter": 1, "pro": 2, "enterprise": 3}
    cards.sort(key=lambda c: order.get(c["id"], 9))
    return cards


def _money_pages(pages) -> str:
    if pages is None:
        return "unlimited pages"
    return f"{human_num(pages)} pages / month"


@router.get("/app/billing", response_class=HTMLResponse, name="billing")
async def billing_page(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    usage, invoices, plans = await _gather(api.usage(), api.invoices(), api.plans())
    usage_data = usage.data if usage.ok and isinstance(usage.data, dict) else {}
    raw_plans = plans.data if plans.ok and isinstance(plans.data, dict) else {}
    cards = _plan_cards(raw_plans) or PLANS
    return render(
        request,
        "billing.html",
        page="billing",
        me=me,
        usage=usage_data,
        invoices=(
            invoices.data if invoices.ok and isinstance(invoices.data, dict) else None
        ),
        plans=cards,
        rates=(raw_plans.get("usage_based") or {}),
        error=usage.error,
        title="Billing — Web Intelligence OS",
    )


def webhook_events() -> list[str]:
    """The event names ``POST /webhooks`` accepts, read from the API schema.

    Hard-coding the list in the console would let the two drift apart; the
    enum is one import away, so read it and keep a literal list only as a
    fallback for when the schema module cannot be imported.
    """
    try:
        from src.api.schemas import WebhookEvent

        return [e.value for e in WebhookEvent]
    except Exception:  # noqa: BLE001 - pragma: no cover
        return [
            "crawl.completed",
            "crawl.failed",
            "data.ready",
            "export.ready",
            "quota.warning",
            "dataset.sold",
            "discovery.completed",
            "discovery.approved",
            "web.changed",
        ]


@router.get("/app/settings", response_class=HTMLResponse, name="settings")
async def settings_page(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    hooks = await api.webhooks()
    return render(
        request,
        "settings.html",
        page="settings",
        me=me,
        hooks=(hooks.data if hooks.ok else None),
        error=hooks.error,
        events=webhook_events(),
        has_provider=_has_discovery_provider(),
        stripe_ready=bool(os.getenv("STRIPE_SECRET_KEY")),
        title="Settings — Web Intelligence OS",
    )


@router.post("/app/settings/webhooks")
async def webhook_create(
    request: Request,
    url: str = Form(""),
    description: str = Form(""),
    events: Annotated[list[str] | None, Form()] = None,
):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    res = await api.webhook_create(url.strip(), events or [], description.strip())
    return _redirect(
        "/app/settings",
        "Webhook subscribed" if res.ok else res.error,
        "ok" if res.ok else "err",
    )


@router.post("/app/settings/webhooks/{sub_id}/delete")
async def webhook_delete(request: Request, sub_id: str):
    api, _me, redirect = await _guard(request)
    if redirect:
        return redirect
    res = await api.webhook_delete(sub_id)
    return _redirect(
        "/app/settings",
        "Webhook removed" if res.ok else res.error,
        "ok" if res.ok else "err",
    )


@router.get("/app/poll/jobs/{job_id}", name="poll_job")
async def poll_job(request: Request, job_id: str):
    """Job state as JSON, authenticated by the browser's session cookie.

    Live-refreshing a page means the browser has to ask *something* for
    progress, and it cannot ask ``/api/v1`` directly — the JWT lives in an
    httpOnly cookie that JavaScript is deliberately not allowed to read. So
    the console proxies it: same endpoint, server-side credentials, and the
    smallest possible surface (one read-only reader per page that needs it).
    """
    api, _me, redirect = await _guard(request)
    if redirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    res = await api.job(job_id)
    return JSONResponse(
        res.data if res.ok else {"error": res.error},
        status_code=res.status if res.ok else 404,
    )


@router.get("/app/poll/discovery/{job_id}", name="poll_discovery")
async def poll_discovery(request: Request, job_id: str):
    """Discovery state as JSON — see :func:`poll_job` for why this exists."""
    api, _me, redirect = await _guard(request)
    if redirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    res = await api.discovery(job_id)
    return JSONResponse(
        res.data if res.ok else {"error": res.error},
        status_code=res.status if res.ok else 404,
    )


@router.get("/app/docs", response_class=HTMLResponse, name="docs")
async def docs_page(request: Request):
    _api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    return render(
        request,
        "docs.html",
        page="docs",
        me=me,
        endpoints=_ENDPOINTS,
        title="API reference — Web Intelligence OS",
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _gather(*awaitables):
    """Run several API calls and return their :class:`Result` in order."""
    import asyncio

    return await asyncio.gather(*awaitables)


_ENDPOINTS = [
    ("POST", "/api/auth/signup", "Create an organisation and its owner user."),
    ("POST", "/api/auth/login", "Exchange email + password for a JWT pair."),
    ("POST", "/api/auth/api-keys", "Mint a long-lived API key (JWT required)."),
    ("POST", "/api/v1/jobs/crawl", "Queue a crawl: url, max_pages, stealth, priority."),
    ("GET", "/api/v1/jobs/{job_id}", "Job state, progress and error, if any."),
    ("POST", "/api/v1/jobs/{job_id}/cancel", "Ask the scheduler to stop a job."),
    ("POST", "/api/v1/discovery", "Turn a topic sentence into a ranked shortlist."),
    ("POST", "/api/v1/discovery/preview", "Same ranking, crawls nothing."),
    ("GET", "/api/v1/discovery/{job_id}/candidates", "The shortlist, with reasons."),
    ("POST", "/api/v1/discovery/{job_id}/approve", "Approve and start crawling."),
    ("GET", "/api/v1/search-index/query", "Search your own index (BM25 + authority)."),
    ("GET", "/api/v1/search-index/spell", "Correct a query against your vocabulary."),
    ("GET", "/api/v1/search-index/stats", "Index size, sources and build state."),
    ("GET", "/api/v1/search-index/levels", "What each index level costs."),
    ("POST", "/api/v1/search", "Vector/RAG search over ingested pages."),
    ("GET", "/api/v1/marketplace/datasets", "Browse datasets for sale."),
    ("POST", "/api/v1/marketplace/datasets", "List a dataset you crawled."),
    ("POST", "/api/v1/marketplace/bounties", "Post a bounty for data you need."),
    ("GET", "/api/v1/billing/usage", "Pages, tokens, storage and overage."),
    ("GET", "/api/v1/billing/plans", "Plan limits and prices."),
    ("POST", "/api/v1/webhooks", "Subscribe to job and delivery events."),
    ("GET", "/healthz", "Liveness probe."),
]


def mount_webui(app) -> bool:
    """Attach the console to an existing FastAPI app.

    Returns ``False`` (and logs once) when Jinja2 is missing, so a minimal
    deployment can skip the UI without the whole API refusing to boot.
    """
    try:
        import jinja2  # noqa: F401
    except ImportError:
        logger.warning(
            "jinja2 is not installed — the web console is disabled "
            "(pip install jinja2 to enable it)"
        )
        return False

    # The console calls the API it is mounted on. Handing it the ASGI app
    # directly keeps that in-process: no loopback socket, no extra port, and
    # it cannot deadlock a single-worker deployment.
    app.state.webui_target = app
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="webui-static")
    app.include_router(router)
    try:
        from src.webui.speed import install_speed

        install_speed(app)
    except Exception as exc:  # noqa: BLE001 - speed is never worth a 500
        logger.warning("console speed middleware disabled (%s)", exc)
    logger.info("web console mounted at /")
    return True
