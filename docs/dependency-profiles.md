# Python dependency profiles

Dependency requirements are grouped by service and by capabilities that are
large or environment-specific. Each service profile includes
`requirements-core.txt`, so it is independently installable.

| Profile | Install command | Includes |
|---|---|---|
| API/dashboard | `pip install -r requirements-api.txt` | FastAPI, GraphQL, auth, billing, metrics, core crawler/storage clients |
| Celery worker | `pip install -r requirements-worker.txt` | Core runtime plus tabular/data pipeline packages |
| AI/PII | `pip install -r requirements-ai.txt` | LangChain/OpenAI integration, Chroma, fastText, Presidio |
| Browser/stealth | `pip install -r requirements-browser.txt` | Playwright, Selenium and curl-cffi stealth backends |
| Full development | `pip install -r requirements-dev.txt` | Backwards-compatible full dependency set plus tests and lint tools |
| Search-index build | `pip install -r requirements-search-index.txt` | Minimal standalone own-index build/query dependencies |
| OTLP export | `pip install -r requirements-otel.txt` | Optional OpenTelemetry OTLP/gRPC exporter |

`requirements.txt` remains a compatibility entry point for the complete
runtime and development dependency set. New services and container targets
should use the narrowest profile they need. The standard Docker targets install
API or worker profiles; `worker-stealth` adds the browser profile. Pull requests
build all four Docker targets without publishing images, load them into the
runner, report each Docker virtual size, and upload per-image reports with
30-day retention. After a push to `main` publishes commit-SHA images, CI pulls
all four through `docker-compose.ghcr.yml`, reports their virtual sizes, uploads
a combined 90-day report, and smoke-tests the published dashboard and both
Celery workers with Redis. The full GHCR stack is not yet exercised.
AI and browser libraries are imported lazily in the relevant features, which
retain their built-in fallbacks when those optional packages are not installed.

Install multiple profiles together when a deployment needs multiple feature
sets, for example:

```bash
pip install -r requirements-api.txt -r requirements-ai.txt
```

The profiles use version ranges, not a lockfile. Resolve and test them together
when changing dependency versions, especially when upgrading FastAPI,
Strawberry, Pydantic, Celery, or OpenTelemetry.
