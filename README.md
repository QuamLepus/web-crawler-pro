# 🕷️ Web Crawler Pro

**Enterprise-grade, cloud-native, distributed web scraping & data-ingestion platform** — a self-hosted alternative to Scrapy Cloud / Zyte with stealth fetching, quality engineering, AI-powered extraction, **and a full multi-tenant SaaS layer** (auth gateway, billing, GraphQL API, data marketplace) plus a **trillion-token LLM training pipeline**.

---

## ✨ Feature Matrix

| Capability | Implementation |
|---|---|
| Distributed task queue | **Celery + Redis**, 3 priority queues (`crawl.high/default/bulk`), late-ack, fair dispatch |
| URL Frontier | **Redis**: dedupe (SET), priority (ZSET), per-domain politeness, crash-safe in-flight recovery — atomic Lua pop |
| Horizontal workers | Stateless Celery nodes; scale by starting more pods/processes |
| Proxy rotation | Weighted tiers: **datacenter / residential / mobile**, failure cooldowns, provider-gateway ready |
| Stealth browsing | **Playwright** (stealth init scripts) + **undetected-chromedriver** fallback |
| TLS fingerprinting | **curl_cffi** Chrome JA3/H2 impersonation (Cloudflare bypass) |
| CAPTCHA solving | **2Captcha / Anti-Captcha** reCAPTCHA v2 (incl. invisible) |
| Data cleaning | Unicode/whitespace normalization, URL canonicalization, ISO timestamps (**Pandas + Polars**) |
| Deduplication | **MinHash** (shared Redis index) + **SimHash** (Hamming ≤ 3) |
| Language detection | **fastText** `lid.176` with heuristic fallback (env resolved lazily — no import-time capture) |
| PII redaction | **Presidio** analyzer/anonymizer + always-on regex fallback → canonical `[REDACTED_*]` tokens |
| Storage | JSONL, CSV, partitioned **Parquet** → **MinIO/S3**, **Elasticsearch** bulk indexing, tiered lake lifecycle |
| REST API | FastAPI, canonical mount `/api/v1/*` (+ `/v1/*` legacy alias): jobs · search · webhooks · billing · marketplace routers |
| GraphQL | **Strawberry** single endpoint via `make_graphql_router()` (schema registers the `ID` scalar) |
| Auth / tenancy | OAuth2 **JWT** (rotation via `JWT_OLD_SECRETS`) + **API keys**; per-tenant context middleware |
| Rate limiting | Per-key Redis sliding windows, plan-aware limits |
| Billing | **Stripe** sync, plans/invoices/quota metering (`QuotaExceededError` → structured 402/429) |
| Webhooks | Signed delivery, retry semantics, tenant-scoped endpoints |
| Data Marketplace | Listings, previews, reviews, bounties, purchases with **HMAC-signed delivery manifests** + SHA-256 content digests |
| AI parsing | **LangChain LLM parser** — structured JSON from raw HTML, zero CSS selectors |
| Semantic search | **ChromaDB** vectors + pluggable embeddings (OpenAI / sentence-transformers / offline hash) |
| AI agents | Agentic crawler (LLM link planning + budget guard), **RAG chain** (cited answers, extractive fallback), **data curator** (LLM-as-judge quality scoring), **synthetic generator** (QA / paraphrase / Self-Instruct) |
| Ingestion | Data-source registry (RSS/Atom, sitemap, paginated APIs, S3, Common Crawl, website seeds) + CC & HF-datasets downloaders |
| Compute | Priority job scheduler (Celery beat schedules in Redis, quota-aware enqueue), **Ray** and **Spark** job adapters |
| Training | Trillion-token pipeline: toxicity/quality filters, MinHash + semantic dedupe, tokenizer, **WebDataset shards** with sequence packing (idempotent, resumable) |
| Dashboard | FastAPI + **WebSocket** live metrics, frontier depth, throughput |
| Observability | **Prometheus** client metrics + **Sentry** error tracking & tracing |
| DevOps | Multi-stage Dockerfiles, docker-compose full stack, GitHub Actions CI/CD, **Kubernetes** manifests + HPA |

---

## 🏗️ System Architecture

