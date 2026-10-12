# 🕷️ Web Crawler Pro

**Enterprise-grade, cloud-native, distributed web scraping & data-ingestion platform** — a self-hosted alternative to Scrapy Cloud / Zyte, with stealth fetching, quality engineering, and AI-powered extraction built in.

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
| Language detection | **fastText** `lid.176` with heuristic fallback |
| PII redaction | **Presidio** analyzer/anonymizer with regex fallback |
| Storage | JSONL, CSV, partitioned **Parquet** → **MinIO/S3**, **Elasticsearch** bulk indexing |
| AI parsing | **LangChain LLM parser** — structured JSON from raw HTML, zero CSS selectors |
| Semantic search | **ChromaDB** vectors + pluggable embeddings (OpenAI / sentence-transformers / offline hash) |
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
│   ├── crawlers/basic_crawler.py    # sync core crawler
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
│   │   └── vector_search.py         # ChromaDB semantic search, pluggable embedders
│   ├── observability/metrics.py     # Prometheus counters/gauges/histograms + Sentry init
│   ├── dashboard/app.py             # FastAPI + WebSocket live ops UI
│   └── utils/logger.py
├── workers/crawl_tasks.py           # seed / drain / fetch / process Celery tasks
├── deploy/
│   ├── prometheus.yml
│   └── k8s/manifests/               # redis, worker Deployment + HPA, dashboard + Ingress
├── .github/workflows/ci.yml         # test job (Redis service) + matrix image builds
└── tests/                           # frontier, pipeline, middleware suites (fakeredis)
```

---

## 🚀 Setup Guide

### 1. Local development (single node)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # edit as needed
pytest                                    # 23 tests, fakeredis-backed, no services required
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

* **Prometheus** scrapes workers on `:8000/metrics` (`crawler_pages_crawled_total`, `crawler_frontier_pending`, `crawler_request_duration_seconds`, …). Query at http://localhost:9090.
* **Sentry**: set `SENTRY_DSN`; the Celery integration is wired automatically.
* **Dashboard** `/ws/metrics` streams frontier stats over WebSockets; `/api/stats` for REST.

### 7. CI/CD

`.github/workflows/ci.yml` runs lint + pytest against a real Redis service container, then builds & pushes all four Docker targets to GHCR on merges to `main`.

---

## ⚙️ Configuration Reference

See **`.env.example`** for the complete list. Highlights:

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Frontier + Celery broker/backend |
| `FRONTIER_NAMESPACE` | `frontier` | Key prefix (multi-tenant frontiers) |
| `DEDUPE_THRESHOLD` | `0.9` | MinHash Jaccard duplicate cutoff |
| `PROXY_DATACENTER/_RESIDENTIAL/_MOBILE` | – | Comma-separated proxy pools per tier |
| `CAPTCHA_API_KEY` / `CAPTCHA_PROVIDER` | – | `2captcha` \| `anti-captcha` |
| `MINIO_ENDPOINT` etc. | `localhost:9000` | S3-compatible object store |
| `ELASTICSEARCH_URL` | `http://localhost:9200` | Search backend |
| `LLM_MODEL` | `gpt-4o-mini` | Extraction model |
| `EMBEDDING_PROVIDER` | `hash` | `openai` \| `sentence-transformers` \| `hash` |
| `SENTRY_DSN` | – | Error tracking (optional) |

---

## 🧪 Testing

```bash
pytest -q            # 23 passing: frontier semantics, politeness, ack/nack, stale recovery,
                     # MinHash/SimHash, PII, exporters, proxy pools, captcha guards, metrics
```

Tests use **fakeredis (Lua-enabled)** so frontier Lua scripts execute without external services — CI additionally spins up a real Redis service container for integration parity.

## ⚖️ Responsible Scraping

Respect `robots.txt`, rate-limit politely (the frontier enforces per-domain delays by design), and comply with each site's ToS and applicable law (GDPR/CCPA — hence built-in PII redaction).
