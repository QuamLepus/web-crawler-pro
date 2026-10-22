# 🕸️ Web Intelligence OS

**Discover. Extract. Understand. Automate.**

Web Intelligence OS builds on the Web Crawler Pro foundation: a self-hostable platform for topic discovery, distributed crawling, data extraction, and search. Its goal is to help users move from a research question to traceable, useful web data—not just submit URLs. The current implementation includes the crawler, topic discovery, APIs, search, an extractive `/ask` endpoint with citations, a tenant-isolated provenance ledger, an early Redis-backed entity graph, early Python and TypeScript SDKs, and an MCP server. It is an evolving product; monitoring supports scheduled one-page snapshots and basic diffs, and research jobs combine discovery, crawl, and cited extracts. Semantic monitoring, independent fact verification, and workflow automation remain incomplete.

It ships in three usable layers, alongside topic discovery and indexed retrieval:

| Layer | What you get | Entry point |
|---|---|---|
| **Single-node crawler** | Scrape a site from your laptop and export CSV/JSON | `python main.py` |
| **Distributed crawl platform** | Celery + Redis frontier, stealth/anti-bot, quality pipeline | `workers/crawl_tasks.py` |
| **Hosted API (SaaS)** | Multi-tenant REST + GraphQL, API keys, quotas, billing, webhooks, marketplace | `src/api/main.py` |
| **Topic discovery** | Give it a *topic* and a number instead of URLs — it finds the websites itself | `python -m src.cli discover` |
| **Grounded intelligence (early)** | Ask against indexed tenant data and receive extractive answers with source citations | `POST /api/v1/ask` |
| **Topic → cited research (early)** | Asynchronous discovery, robots-aware crawl, source excerpts, timestamps, hashes, and per-excerpt citations | `POST /api/v1/research` |
| **Provenance ledger** | Tenant-isolated Redis passports and optional HMAC-signed manifest export | `POST /api/v1/provenance/records` |
| **Knowledge graph (early)** | Tenant-isolated entity/relationship API, exact normalized aliases, and interactive canvas explorer | `POST /api/v1/graph/entities`, `/app/graph` |
| **Web monitoring (early)** | Scheduled, robots-aware one-page crawls; change excerpts plus up to 20 retained text snapshots per watch, `web.changed` webhooks, and watch-only public-DNS pinning | `POST /api/v1/watch` |
| **Python SDK (early)** | HTTPX client for ask, research, crawl jobs, watch APIs, and workflow definitions/runs | `sdks/python/` |
| **TypeScript SDK + MCP (early)** | Typed fetch client and stdio tools for ask, research, crawl, and watches | `sdks/typescript/`, `sdks/mcp/` |

---

## ✨ Feature Matrix

| Capability | Implementation |
|---|---|
| Distributed task queue | **Celery + Redis**, 3 priority queues (`crawl.high/default/bulk`), late-ack, fair dispatch |
| URL Frontier | **Redis**: dedupe (SET), priority (ZSET), per-domain politeness, crash-safe in-flight recovery — atomic Lua pop |
| Horizontal workers | Stateless Celery nodes; scale by starting more pods/processes |
| Job orchestration | Priority scheduler (`crawl_job` / `ingest_source` / `tokenize` / `dedupe` / `discover`) with quota gates |
| Proxy rotation | Weighted tiers: **datacenter / residential / mobile**, failure cooldowns, provider-gateway ready |
| Stealth browsing | **Playwright** (stealth init scripts) + **undetected-chromedriver** fallback |
| TLS fingerprinting | **curl_cffi** Chrome JA3/H2 impersonation (Cloudflare bypass) |
| CAPTCHA solving | **2Captcha / Anti-Captcha** reCAPTCHA v2 (incl. invisible) |
| Topic discovery | **Give a topic + N**: searches the web, ranks sites, crawls the shortlist (`src/discovery/`, `python -m src.cli discover`) |
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
| Observability | **Prometheus** metrics, **Sentry** error reporting, and **OpenTelemetry** API-to-Celery trace propagation |
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
├── requirements.txt                 # backward-compatible full runtime + dev install
├── requirements-api.txt             # API/dashboard service profile
├── requirements-worker.txt          # Celery data-processing profile
├── requirements-ai.txt              # optional AI, vector, and PII integrations
├── requirements-browser.txt         # optional browser/stealth fetching profile
├── requirements-dev.txt             # alias for the full developer/test install
├── requirements-search-index.txt    # minimal own-search-index profile
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
│   ├── discovery/                   # topic → ranked websites: providers, planner, ranker, service
│   ├── ai/
│   │   ├── llm_parser.py            # LangChain schema-driven HTML→JSON extraction
│   │   ├── vector_search.py         # ChromaDB semantic search, pluggable embedders
│   │   └── agents/                  # agent_crawler, data_curator, rag_chain, synthetic
│   ├── api/
│   │   ├── main.py                  # app factory, auth middleware, service container
│   │   ├── graphql.py               # Strawberry schema mounted at /graphql
│   │   └── routers/                 # jobs, discovery, search, webhooks, billing, marketplace
│   ├── gateway/                     # auth/tokens, users, billing, quotas, rate limit, webhooks
│   ├── compute/
│   │   ├── scheduler.py             # priority job scheduler (Celery beat + Redis)
│   │   └── ray_jobs.py, spark_jobs.py
│   ├── marketplace/service.py       # datasets, bounties, purchases, reviews
│   ├── ingestion/                   # RSS / sitemap / API / S3 / CommonCrawl sources
│   ├── training/                    # quality filters, tokenization, MinHash/semantic dedupe
│   ├── observability/metrics.py     # Prometheus counters/gauges/histograms + Sentry init
│   ├── dashboard/app.py             # FastAPI + WebSocket live ops UI
│   ├── webui/                       # the product surface: server-rendered console
│   │   ├── app.py                   # 32 routes, Jinja2, session cookie auth
│   │   ├── client.py                # in-process HTTP client of our own API
│   │   ├── session.py               # httpOnly cookie helpers
│   │   ├── static/app.css           # design system (no CDN, no build step)
│   │   └── templates/               # 18 templates + 2 partials
│   └── utils/logger.py
├── src/cli.py                       # CLI: discover / preview / crawl / status / results
├── workers/crawl_tasks.py           # seed / drain / fetch / process + crawl_domain Celery tasks
├── workers/discovery_tasks.py       # run_discovery / discover_then_crawl Celery tasks
├── deploy/
│   ├── prometheus.yml
│   └── k8s/manifests/               # redis, worker Deployment + HPA, dashboard + Ingress
├── .github/workflows/ci.yml         # test job (Redis service) + matrix image builds
└── tests/                           # 365 tests, fakeredis-backed, no services required
```

---

## 🚀 Setup Guide

Requires **Python 3.11+** (CI runs 3.12).

### 1. Local development (single node)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # full runtime + test/lint dependencies
cp .env.example .env                      # edit as needed
pytest                                    # offline fakeredis-backed suite
python main.py                            # quick crawl of https://example.com
```

