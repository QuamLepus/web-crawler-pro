# 🕷️ Web Crawler Pro

**Enterprise-grade, cloud-native, distributed web scraping & data-ingestion platform** — a self-hosted alternative to Scrapy Cloud / Zyte, with stealth fetching, quality engineering, and AI-powered extraction built in.

It ships in three layers, usable independently:

| Layer | What you get | Entry point |
|---|---|---|
| **Single-node crawler** | Scrape a site from your laptop and export CSV/JSON | `python main.py` |
| **Distributed crawl platform** | Celery + Redis frontier, stealth/anti-bot, quality pipeline | `workers/crawl_tasks.py` |
| **Hosted API (SaaS)** | Multi-tenant REST + GraphQL, API keys, quotas, billing, webhooks, marketplace | `src/api/main.py` |

---

## ✨ Feature Matrix

| Capability | Implementation |
|---|---|
| Distributed task queue | **Celery + Redis**, 3 priority queues (`crawl.high/default/bulk`), late-ack, fair dispatch |
| URL Frontier | **Redis**: dedupe (SET), priority (ZSET), per-domain politeness, crash-safe in-flight recovery — atomic Lua pop |
| Horizontal workers | Stateless Celery nodes; scale by starting more pods/processes |
| Job orchestration | Priority scheduler (`crawl_job` / `ingest_source` / `tokenize` / `dedupe`) with quota gates |
| Proxy rotation | Weighted tiers: **datacenter / residential / mobile**, failure cooldowns, provider-gateway ready |
| Stealth browsing | **Playwright** (stealth init scripts) + **undetected-chromedriver** fallback |
| TLS fingerprinting | **curl_cffi** Chrome JA3/H2 impersonation (Cloudflare bypass) |
| CAPTCHA solving | **2Captcha / Anti-Captcha** reCAPTCHA v2 (incl. invisible) |
| Data cleaning | Unicode/whitespace normalization, URL canonicalization, ISO timestamps (**Pandas + Polars**) |
| Deduplication | **MinHash** (shared Redis index) + **SimHash** (Hamming ≤ 3) |
| Language detection | **fastText** `lid.176` with heuristic fallback |
| PII redaction | **Presidio** analyzer/anonymizer with regex fallback |
| Storage | JSONL, CSV, partitioned **Parquet** → **MinIO/S3**, **Elasticsearch** bulk indexing |
| AI parsing | **LangChain LLM parser** — structured JSON from raw HTML, zero CSS selectors |
| Semantic search | **ChromaDB** vectors + pluggable embeddings (OpenAI / sentence-transformers / offline hash) |
| Multi-tenancy | Signup, org, JWT + API keys, per-tenant quotas and rate limits |
| Billing | **Stripe** checkout/subscriptions/invoices, usage metering, plan quotas |
| Webhooks | Signed deliveries with per-subscription events and a delivery log |
| Marketplace | Publish crawled datasets, sell them, post bounties, review purchases |
| Dashboard | FastAPI + **WebSocket** live metrics, frontier depth, throughput |
| Observability | **Prometheus** client metrics + **Sentry** error tracking & tracing |
| DevOps | Multi-stage Dockerfiles, docker-compose full stack, GitHub Actions CI/CD, **Kubernetes** manifests + HPA |

---

## 🏗️ System Architecture