```mermaid
flowchart TB
    subgraph SaaS["SaaS Layer"]
        GW[API Gateway<br/>JWT + API keys · rate limit · quotas]
        REST[/api/v1 REST<br/>jobs · search · webhooks · billing · marketplace/]
        GQL[/GraphQL · Strawberry/]
        BILL[Stripe billing<br/>plans · invoices · metering]
        MKT[Data Marketplace<br/>listings · bounties · signed manifests]
        GW --- REST & GQL
        GW --- BILL & MKT
    end

    subgraph Control["Control Plane"]
        SEED[Seed API / CLI] -->|seed URLs| FR
        BEAT[Celery Beat scheduler] -->|drain_frontier| BROKER
        REG[Source Registry<br/>RSS · sitemap · API · S3 · CC] --> SCH[Priority Scheduler]
        SCH --> BROKER
    end

    subgraph Dist["Distributed Crawling Layer"]
        FR[(Redis URL Frontier<br/>dedupe · priority · politeness)]
        BROKER[(Celery Broker<br/>Redis queues)]
        FR -->|pop batch| W0[drain task]
        W0 --> BROKER
        BROKER --> W1[Worker pool<br/>fetch_page ×N]
        BROKER --> W2[Stealth worker<br/>playwright / curl_cffi]
        W1 & W2 -->|links| FR
    end

    subgraph Anti["Anti-Bot Middleware"]
        PX[Proxy rotation<br/>DC · Residential · Mobile]
        CAP[CAPTCHA solver<br/>2Captcha / Anti-Captcha]
        TLS[TLS fingerprint<br/>curl_cffi impersonation]
    end
    W1 -.-> PX & TLS
    W2 -.-> PX & CAP & TLS

    subgraph Pipe["Data Pipeline & Quality"]
        W1 & W2 --> CLN[Cleaning / normalization<br/>Pandas · Polars]
        CLN --> DEDUP[Dedupe<br/>MinHash · SimHash]
        DEDUP --> LANG[Language detect fastText<br/>PII redact Presidio]
        LANG --> ST{Storage fan-out}
    end

    subgraph Store["Storage Layer"]
        ST --> JS[JSONL / CSV]
        ST --> PQ[(Parquet lake<br/>→ MinIO S3 · tiering)]
        ST --> ES[(Elasticsearch)]
        ST --> VEC[(ChromaDB vectors)]
    end

    subgraph AI["AI Layer"]
        LLM[LangChain Intelligent Parser<br/>HTML → structured JSON]
        AGT[Agentic Crawler]
        RAG[RAG Chain<br/>retrieval + citations]
        CUR[Data Curator<br/>LLM-as-judge DQS]
        SYN[Synthetic Generator]
        SEM[Semantic search API]
        VEC --> SEM & RAG
    end

    subgraph Train["Training / Compute Layer"]
        INGSRC[Ingestion<br/>Common Crawl · HF datasets]
        TP[Tokenize → filter → dedupe<br/>→ WebDataset shards]
        RAY[Ray jobs] & SPARK[Spark jobs]
        INGSRC --> TP
        TP --> RAY & SPARK
    end
    ST --> INGSRC
    CUR --> TP

    subgraph Obs["Observability"]
        PROM[(Prometheus)] --- GRAF[Grafana]
        SENTRY[Sentry]
        DASH[Live Dashboard<br/>FastAPI + WebSockets]
    end
    FR & BROKER -->|metrics| PROM
    W1 & W2 -->|errors| SENTRY
    DASH <--> FR
    GW --> REST & GQL
```

---

## 📁 Project Structure