### 2. Full stack with Docker Compose

```bash
docker compose up --build -d
# Core services: redis, postgres, minio (:9001 console), elasticsearch (:9200),
#                worker, worker-stealth, celery beat, dashboard (:8080), prometheus
# The crawler CLI is opt-in because starting it performs a sample external crawl:
# docker compose --profile on-demand up --build crawler
# Authenticate with `docker login quay.io` if your MinIO image pull requires it.

# To run the published commit-SHA images instead of building locally, see:
# docs/ghcr-compose.md (:9090)

# Seed the frontier and start crawling:
celery -A src.celery_app call workers.seed_frontier --json='["https://example.com"]'
celery -A src.celery_app call workers.drain_frontier --kwargs='{"batch": 20}'
```

Open **http://localhost:8080** for the live dashboard (WebSocket updates every 2 s).

Prebuilt images are published to GHCR on every merge to `main`, tagged with the
commit SHA:

```bash
docker pull ghcr.io/quamlepus/web-intelligence-os/worker:<sha>
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
pip install -r requirements-browser.txt
playwright install chromium
export PROXY_RESIDENTIAL="http://user:pass@res-gateway:8000"
export CAPTCHA_API_KEY="<2captcha key>" CAPTCHA_PROVIDER=2captcha
```

The `worker-stealth` Docker target bundles everything (browsers + fonts included).

### 5. Enable AI features