```mermaid
flowchart TB
    subgraph Control["Control Plane"]
        SEED[Seed API / CLI] -->|seed URLs| FR
        BEAT[Celery Beat scheduler] -->|drain_frontier| BROKER
        API[REST + GraphQL API<br/>POST /v1/jobs/crawl] -->|submit| SCHED[Scheduler]
        SCHED --> BROKER
    end

    subgraph Dist["Distributed Crawling Layer"]
        FR[(Redis URL Frontier<br/>dedupe · priority · politeness)]
        BROKER[(Celery Broker<br/>Redis queues)]
        FR -->|pop batch| W0[drain task]
        W0 --> BROKER
        BROKER --> W1[Worker pool<br/>fetch_page ×N]
        BROKER --> W2[Stealth worker<br/>playwright / curl_cffi]
        BROKER --> W3[crawl_domain<br/>job-backed BFS crawl]
        W1 & W2 & W3 -->|links| FR
        W3 -->|progress + digest| JOB[("wcp:v1:job:*")]
    end

    subgraph Anti["Anti-Bot Middleware"]
        PX[Proxy rotation<br/>DC · Residential · Mobile]
        CAP[CAPTCHA solver<br/>2Captcha / Anti-Captcha]
        TLS[TLS fingerprint<br/>curl_cffi impersonation]
    end
    W1 -.-> PX & TLS
    W2 -.-> PX & CAP & TLS

    subgraph Pipe["Data Pipeline & Quality"]
        W1 & W2 & W3 --> CLN[Cleaning / normalization<br/>Pandas · Polars]
        CLN --> DEDUP[Dedupe<br/>MinHash · SimHash]
        DEDUP --> LANG[Language detect fastText<br/>PII redact Presidio]
        LANG --> ST{Storage fan-out}
    end

    subgraph Store["Storage Layer"]
        ST --> JS[JSONL / CSV]
        ST --> PQ[(Parquet lake<br/>→ MinIO S3)]
        ST --> ES[(Elasticsearch)]
        ST --> VEC[(ChromaDB vectors)]
    end

    subgraph AI["AI Layer"]
        LLM[LangChain Intelligent Parser<br/>HTML → structured JSON]
        SEM[Semantic search API]
        VEC --> SEM
    end

    subgraph Obs["Observability"]
        PROM[(Prometheus)] --- GRAF[Grafana]
        SENTRY[Sentry]
        DASH[Live Dashboard<br/>FastAPI + WebSockets]
    end
    FR & BROKER -->|metrics| PROM
    W1 & W2 -->|errors| SENTRY
    DASH <--> FR
    JOB --> MKT[Marketplace listings]
```

---

## 📁 Project Structure

```
web-crawler-pro/
├── main.py                          # single-node CLI entry point
├── requirements.txt                 # full runtime + dev dependencies
├── .env.example                     # every tunable, documented
├── Dockerfile                       # multi-stage: worker | crawler | dashboard | worker-stealth
├── docker-compose.yml               # full stack: Redis, Postgres, MinIO, Elastic, workers, beat, dashboard, Prometheus
├── src/
│   ├── celery_app.py                # Celery app, queues, reliability config
│   ├── config.py                    # env-driven singleton (development / production / testing)
│   ├── crawlers/                    # single-node crawlers: basic, advanced, async, pagination
│   ├── frontier/url_frontier.py     # Redis frontier (Lua-atomic pop, politeness, ack/nack)
│   ├── middleware/
│   │   ├── proxy_rotation.py        # weighted tier proxies + cooldowns
│   │   ├── stealth_middleware.py    # curl_cffi → playwright → undetected-chromedriver
│   │   └── captcha_solver.py        # 2Captcha / Anti-Captcha
│   ├── pipeline/
│   │   ├── cleaning.py              # normalize_item, clean_dataframe (Polars/Pandas)
│   │   ├── dedupe.py                # MinHashDeduper (Redis) + SimHashDeduper
│   │   ├── language_pii.py          # fastText detect + Presidio redact (with fallbacks)
│   │   └── storage/                 # json, csv, parquet+minio, elasticsearch exporters
│   ├── ai/
│   │   ├── llm_parser.py            # LangChain schema-driven HTML→JSON extraction
│   │   ├── vector_search.py         # ChromaDB semantic search, pluggable embedders
│   │   └── agents/                  # agent_crawler, data_curator, rag_chain, synthetic
│   ├── api/
│   │   ├── main.py                  # app factory, auth middleware, service container
│   │   ├── graphql.py               # Strawberry schema mounted at /graphql
│   │   └── routers/                 # jobs, search, webhooks, billing, marketplace
│   ├── gateway/                     # auth/tokens, users, billing, quotas, rate limit, webhooks
│   ├── compute/
│   │   ├── scheduler.py             # priority job scheduler (Celery beat + Redis)
│   │   └── ray_jobs.py, spark_jobs.py
│   ├── marketplace/service.py       # datasets, bounties, purchases, reviews
│   ├── ingestion/                   # RSS / sitemap / API / S3 / CommonCrawl sources
│   ├── training/                    # quality filters, tokenization, MinHash/semantic dedupe
│   ├── observability/metrics.py     # Prometheus counters/gauges/histograms + Sentry init
│   ├── dashboard/app.py             # FastAPI + WebSocket live ops UI
│   └── utils/logger.py
├── workers/crawl_tasks.py           # seed / drain / fetch / process + crawl_domain Celery tasks
├── deploy/
│   ├── prometheus.yml
│   └── k8s/manifests/               # redis, worker Deployment + HPA, dashboard + Ingress
├── .github/workflows/ci.yml         # test job (Redis service) + matrix image builds
└── tests/                           # 75 tests, fakeredis-backed, no services required
```

