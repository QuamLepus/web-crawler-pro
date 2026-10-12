"""WebCrawlerPro public v1 API package.

Exposes the FastAPI application factory (:func:`src.api.main.create_api_app`)
plus the router modules and shared Pydantic schemas that make up the stable
``/api/v1`` contract:

* :mod:`src.api.routers.jobs`        — crawl job submit / status / cancel
* :mod:`src.api.routers.search`      — semantic + structured search
* :mod:`src.api.routers.webhooks`    — webhook subscriptions & test events
* :mod:`src.api.routers.billing`     — plans, usage meters, Stripe checkout
* :mod:`src.api.routers.marketplace` — dataset listings, reviews, bounties
* :mod:`src.api.graphql`             — Strawberry GraphQL schema (``/graphql``)
* :mod:`src.api.middleware`          — Redis-backed per-key rate limiting
* :mod:`src.api.observability`       — OpenTelemetry tracing + Prometheus
"""

from src.api.main import app, create_api_app

__all__ = ["app", "create_api_app"]