```
web-crawler-pro/
├── main.py                          # single-node CLI entry point
├── requirements.txt                 # layered deps (core / optional extras)
├── .env.example                     # every tunable, documented
├── Dockerfile                       # multi-stage: worker | crawler | dashboard | worker-stealth
├── docker-compose.yml               # full stack: Redis, Postgres, MinIO, Elastic, workers, beat, dashboard, Prometheus
├── src/
│   ├── celery_app.py                # Celery app, queues, reliability config
│   ├── config.py                    # env-driven singleton
│   ├── api/                         # ── SaaS layer ──
│   │   ├── main.py                  # FastAPI app factory; canonical /api/v1 mount + /v1 alias,
│   │   │                            #   GraphQL mounted once via make_graphql_router()
│   │   ├── graphql.py               # Strawberry schema (Job/Query/Mutation; ID scalar registered)
│   │   ├── routers/                 # jobs · search · webhooks · billing · marketplace
│   │   ├── middleware.py            # auth-context middleware (tenant scoping)
│   │   ├── observability.py         # process-time headers, request metrics
│   │   └── schemas.py
│   ├── gateway/                     # ── API gateway / tenancy ──
│   │   ├── app.py                   # standalone gateway app (OAuth2 + API key + rate limit)
│   │   ├── auth/                    # JWT tokens (secret rotation), user store
│   │   ├── billing/                 # plans, quota metering, Stripe sync
│   │   ├── ratelimit/limiter.py     # per-key Redis sliding windows
│   │   └── webhooks/service.py      # signed webhook delivery
│   ├── marketplace/service.py       # listings, previews, reviews, bounties,
│   │                                #   purchases w/ HMAC-signed delivery manifests
│   ├── crawlers/basic_crawler.py    # sync core crawler
│   ├── frontier/url_frontier.py     # Redis frontier (Lua-atomic pop, politeness, ack/nack)
│   ├── middleware/
│   │   ├── proxy_rotation.py        # weighted tier proxies + cooldowns
│   │   ├── stealth_middleware.py    # curl_cffi → playwright → undetected-chromedriver
│   │   └── captcha_solver.py        # 2Captcha / Anti-Captcha
│   ├── pipeline/
│   │   ├── cleaning.py              # normalize_item, clean_dataframe (Polars/Pandas)
│   │   ├── dedupe.py                # MinHashDeduper (Redis) + SimHashDeduper
│   │   ├── language_pii.py          # fastText detect + Presidio redact (lazy env, regex fallback)
│   │   └── storage/                 # json, csv, parquet+minio, elasticsearch exporters
│   ├── storage/                     # connectors, Parquet lake, tiering/lifecycle
│   ├── ai/                          # ── AI layer ──
│   │   ├── llm_parser.py            # LangChain schema-driven HTML→JSON extraction
│   │   ├── vector_search.py         # ChromaDB semantic search, pluggable embedders
│   │   └── agents/
│   │       ├── agent_crawler.py     # agentic crawl: LLM link planning, page budget, fallbacks
│   │       ├── rag_chain.py         # chunking, retrieval, cited answers, LLM synthesis
│   │       ├── data_curator.py      # LLM-as-judge quality scoring (robust JSON parsing)
│   │       └── synthetic.py         # QA / paraphrase / Self-Instruct generation → JSONL
│   ├── ingestion/                   # ── data acquisition ──
│   │   ├── registry.py              # source registry: rss/atom/sitemap/api/s3/commoncrawl/website
│   │   ├── commoncrawl.py           # CC index queries + WARC streaming
│   │   ├── hf_datasets.py           # HuggingFace dataset loaders
│   │   └── downloader.py
│   ├── compute/                     # ── compute orchestration ──
│   │   ├── scheduler.py             # priority queues, dynamic beat schedules, quota-aware enqueue
│   │   ├── ray_jobs.py              # Ray job submission
│   │   └── spark_jobs.py            # Spark batch jobs
│   ├── training/                    # ── trillion-token pipeline ──
│   │   ├── pipeline.py              # WebDataset shards, sequence packing, mixing (resumable)
│   │   ├── tokenizer.py             # fast BPE/tokenization stage
│   │   ├── filters.py               # length/quality/toxicity gates
│   │   ├── language_toxicity.py     # per-language toxicity scoring
│   │   ├── minhash_dedupe.py        # exact+near dedupe at corpus scale
│   │   └── semantic_dedupe.py       # embedding-cluster dedupe
│   ├── observability/metrics.py     # Prometheus counters/gauges/histograms + Sentry init
│   ├── dashboard/app.py             # FastAPI + WebSocket live ops UI
│   └── utils/logger.py
├── workers/crawl_tasks.py           # seed / drain / fetch / process Celery tasks
├── deploy/
│   ├── prometheus.yml
│   └── k8s/manifests/               # redis, worker Deployment + HPA, dashboard + Ingress
├── .github/workflows/ci.yml         # test job (Redis service) + matrix image builds
└── tests/                           # 62 tests across 6 suites (fakeredis, offline, env-isolated)
```