---

## 🚀 Setup Guide

Requires **Python 3.11+** (CI runs 3.12).

### 1. Local development (single node)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # edit as needed
pytest                                    # 75 tests, fakeredis-backed, no services required
python main.py                            # quick crawl of https://example.com
```

### 2. Full stack with Docker Compose

```bash
docker compose up --build -d
# Services: redis, postgres, minio (:9001 console), elasticsearch (:9200),
#           worker, worker-stealth, celery beat, dashboard (:8080), prometheus (:9090)

# Seed the frontier and start crawling:
celery -A src.celery_app call workers.seed_frontier --json='["https://example.com"]'
celery -A src.celery_app call workers.drain_frontier --kwargs='{"batch": 20}'
```

Open **http://localhost:8080** for the live dashboard (WebSocket updates every 2 s).

Prebuilt images are published to GHCR on every merge to `main`, tagged with the
commit SHA:

```bash
docker pull ghcr.io/quamlepus/web-crawler-pro/worker:<sha>
# also: crawler, dashboard, worker-stealth
```

### 3. Scale workers horizontally

```bash
docker compose up -d --scale worker=8       # plain processes
# or on Kubernetes (HPA scales 3→50 on queue depth):
kubectl apply -f deploy/k8s/manifests/
```

### 4. Enable stealth / anti-bot features

```bash
pip install playwright curl-cffi undetected-chromedriver
playwright install chromium
export PROXY_RESIDENTIAL="http://user:pass@res-gateway:8000"
export CAPTCHA_API_KEY="<2captcha key>" CAPTCHA_PROVIDER=2captcha
```

The `worker-stealth` Docker target bundles everything (browsers + fonts included).

### 5. Enable AI features

```bash
pip install langchain langchain-openai chromadb
export OPENAI_API_KEY=sk-...
export EMBEDDING_PROVIDER=openai      # or sentence-transformers / hash (offline)
```

```python
from src.ai.llm_parser import LLMParser
schema = {"title": "string", "price": "number|null", "author": "string"}
data = LLMParser().parse(html, schema)          # no CSS selectors needed

from src.ai.vector_search import SemanticSearch
s = SemanticSearch(); s.add_document("id1", crawled_text, {"url": url})
s.search("best data ingestion tools", top_k=5)
```

### 6. Observability

* **Prometheus** scrapes workers on `:8000/metrics` (`crawler_pages_crawled_total`, `crawler_frontier_pending`, `crawler_request_duration_seconds`, `crawler_items_stored_total`, `crawler_duplicates_skipped_total`, …). Query at http://localhost:9090.
* **Sentry**: set `SENTRY_DSN`; the Celery integration is wired automatically.
* **Dashboard** `/ws/metrics` streams frontier stats over WebSockets; `/api/stats` for REST.

### 7. CI/CD

`.github/workflows/ci.yml` runs lint + pytest (75 tests) against a real Redis service container on Python 3.12, then builds & pushes all four Docker targets (`worker`, `crawler`, `dashboard`, `worker-stealth`) to GHCR on merges to `main`.

---

## 🌐 Hosted API

The multi-tenant surface lives under `/api/v1` (a legacy `/v1` alias is kept) with a GraphQL
alternative at `/graphql`. Authenticate with a `Bearer` JWT or an `X-API-Key` header.

Run it locally with:

```bash
uvicorn src.api.main:app --host 0.0.0.0 --port 8000
```

Then:

```bash
# 1. create a tenant
curl -X POST localhost:8000/auth/signup -H 'content-type: application/json' \
     -d '{"org_name":"Acme Labs","email":"ada@example.com","password":"s3cret-passw0rd!"}'

# 2. mint an API key with the returned access token
curl -X POST localhost:8000/keys -H "Authorization: Bearer $ACCESS" \
     -H 'content-type: application/json' -d '{"name":"ci"}'

# 3. submit a crawl
curl -X POST localhost:8000/v1/jobs/crawl -H "X-API-Key: $KEY" \
     -H 'content-type: application/json' \
     -d '{"url":"https://example.com","max_pages":50}'