```bash
pip install -r requirements-ai.txt
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

`.github/workflows/ci.yml` runs lint + pytest (365 tests) against a real Redis service container on Python 3.12, then builds & pushes all four Docker targets (`worker`, `crawler`, `dashboard`, `worker-stealth`) to GHCR on merges to `main`.

---

## 🌐 Hosted API

The multi-tenant surface lives under `/api/v1` (a legacy `/v1` alias is kept) with a GraphQL
alternative at `/graphql`. Authenticate with a `Bearer` JWT or an `X-API-Key` header.

Run it locally with:

```bash
uvicorn src.api.main:app --host 0.0.0.0 --port 8000
```

### 🖥️ The web console

The same process also serves a **product surface** at `/` — no separate app, no
build step, no CDN:

| Page | What it does |
|---|---|
| `/` `/pricing` | landing page and plans, rendered server-side |
| `/app` | dashboard: quota, recent jobs, index size |
| `/app/search` | query workbench over **your own** index: levels, spelling, reranker |
| `/app/crawl` | crawl jobs, new-job modal, live progress |
| `/app/discovery` | topic → candidate shortlist → approve → crawl |
| `/app/index` | own-index stats, per-source shares, build commands |
| `/app/marketplace` | catalogue of datasets our own crawlers produced, plus bounties |
| `/app/keys` | create and revoke API keys (shown once) |
| `/app/billing` | usage meters, invoice lines, plan comparison |
| `/app/settings` | webhooks, identity, environment |
| `/app/docs` | endpoint reference with a copy-pasteable round trip |

Three rules shape it:

* **It is a client of its own API.** Every page calls the documented endpoints
  with the caller's JWT, so the UI can never drift from the API — anything it
  can do, `curl` can do.
* **The JWT stays out of JavaScript.** It lives in an httpOnly cookie; the
  server swaps it for `Authorization: Bearer` on each call. A browser that has
  to poll for progress does so through `/app/poll/*`, which re-authenticates
  server-side.
* **It works unplugged.** One stylesheet, one script, system fonts. Set
  `WCP_WEBUI=0` (or omit `jinja2`) for a pure-API deployment and `/` turns back
  into the JSON index.
* **It is fast on the wire, not just in Python.** `src/webui/speed.py` gzips
  HTML, answers `If-None-Match` with a bodiless `304`, and marks `/static/*`
  immutable for a year (every reference carries `?v=`, so a change can never be
  shadowed by a cached copy). No response cache: every page shows one tenant's
  own numbers, and a shared cache is one bad key away from showing them to
  somebody else.

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
| Discovery | `POST /v1/discovery`, `/preview`, `GET /v1/discovery`, `GET /v1/discovery/{id}`, `/candidates`, `DELETE /candidates/{domain}`, `POST /{id}/approve`, `GET /{id}/results`, `POST /{id}/cancel` |
| Search | `POST /v1/search`, `GET /v1/search/rag` |
| Webhooks | `GET/POST /v1/webhooks`, `POST /v1/webhooks/test`, `GET /v1/webhooks/{id}/deliveries`, `DELETE /v1/webhooks/{id}` |
| Billing | `GET /v1/billing/plans`, `/usage`, `/invoice`, `POST /v1/billing/checkout` |
| Marketplace | datasets, bounties, submissions, awards, purchases, reviews, deliveries |
| Ops | `GET /healthz`, `/readyz`, `/metrics` |

Free marketplace purchases issue HMAC-signed delivery manifests; configure `MARKETPLACE_SIGNING_SECRET` with at least 32 random bytes before enabling free purchases. Without a valid secret, purchase requests fail with `503` instead of signing with a known fallback key. Paid listings cannot currently be purchased: until payment settlement is integrated, attempts return `503` and create no purchase or delivery. Marketplace reviews can be submitted only for published datasets by a tenant that purchased them, once per dataset; review reads omit buyer tenant IDs and follow listing visibility (published listings, or the authenticated seller's own listings). Bounty submissions must reference a published listing owned by the submitting tenant, and submitting the same listing twice does not inflate the submission count. Only the bounty poster can read the paginated submission IDs. A bounty can be awarded only once by its poster, only while open and unexpired, and only to a submitted dataset that is still published. Open-bounty reads lazily mark past-deadline items as expired and omit them. Listing creation and tag edits normalize case, remove duplicates, and keep tag-search indexes synchronized. Seller listing patches reject unknown fields, invalid lifecycle states, and negative or out-of-range prices; edits to foreign private listings return 404 to match read visibility.

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
     → BFS within the start domain, fragment-only URL variants deduplicated,
       robots.txt honoured, configured delay between pages
     → per page: clean → dedupe → store
     → live progress written to wcp:v1:job:{id}
     → on finish: data_sha256 stamped, crawl.completed webhook emitted
```

Because the digest is written to the job record, the owning tenant can publish a
completed crawl to the marketplace with `source_job_id`. Publication rejects missing
or foreign-tenant jobs and non-completed crawls; digest and crawl counters are read
from that tenant-scoped job record. Job status, crawl results, and the completion
webhook also report `robots_blocked`, the number of queued URLs skipped because
robots policy denied access or could not be retrieved.

Crawl-job cancellation is cooperative: an in-flight page is allowed to finish,
but the worker stops before starting another page and preserves the cancelled
state. A `crawl.cancelled` webhook is emitted when the crawl job is cancelled.
For non-watch crawls, HTTP responses with status 400 or higher count as failed
page attempts and are not stored as crawl items; watch jobs retain status pages
for change monitoring.

The general crawler permits a target when `robots.txt` returns a 4xx response,
consistent with the Robots Exclusion Protocol's unavailable-file handling. A
5xx response or network/request failure blocks that target until the robots
policy can be retrieved; successfully fetched rules are enforced normally. Each
crawl job retrieves one robots policy per origin and applies it to every path.
Where `robots.txt` provides a supported `Crawl-delay`, job-backed crawls wait
for at least the greater of that delay and the configured `DELAY` interval
after retrieving the policy and between page fetches. Direct HTTP crawls cap
response bodies at 5 MiB; oversized pages are not stored. Non-stealth direct
fetches (including robots.txt) pin connections to validated public addresses
and revalidate redirects. Stealth browser fetches still require operator-level
egress restrictions. The crawler retains response media types, parses links
only from HTML, and stores text excerpts for recognized text and JSON/XML
responses rather than mislabeling them as HTML.

---

## 🔍 Topic Discovery

**Give the crawler a topic and a number instead of a list of URLs.** It searches
the web, ranks what it finds into a shortlist of distinct websites, and crawls
each one through the normal pipeline.

```bash
python -m src.cli discover \
    --topic "best budget laptops 2026" \
    --count 25 \
    --seed https://www.rtings.com \
    --max-pages 8 \
    --auto
```

```
Topic discovery
  topic      : best budget laptops 2026
  websites   : 25
  seeds      : https://www.rtings.com
  pages/site : 8
  mode       : automatic

[1/5] Searching the web and ranking what we find
[2/5] Shortlist built  25 of 214 candidates
[3/5] Shortlist  25 websites

  1. 0.91  rtings.com      [seed, duckduckgo+brave]
     https://www.rtings.com/laptop/reviews/best/budget
     seeded by user; found on 2 provider(s); relevance 0.88; passed quality rules
  2. 0.83  nytimes.com     [duckduckgo+brave]
     ...

[4/5] Crawling  23/25 sites, 187 pages
     ok rtings.com                              22 pages    21 items
     ok nytimes.com                              9 pages     9 items
     !! parked-domain.example                    0 pages     0 items  fetch failed: 403
[5/5] Report  312 items in 41.2s

✓ 312 items from 23/25 websites
   output/discovery/disc_a1b2c3d4e5f/report.json
   output/discovery/disc_a1b2c3d4e5f/items.jsonl
```

Leave out `--auto` to stop at the shortlist and review it first; add `--approve`
once you are happy with it.

### How it works

```
topic ──plan──▶ search queries ──search──▶ raw hits ──rank──▶ top-N websites ──crawl──▶ dataset
```

1. **Plan** — one topic becomes several queries (`"best budget laptops 2026"`,
   `+ "review"`, `+ "buying guide"`, …). LLM-generated when `DISCOVERY_USE_LLM=true`,
   deterministic templates otherwise.
2. **Search** — every query fans out to every configured provider.
3. **Rank** — candidates are scored, explained and de-duplicated to one entry
   point per domain (that is what "N websites" means).
4. **Review** *(optional)* — the shortlist is human-readable and editable.
5. **Crawl** — one ordinary `crawl_job` per website, so robots.txt, politeness,
   cleaning, dedupe, PII redaction and storage all behave exactly as they do for
   a manually submitted crawl.

### Scoring

| Signal | Weight | Where it comes from |
|---|---|---|
| Relevance | 0.40 | Cosine similarity, topic vs. title + snippet (pluggable embeddings, `hash` by default) |
| Lexical | 0.20 | Keyword overlap via `HeuristicPlanner` — the same scorer the autonomous agent uses |
| Cross-provider agreement | 0.20 | Independent search engines agreeing beats one engine ranking something first |
| Position | 0.10 | Decay within each provider's result list |
| Quality | 0.10 | C4 rules (`src/training/filters.py`): lorem-ipsum, cookie walls, symbol soup |
| Seed boost | +0.15 | A domain you supplied explicitly always outranks a guess |

Every candidate ships with the reasons behind its score, because "the best N
websites for a topic" is a judgement call and you are entitled to override it.

### Search providers

| Provider | Key | Notes |
|---|---|---|
| **`own`** | – | **The platform's own index** (see below). No key, no rate limit, works offline. Registered automatically once an index exists. |
| `duckduckgo` | – | Default. No key, no cost. Good for personal / low volume. |
| `brave` | `BRAVE_API_KEY` | Licensed, free tier. **Use this in production.** |
| `serpapi` | `SERPAPI_KEY` | Google/Bing results as JSON |
| `tavily` | `TAVILY_API_KEY` | Search API built for LLM agents |
| `commoncrawl` | – | Historical coverage; matches a URL regex, not keywords |
| `llm` | `OPENAI_API_KEY` | Asks a model which sites matter (model knowledge) |
| `seed` | – | Your own `--seed` URLs, expanded via `/sitemap.xml` |

Providers with a key are enabled automatically; list them explicitly with
`DISCOVERY_PROVIDERS=duckduckgo,brave`.

> **ToS:** scraping Google or Bing result pages directly violates their terms and
> gets you blocked. The default DuckDuckGo HTML endpoint is fine for light use;
> for anything at scale use a licensed API (Brave / SerpAPI / Tavily) — or build
> your own index with `src.search_index` and stop depending on anybody.

### REST API

```bash
# 1. start a discovery job (202 Accepted — a worker picks it up)
curl -X POST localhost:8000/v1/discovery -H "X-API-Key: $KEY" \
     -H 'content-type: application/json' \
     -d '{"topic":"best budget laptops 2026","count":25,
          "seeds":["https://www.rtings.com"],"auto":false}'
# -> {"job_id":"disc_a1b2c3d4e5f","state":"queued", ...}

# 2. read the ranked shortlist (state becomes awaiting_approval)
curl localhost:8000/v1/discovery/disc_a1b2c3d4e5f/candidates?only_selected=true \
     -H "X-API-Key: $KEY"

# 3. drop one, then approve
curl -X DELETE localhost:8000/v1/discovery/disc_a1b2c3d4e5f/candidates/spam.example \
     -H "X-API-Key: $KEY"
curl -X POST localhost:8000/v1/discovery/disc_a1b2c3d4e5f/approve -H "X-API-Key: $KEY"

# 4. aggregated report
curl localhost:8000/v1/discovery/disc_a1b2c3d4e5f/results -H "X-API-Key: $KEY"
```

`POST /v1/discovery/preview` does the ranking synchronously and returns the
shortlist **without** crawling anything or creating a job — useful for tuning a
topic before committing to it.

A discovery job is charged `count` requests against the tenant quota up front, so
a large shortlist cannot be used to bypass the crawl budget.

### Degradation

Nothing here is a hard dependency. With no API keys, no LLM and no network the
whole flow still runs — it simply finds fewer candidates. Ranking falls back to
lexical scoring, embeddings fall back to the offline hash embedder, and a dead
provider is skipped with a warning rather than failing the job.

---

## 🔎 Own Search Index — drop the third-party search engine

Topic discovery used to begin with a request to somebody else's search engine.
**`src.search_index`** removes that dependency: it builds a BM25 index over free,
downloadable datasets and answers topic queries from it. No API key, no quota, no
per-query cost, and it keeps working with the network unplugged.

The trick that makes this cheap is that **ranking does not require crawling**.
Three public datasets already carry the signals a crawler would otherwise have to
rediscover the hard way:

| Dataset | What it gives us | Size |
|---|---|---|
| [Curlie](https://curlie.org/download) (CC-BY) | ~2.9M **human-edited** sites, each with a written description *and* a category path like `Top/Science/Energy/Solar` | ~200 MB |
| [Open PageRank](https://www.domcop.com/openpagerank/) | Authority 0–10 for the top 10M domains — we never build a link graph | 117 MB |
| [Tranco](https://tranco-list.eu/) | Community-merged popularity list, catches what PageRank misses | ~15 MB |

Put together that is **~330 MB of download** and it already beats a general
search engine *for this narrow job*: an editor's taxonomy matches a topic
directly (no SEO sludge), there is no rate limit and there is no terms-of-service
question to answer.

### The $0 roadmap

| Level | What gets indexed | Download | Disk | Effort |
|---|---|---|---|---|
| **0** | Curlie + Open PageRank + Tranco | ~330 MB | ~3 GB | 2–3 days |
| **1** | + Wikipedia (7.2M articles) | ~27 GB | ~40 GB | 3–5 days |
| **2** | + Common Crawl, 10M pages | ~55 GB | ~150 GB | 1–2 weeks |
| **3** | + Common Crawl, 100M pages | ~310 GB | 0.5–1 TB | 1–2 months |

Compute is free the whole way: **GitHub Actions gives public repositories
unlimited minutes**, so levels 2 and 3 run as a sharded matrix —
`.github/workflows/search-index.yml` fans out 20 parallel jobs, each building an
independent part, then merges them. Past level 3 the cost is disk and servers,
not software.

### Quick start

```bash
pip install -r requirements-search-index.txt      # numpy + requests + dotenv

python -m src.cli search-index levels             # what each level costs
python -m src.cli search-index sources --level 2  # exactly what gets downloaded
python -m src.cli search-index build --level 0    # ~330 MB; Ctrl-C is safe
python -m src.cli search-index status
python -m src.cli search-index query "renewable energy research" --limit 10
```

Then point discovery at it and DuckDuckGo is out of the loop:

```bash
export SEARCH_INDEX_DIR=data/search-index
export DISCOVERY_PROVIDERS=own
python -m src.cli discover --topic "solar energy research" --count 20 --approve
```

Already have the datasets? Drop them into `$SEARCH_INDEX_DIR/raw/` and add
`--no-download`. Downloads resume via HTTP `Range`, and the build checkpoints
`state.json` after **every shard**, so a job killed at 5h55m restarts on the next
shard instead of the next day.

### Query syntax

```
solar panels                    # any of the words, ranked by BM25 × authority
"exact phrase"                  # phrase, verified against the document
solar -blog                     # exclude
site:.edu                       # host suffix (site:example.org also works)
source:wikipedia                # curlie | wikipedia | commoncrawl | seed
```

Ranking is `bm25 × (1 + authority_weight × authority/10)`, which is why "python"
returns python.org instead of a scraper farm. Results are collapsed to **one per
website** by default, because discovery wants domains, not pages.

### HTTP surface

```bash
curl "localhost:8000/v1/search-index/query?q=solar%20energy&limit=5" -H "X-API-Key: $KEY"
curl "localhost:8000/v1/search-index/spell?q=solr%20energi"          -H "X-API-Key: $KEY"
curl "localhost:8000/v1/search-index/stats"                          -H "X-API-Key: $KEY"
curl "localhost:8000/v1/search-index/levels"                          -H "X-API-Key: $KEY"
```

A corrected query comes back as `corrected_query` + `corrections`, so the caller
can show "showing results for **solar energy**" instead of silently rewriting
what the user typed.

### Three optional layers on top of BM25

BM25 is lexical: it cannot know that *cars* and *automobiles* are the same
thing. These three layers are **off by default** (or free), and each one fails
soft — with the model missing, the code says so and carries on.

**1. Semantic reranking** — BM25 shortlists, an encoder reorders.

```bash
pip install sentence-transformers                       # optional, ~90 MB model
python -m src.cli search-index embeddings --data-dir data/search-index
python -m src.cli search-index query "cars" --rerank embedding
```

| `SEARCH_INDEX_RERANK` | What it does |
|---|---|
| `none` (default) | nothing — BM25 + authority, zero cost |
| `embedding` | bi-encoder cosine on the fly; `all-MiniLM-L6-v2`, 384-d |
| `cross-encoder` | sharper and slower (`ms-marco-MiniLM-L-6-v2`) |

Both signals are min-max normalised *inside the candidate set* (BM25 scores are
unbounded and differ by orders of magnitude between shards) and blended as
`(1-w)·bm25 + w·semantic`, with `w = SEARCH_INDEX_RERANK_WEIGHT` (0.35). If
`sentence-transformers` is not installed the reranker switches **itself off**
rather than pretending — and it refuses the built-in hashing fallback, because
that fallback is itself lexical and would only re-measure BM25.

`build --embeddings` pre-computes vectors per shard (`docs × 384 × 4` bytes,
~154 MB per 100k docs) so reranking is a memory-mapped lookup instead of a model
call. Merging parts renumbers every `rowid`, so the merge step deletes stale
vectors — rebuild them on the merged index.

**2. Spelling correction** — trained on our own corpus.

```bash
python -m src.cli search-index spell --check "kubernets,reserch"
```

The dictionary is the index's **own vocabulary**, not a generic English word
list. That matters: a standard dictionary "corrects" `kubernetes`, `tranco` and
`duckduckgo` into something worse. Here, `df.npy` — the document frequency we
already compute for BM25 — is the frequency, and a term is only rewritten when
it is unknown, ≥4 characters, digit-free, and the candidate is ≥8× more
frequent. Query operators (`site:`, `source:`, `-exclude`) and quoted phrases
are never touched.

The vocabulary is stored as *stems* (that is what BM25 indexes), so each shard
also writes a `surface.json` mapping every stem back to the word people
actually write — collected as a by-product of the tokenisation the build is
already doing. Without it the checker answers `kubernet` instead of
`kubernetes`: fine for matching, wrong to show anyone.

**3. Incremental refresh** — re-index only what changed.

```bash
python -m src.cli search-index refresh --snapshot CC-MAIN-2026-40
```

Every URL is hashed (blake2b-64) and stored with a hash of the **indexed text**
(title + snippet + first N body characters), not of the whole page — so a
footer-only change counts as unchanged. On a refresh:

| | |
|---|---|
| `unchanged` | skipped entirely — not re-tokenised, not re-inverted |
| `changed` | `UPDATE`d in place, keeping the `rowid` the shards point at |
| `new` | inserted as usual |

The owning shards are marked dirty and rebuilt at the end, so a refresh touches
a handful of shards instead of all of them.

**The honest limit:** Common Crawl has no "changed since" API. The *download*
cost of a refresh is still a full snapshot — it is storage, indexing and shard
rebuilds that get cheaper, not bandwidth.

### Keeping it fresh

```bash
python -m src.cli search-index refresh --snapshot CC-MAIN-2026-40   # only what changed
python -m src.cli search-index spell                                # retrain after new words
```

The monthly scheduled workflow does exactly this: it restores the previous index
from the Actions cache, refreshes, retrains the dictionary and uploads a new
artifact.

### How it is built

| Layer | File | Job |
|---|---|---|
| Sources | `src/search_index/sources/` | streaming readers for Curlie TSV, PageRank/Tranco CSV, Wikipedia multistream bz2, Common Crawl WET |
| Store | `src/search_index/store.py` | SQLite — stdlib only, random access by `rowid`, `url UNIQUE` gives de-duplication for free |
| Index | `src/search_index/bm25.py` | per-shard Okapi BM25 in CSR form, memory-mapped, top-k merged across shards like a real distributed engine |
| Build | `src/search_index/indexer.py` | resumable, checkpointed, sharded |
| Merge | `src/search_index/merge.py` | combines parallel parts, remapping `rowid`s and dropping duplicates |
| Query | `src/search_index/query.py` | parse → correct → retrieve → verify → blend → rerank → collapse |
| Rerank | `src/search_index/rerank.py` | bi-encoder / cross-encoder second stage, plus pre-computed shard vectors |
| Spell | `src/search_index/spell.py` | reduced SymSpell over the index vocabulary |
| Incremental | `src/search_index/incremental.py` | SQLite content-hash tracker: new / unchanged / changed |
| Provider | `src/search_index/provider.py` | `OwnIndexProvider`, a drop-in `SearchProvider` |
| Tokeniser | `src/search_index/text.py` | `analyze()` — the hot loop; see below |

### The tokeniser's optional C fast path

`analyze()` runs once per indexed document, and a Common Crawl snapshot is
billions of documents. `src/search_index/wcpfast.c` is the same loop in C,
called through `ctypes`:

```bash
./scripts/build-native.sh          # needs a C compiler; entirely optional
python scripts/bench-tokenizer.py  # proves the two paths agree, then measures
```

Measured on this tree: **4.3–4.5× faster** (~18k → ~80k documents/second).
Three properties matter more than the multiplier:

* **It cannot change an answer.** The library is ASCII-only. The first byte
  above `0x7F` makes it decline and Python does the work, so Bengali, Arabic and
  accented Latin keep the Unicode-correct path and the results they always had.
* **It cannot drift.** The stopword set and the suffix rules are handed to it
  from `text.py` at configure time — there is no second copy to fall out of
  sync.
* **It is genuinely optional.** No compiler, no file, or a decline: every call
  returns `None` and the Python path runs. `tests/test_native_tokenizer.py`
  checks the fallback unconditionally and runs the comparison only when the
  library has been built.

A plain C ABI rather than a CPython extension module, because that needs no
Python headers at build time (slim images without `python3-dev` work fine) and
no rebuild per interpreter version.

**Zero new hard dependencies** — numpy (already present) and the standard
library. Phrases are verified against stored text instead of indexed with
positions (positions would double the index), Curlie's category path is indexed
as text so a topic matches an editor's taxonomy, and authority comes from someone
else's link graph instead of one we have to build.

### Honest limits

- Curlie is 2.9M sites: excellent head, no long tail, and new sites appear only
  when an editor adds them.
- Curlie + Common Crawl freshness is **monthly**, not real-time.
- GitHub Actions is free but subject to a fair-use policy and a 6-hour job cap.
- Level 3 needs ~1 TB of disk somewhere; "free" stops at hardware.
- This is optimised to work well *inside this platform* — find authoritative,
  on-topic pages for a topic — not to be a general web search engine.

---

---

## ⚙️ Configuration Reference

See **`.env.example`** for the complete list. Highlights:

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Frontier + Celery broker/backend |
| `FRONTIER_NAMESPACE` | `frontier` | Key prefix (multi-tenant frontiers) |
| `JWT_SECRET` | – | **Required.** Signing key for auth tokens |
| `WCP_PUBLIC_BASE_URL` | – | Public HTTPS origin used for OIDC callback URLs |
| `WCP_SSO_ENCRYPTION_KEY` | – | Fernet key required to configure enterprise OIDC SSO |
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
| `DISCOVERY_PROVIDERS` | `duckduckgo` | Comma-separated search backends: `duckduckgo,brave,serpapi,tavily,commoncrawl,llm` |
| `BRAVE_API_KEY` / `SERPAPI_KEY` / `TAVILY_API_KEY` | – | Enables that provider automatically when set |
| `DISCOVERY_MAX_CANDIDATES` | `300` | Upper bound on scored candidates per job |
| `DISCOVERY_MAX_QUERIES` | `6` | Search queries generated per topic |
| `DISCOVERY_MAX_PAGES_PER_SITE` | `5` | Crawl budget per discovered website |
| `DISCOVERY_BLOCKLIST` | – | Comma-separated domains that must never be proposed |
| `DISCOVERY_USE_LLM` / `DISCOVERY_USE_COMMONCRAWL` | `false` | Opt into the LLM / CommonCrawl discovery backends |
| `DISCOVERY_USE_OWN_INDEX` | `true` | Auto-register `OwnIndexProvider` when an index exists |
| `SEARCH_INDEX_DIR` | `data/search-index` | Where the own index lives (`docs.sqlite3`, `index/`, `raw/`) |
| `SEARCH_INDEX_LEVEL` | `0` | 0 Curlie · 1 +Wikipedia · 2 +10M CC pages · 3 +100M CC pages |
| `SEARCH_INDEX_SHARD_DOCS` | `100000` | Documents per BM25 shard |
| `SEARCH_INDEX_BODY_CHARS` | `512` | How much body text BM25 indexes |
| `SEARCH_INDEX_AUTHORITY_WEIGHT` | `0.35` | `score = bm25 × (1 + weight × authority/10)`; `0` = pure BM25 |
| `SEARCH_INDEX_COLLAPSE_HOST` | `true` | One result per website |
| `SEARCH_INDEX_MAX_PAGES` | `0` | Common Crawl page budget override (`0` = level default) |
| `SEARCH_INDEX_SOURCES` | – | Comma-separated override, e.g. `curlie,wikipedia` |
| `SEARCH_INDEX_RERANK` | `none` | `none` \| `embedding` \| `cross-encoder` — second-stage reranking |
| `SEARCH_INDEX_RERANK_WEIGHT` | `0.35` | `0` = ignore semantics, `1` = semantics only |
| `SEARCH_INDEX_RERANK_CANDIDATES` | `100` | How many BM25 hits the reranker sees |
| `SEARCH_INDEX_EMBED_MODEL` | `all-MiniLM-L6-v2` | Encoder used by the reranker |
| `SEARCH_INDEX_EMBEDDINGS` | `false` | Pre-compute vectors at build time (faster reranking, ~154 MB/100k docs) |
| `SEARCH_INDEX_SPELL` | `true` | Typo correction against the index vocabulary |
| `SEARCH_INDEX_SPELL_MAX_TERMS` | `30000` | Cap on the spelling dictionary |
| `SEARCH_INDEX_INCREMENTAL` | `true` | Skip unchanged pages when re-ingesting a crawl |
| `SENTRY_DSN` | – | Error tracking (optional) |

Environment variables may be given bare (`REDIS_URL`) or with a `CRAWLER_` prefix
(`CRAWLER_MAX_PAGES`); the prefixed form wins.

---

## 🧪 Testing

```bash
pytest                                          # 365 tests
pytest -q tests/test_search_index.py           # own search index, fully offline
pytest -q tests/test_search_index_advanced.py  # rerank + spell + incremental refresh
pytest -q tests/test_native_tokenizer.py       # C tokeniser vs Python, must agree
pytest -q tests/test_webui.py                  # console, driven like a browser
pytest -q tests/test_worker_jobs.py tests/test_saas_billing.py
ruff check src workers tests
```

The suite is fully offline: Redis is faked with `fakeredis`, and the test config sets
`WCP_OFFLINE=1` so the stealth browser ladder refuses to launch a real browser. No
services need to be running.

---

## 🗺️ Roadmap

Done: distributed frontier, job orchestration, stealth/anti-bot, quality pipeline,
multi-tenant REST + GraphQL, quotas, webhooks, marketplace, topic discovery,
**own search index (Curlie + PageRank + Wikipedia + Common Crawl, $0)**,
CI/CD with published images.

Next up:

- [x] Flesh out the dashboard — the server-rendered console at `/` replaces the live-metrics prototype
- [x] Semantic (vector) reranking on top of the own index; degrades to BM25 when no encoder is installed
- [x] Spelling correction learned from the indexed corpus itself (SymSpell-style)
- [x] Incremental Common Crawl updates: index only what changed since the last snapshot
- [x] Deeper OpenTelemetry tracing across workers — W3C context propagation and task spans; see [`docs/observability.md`](docs/observability.md)
- [ ] Complete real Stripe end-to-end validation — opt-in test-mode Checkout test is available; see [`docs/stripe-testing.md`](docs/stripe-testing.md)
- [x] Split Python dependencies into service profiles and optional AI/browser groups; see [`docs/dependency-profiles.md`](docs/dependency-profiles.md)
- [ ] Verify the full published GHCR Compose stack — CI smoke-tests the published dashboard + Redis; see [`docs/ghcr-compose.md`](docs/ghcr-compose.md)

---

## Frontend performance and motion targets

The current web console remains FastAPI + Jinja2 with local static assets and no
required CDN. These are **targets, not measured results**: cached navigation
100–200 ms; ordinary read API p95 below 200 ms; LCP below 2.5 s (the more
aggressive sub-1 s goal requires a separately specified test profile); CLS below
0.1; animation near 60 FPS; and near-real-time job progress. Establish and
publish repeatable browser/device/network conditions before claiming a target
is met. Respect reduced-motion preferences; motion should remain brief and
nonessential. A synthetic own-index benchmark is available at
`scripts/bench-search-index.py`; its small sandbox baseline and caveats are
recorded in [`docs/performance-benchmarks.md`](docs/performance-benchmarks.md).

## Grounded intelligence and provenance (early building blocks)

The authenticated `POST /api/v1/ask` endpoint searches the tenant's existing
index and returns extractive answers with source citations. It does not launch
an implicit crawl. The request's page/time budget is reserved for future
retrieval expansion; cost is reported as unavailable, and confidence is a
heuristic coverage indicator rather than calibrated probability. See
[`docs/intelligence-provenance.md`](docs/intelligence-provenance.md) for a curl
example and limits. See [`docs/roadmap-status.md`](docs/roadmap-status.md) for
an honest implemented/partial/not-yet-implemented matrix.

The provenance API stores caller-supplied passports in tenant-isolated Redis and
can export a signed manifest when `WCP_PROVENANCE_SIGNING_KEY` is configured.
It does not retroactively annotate older records or infer legal permissions;
unknown license and robots states remain explicit. HMAC is an integrity
mechanism, not an independent public signature.

The early Redis-backed knowledge graph supports entities, exact normalized
alias matching, and relationships. Scheduled URL monitoring and extractive
multi-stage research jobs are available with Celery workers/Beat; reports are
not independently fact-checked. Data Studio supports full-corpus, allow-listed
aggregates (with filters, charts, pagination, and CSV export) as well as bounded
search analysis, saved queries, and 90-day immutable snapshots of up to 100 hits;
it does not expose arbitrary SQL or export raw full-corpus documents.
The tenant-scoped workflow builder supports legacy ordered recipes plus bounded
step-dependency DAGs (up to 10 steps), cooperative cancellation, and recurring
runs when Celery Beat is active. DAG steps run serially; failures skip dependent
steps while independent branches continue. It is not a parallel DAG or script
engine. Early Python/TypeScript SDK and stdio MCP packages expose workflow
operations; TypeScript/MCP also expose selected snapshot operations. They are
not yet published as stable releases.
Tenant administrators can configure early OIDC SSO with PKCE, encrypted
client secrets, verified-domain provisioning controls, and an SSO-only audit
trail; it is not a complete enterprise identity/compliance offer. See
[`docs/enterprise-sso.md`](docs/enterprise-sso.md) for prerequisites and limits.

These foundations do not constitute a complete research agent, BI suite, or
enterprise SSO/connectors/compliance offering. Performance figures remain
synthetic baselines and targets until validated under production conditions.

---

## 📝 License

MIT — see the repository for details.

Third-party data used by `src/search_index`, all free to redistribute under the
terms below:

- **Curlie** (https://curlie.org) — [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/),
  the successor to the DMOZ/Open Directory Project.
- **Wikipedia** (https://dumps.wikimedia.org) — [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
- **Common Crawl** (https://commoncrawl.org) — public dataset on an AWS Open Data
  bucket; no egress charge, no API key.
- **Open PageRank** (https://www.domcop.com/openpagerank/) — free for any use.
- **Tranco** (https://tranco-list.eu/) — free for research and commercial use.

---

**Made with ❤️ by SWADHIN**

⭐ If you find this project useful, please star it on GitHub!