---

## 🚀 Setup Guide

### 1. Local development (single node)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # edit as needed
pytest                                    # 62 tests, fakeredis-backed, fully offline, no services required
python main.py                            # quick crawl of https://example.com
```

### 2. Full stack with Docker Compose

```bash
docker compose up --build -d
# Services: redis, postgres, minio (:9001 console), elasticsearch (:9200),
#           2× workers, stealth worker, celery beat, dashboard (:8080), prometheus (:9090)

# Seed the frontier and start crawling:
celery -A src.celery_app call workers.seed_frontier --json='["https://example.com"]'
celery -A src.celery_app call workers.drain_frontier --kwargs='{"batch": 20}'
```

Open **http://localhost:8080** for the live dashboard (WebSocket updates every 2 s).

### 3. SaaS API surface

With the API app running (`uvicorn src.api.main:app`):

* **REST** — canonical prefix `/api/v1` (legacy alias `/v1` also served):
  * `POST /api/v1/jobs` — submit crawl jobs (quota-checked, priority-classed)
  * `GET  /api/v1/search` — semantic + keyword search, tenant-scoped
  * `/api/v1/webhooks` · `/api/v1/billing` · `/api/v1/marketplace`
* **GraphQL** — single `POST /graphql` endpoint built by `make_graphql_router()`
  (Strawberry schema exposes `job(id: ID!)`, mutations for submit/cancel).
* **Auth** — `POST /api/auth/signup` / `token` (OAuth2 password flow → JWT pair) or
  `Authorization: Bearer <jwt>` / `X-API-Key`. Secrets come from `JWT_SECRET`
  (required, no hardcoded fallback); rotate with `JWT_OLD_SECRETS`.
* **Errors** — uniform envelope: `{"error": {"code": "...", "message": "..."}}`.

Run the standalone gateway (`uvicorn src.gateway.app:app`) for the same auth /
rate-limit / billing / webhook wiring as its own service.

### 4. Scale workers horizontally

```bash
docker compose up -d --scale worker=8       # plain processes
# or on Kubernetes (HPA scales 3→50 on queue depth):
kubectl apply -f deploy/k8s/manifests/
```

### 5. Enable stealth / anti-bot features

```bash
pip install playwright curl-cffi undetected-chromedriver
playwright install chromium
export PROXY_RESIDENTIAL="http://user:pass@res-gateway:8000"
export CAPTCHA_API_KEY="<2captcha key>" CAPTCHA_PROVIDER=2captcha
```

The `worker-stealth` Docker target bundles everything (browsers + fonts included).
Unit tests never touch the network: set `WCP_OFFLINE=1` (the test suite does this automatically).

### 6. Enable AI features

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

from src.ai.agents import AgentCrawler, RAGChain, DataCurator, SyntheticGenerator
```

All agents degrade gracefully offline (heuristic planner, extractive RAG answers,
rule-based curation/synthesis) and tolerate malformed LLM output (balanced-JSON
extraction, judge-score clamping, fallback paths).

### 7. Ingestion → Training pipeline

```python
from src.ingestion.registry import SourceRegistry
from src.compute.scheduler import SchedulerService
from src.training.pipeline import TrainingPipeline

reg = SourceRegistry(redis_url)                 # rss / sitemap / api / s3 / commoncrawl / website
SchedulerService(redis_url).register(reg)       # cron refreshes → priority queues

tp = TrainingPipeline(out_dir="gs://bucket/lm") # raw → clean → dedupe → tokenize → pack
tp.run()                                        # WebDataset tar shards, resumable via Redis bookkeeping
```

Heavy stages can be dispatched to **Ray** (`src/compute/ray_jobs.py`) or
**Spark** (`src/compute/spark_jobs.py`).

### 8. Observability