```

| Group | Endpoints |
|---|---|
| Auth | `POST /auth/signup`, `/auth/login`, `/auth/refresh`, `GET /auth/me`, `POST /keys` |
| Jobs | `POST /v1/jobs/crawl`, `GET /v1/jobs`, `GET /v1/jobs/{id}`, `POST /v1/jobs/{id}/cancel` |
| Search | `POST /v1/search`, `GET /v1/search/rag` |
| Webhooks | `GET/POST /v1/webhooks`, `POST /v1/webhooks/test`, `GET /v1/webhooks/{id}/deliveries`, `DELETE /v1/webhooks/{id}` |
| Billing | `GET /v1/billing/plans`, `/usage`, `/invoice`, `POST /v1/billing/checkout` |
| Marketplace | datasets, bounties, submissions, awards, purchases, reviews, deliveries |
| Ops | `GET /healthz`, `/readyz`, `/metrics` |

Interactive docs at `/docs`, GraphiQL at `/graphql`.

```graphql
{ viewer { tenant role principal }
  jobs(limit: 5) { total items { jobId state url } } }
```

> GraphQL mutations are disabled on the read-only surface: set `WCP_GRAPHQL_MUTATIONS=1`
> to enable them.

### Crawl job lifecycle

```
POST /v1/jobs/crawl  →  Scheduler (quota gate)  →  Celery: crawl_domain
     → BFS within the start domain, robots.txt honoured
     → per page: clean → dedupe → store
     → live progress written to wcp:v1:job:{id}
     → on finish: data_sha256 stamped, crawl.completed webhook emitted
```

Because the digest is written to the job record, a finished crawl can be published straight
to the marketplace with `source_job_id`.

---

## ⚙️ Configuration Reference

See **`.env.example`** for the complete list. Highlights:

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Frontier + Celery broker/backend |
| `FRONTIER_NAMESPACE` | `frontier` | Key prefix (multi-tenant frontiers) |
| `JWT_SECRET` | – | **Required.** Signing key for auth tokens |
| `DEDUPE_THRESHOLD` | `0.9` | MinHash Jaccard duplicate cutoff |
| `CRAWLER_MAX_PAGES` | `100` | Default page budget per crawl |
| `CRAWLER_OUTPUT_DIR` | `output` | Where exported items land |
| `CRAWL_OUTPUT_DIR` | `output/crawl` | Working dir for job-backed crawls |
| `PROXY_DATACENTER/_RESIDENTIAL/_MOBILE` | – | Comma-separated proxy pools per tier |
| `CAPTCHA_API_KEY` / `CAPTCHA_PROVIDER` | – | `2captcha` \| `anti-captcha` |
| `STRIPE_SECRET_KEY` | – | Unset ⇒ billing runs on the in-memory dev shim |
| `STRIPE_WEBHOOK_SECRET` | – | Signature verification for inbound Stripe events |
| `MINIO_ENDPOINT` etc. | `localhost:9000` | S3-compatible object store |
| `ELASTICSEARCH_URL` | `http://localhost:9200` | Search backend |
| `LLM_MODEL` | `gpt-4o-mini` | Extraction model |
| `EMBEDDING_PROVIDER` | `hash` | `openai` \| `sentence-transformers` \| `hash` |
| `WCP_GRAPHQL_MUTATIONS` | `0` | `1` unlocks GraphQL mutations |
| `SENTRY_DSN` | – | Error tracking (optional) |

Environment variables may be given bare (`REDIS_URL`) or with a `CRAWLER_` prefix
(`CRAWLER_MAX_PAGES`); the prefixed form wins.

---

## 🧪 Testing

```bash
pytest                      # 75 tests
pytest -q tests/test_worker_jobs.py tests/test_saas_billing.py
ruff check src workers tests
```

The suite is fully offline: Redis is faked with `fakeredis`, and the test config sets
`WCP_OFFLINE=1` so the stealth browser ladder refuses to launch a real browser. No
services need to be running.

---

## 🗺️ Roadmap

Done: distributed frontier, job orchestration, stealth/anti-bot, quality pipeline,
multi-tenant REST + GraphQL, quotas, webhooks, marketplace, CI/CD with published images.

Next up:

- [ ] Flesh out the dashboard (currently a live-metrics prototype)
- [ ] Deeper OpenTelemetry tracing across workers
- [ ] Exercise billing end-to-end against real Stripe
- [ ] Split `requirements.txt` into optional extras so images stay small
- [ ] Verify the published GHCR images by running the compose stack

---

## 📝 License

MIT — see the repository for details.

---

**Made with ❤️ by SWADHIN**

⭐ If you find this project useful, please star it on GitHub!