* **Prometheus** scrapes workers on `:8000/metrics` (`crawler_pages_crawled_total`, `crawler_frontier_pending`, `crawler_request_duration_seconds`, …). Query at http://localhost:9090.
* **Sentry**: set `SENTRY_DSN`; the Celery integration is wired automatically.
* **Dashboard** `/ws/metrics` streams frontier stats over WebSockets; `/api/stats` for REST.
* API responses carry `X-Process-Time`; rate-limit headers follow plan quotas.

### 9. CI/CD

`.github/workflows/ci.yml` runs lint + pytest against a real Redis service container, then builds & pushes all four Docker targets to GHCR on merges to `main`.

---

## ⚙️ Configuration Reference

See **`.env.example`** for the complete list. Highlights:

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Frontier + Celery broker/backend + registry/marketplace state |
| `FRONTIER_NAMESPACE` | `frontier` | Key prefix (multi-tenant frontiers) |
| `JWT_SECRET` | – (**required** in prod/tests) | Token signing key; no hardcoded fallback |
| `JWT_OLD_SECRETS` | – | Comma-separated previous keys (rotation window) |
| `API_PREFIX` | `/api/v1` | Canonical REST mount (`/v1` alias always added) |
| `DEDUPE_THRESHOLD` | `0.9` | MinHash Jaccard duplicate cutoff |
| `FASTTEXT_MODEL` | `lid.176.bin` | Resolved lazily per call (import-order safe) |
| `PROXY_DATACENTER/_RESIDENTIAL/_MOBILE` | – | Comma-separated proxy pools per tier |
| `CAPTCHA_API_KEY` / `CAPTCHA_PROVIDER` | – | `2captcha` \| `anti-captcha` |
| `MINIO_ENDPOINT` etc. | `localhost:9000` | S3-compatible object store |
| `ELASTICSEARCH_URL` | `http://localhost:9200` | Search backend |
| `LLM_MODEL` | `gpt-4o-mini` | Extraction / agent / judge model |
| `EMBEDDING_PROVIDER` | `hash` | `openai` \| `sentence-transformers` \| `hash` (offline) |
| `WCP_OFFLINE` | – | `1` forces fetchers/agents into no-network mode (tests set it) |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | – | Billing sync + webhook verification |
| `MARKETPLACE_MANIFEST_SECRET` | – | HMAC key for signed delivery manifests |
| `SENTRY_DSN` | – | Error tracking (optional) |

---

## 🧪 Testing

**62 tests, all green:**

```bash
JWT_SECRET=test-secret-key-for-ci pytest tests/ -q
# 62 passed
```

| Suite | Tests | Covers |
|---|---|---|
| `test_ai_agents.py` | 31 | agent crawler, RAG chain, data curator (LLM-judge JSON parsing), synthetic generator |
| `test_auth_flow.py` | 8 | signup → JWT pair, API keys, protected `/api/v1` routes, job submission, GraphQL `job(id: ID!)`, auth-context middleware, rate-limit/process-time headers |
| `test_basic_crawler.py` | 5 | core crawl loop, link extraction, robots politeness |
| `test_frontier.py` | 6 | frontier semantics, politeness, ack/nack, stale recovery |
| `test_middleware.py` | 6 | proxy pools, captcha guards, stealth fallbacks |
| `test_pipeline.py` | 6 | cleaning, MinHash/SimHash dedupe, language detect, PII regex fallback, exporters |

Isolation guarantees:

* Tests use **fakeredis (Lua-enabled)** so frontier Lua scripts execute without external services — CI additionally spins up a real Redis service container for integration parity.
* An autouse fixture in `tests/conftest.py` **snapshots and restores `os.environ` around every test**, preventing cross-test environment pollution (e.g. `FASTTEXT_MODEL` leaking between suites), and sweeps stray artifacts from CWD.
* `WCP_OFFLINE=1` is set before any imports — unit tests never hit the network.
* `FASTTEXT_MODEL` is read lazily at call time (not captured at import), so module load order can't change behavior.

## ⚖️ Responsible Scraping

Respect `robots.txt`, rate-limit politely (the frontier enforces per-domain delays by design), and comply with each site's ToS and applicable law (GDPR/CCPA — hence built-in PII redaction).
