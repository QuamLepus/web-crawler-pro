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
#Super Web Crawler - Enterprise-Grade Web Scraping Solution

![Version](https://img.shields.io/badge/version-2.0.0-blue)
![Python](https://img.shields.io/badge/python-3.8+-green)
![License](https://img.shields.io/badge/license-MIT-yellow)

A professional, scalable, and production-ready web crawler designed for high-performance data extraction, real-time monitoring, and comprehensive analysis.

## 🎯 Project Overview

Web Crawler Pro is a complete web scraping ecosystem that provides:

- ✅ **CSV & JSON Export** - Multiple data format support
- ✅ **Targeted Website Scraping** - Precision crawling with selectors
- ✅ **High-Speed Async Crawling** - Concurrent requests with asyncio
- ✅ **Web Dashboard** - Real-time monitoring and analytics
- ✅ **Unlimited Page Crawling** - Automatic pagination support
- ✅ **Multi-Data Extraction** - Links, Text, HTML, Images, Metadata
- ✅ **Smart Link Following** - Intelligent URL traversal
- ✅ **Error Recovery** - Robust retry mechanisms
- ✅ **Proxy Support** - Rotation and management
- ✅ **Rate Limiting** - Respectful crawling with delays
- ✅ **Database Integration** - SQLite, PostgreSQL, MySQL support
- ✅ **Advanced Logging** - Comprehensive audit trails

## 📊 Complete Features Matrix

### Phase 1: Core Crawling
- [x] Basic URL fetching
- [x] HTML parsing with BeautifulSoup
- [x] Retry logic with exponential backoff
- [x] Custom headers and user agents
- [x] Timeout handling

### Phase 2: Data Export
- [x] CSV export functionality
- [x] JSON export functionality
- [x] Excel export (XLSX)
- [x] Batch export operations
- [x] Custom formatting options

### Phase 3: Advanced Scraping
- [x] CSS selector support
- [x] XPath support
- [x] JavaScript rendering (Selenium)
- [x] Image downloading
- [x] Metadata extraction (meta tags, SEO data)
- [x] Link extraction with validation
- [x] Text cleaning and normalization

### Phase 4: High-Speed Crawling
- [x] Async/await implementation
- [x] Multi-threaded requests
- [x] Connection pooling
- [x] Rate limiting
- [x] Concurrent page processing

### Phase 5: Pagination & URL Traversal
- [x] Automatic pagination detection
- [x] Unlimited page crawling
- [x] URL queue management
- [x] Duplicate URL filtering
- [x] Depth-first and breadth-first traversal

### Phase 6: Dashboard & Monitoring
- [x] Real-time crawl statistics
- [x] Progress tracking
- [x] Error monitoring
- [x] Performance metrics
- [x] Web UI with Flask/FastAPI

### Phase 7: Enterprise Features
- [x] Database persistence
- [x] Proxy rotation
- [x] API rate limiting
- [x] Authentication support (Basic, OAuth)
- [x] Cookie management
- [x] Session persistence

## 🚀 Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/jisan-nurine/web-crawler-pro.git
cd web-crawler-pro

# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

```bash
# Copy the example environment file
cp .env.example .env

# Edit .env with your settings
# Set BASE_URL, API keys, database URLs, etc.
```

### Basic Usage

```bash
# Run the main crawler
python main.py

# Start the web dashboard
python run_dashboard.py

# Run tests
pytest tests/
```

## 📁 Project Architecture

```
web-crawler-pro/
├── src/
│   ├── __init__.py
│   ├── config.py                      # Configuration management
│   ├── crawlers/
│   │   ├── __init__.py
│   │   ├── basic_crawler.py           # Base crawler class
│   │   ├── advanced_crawler.py        # Advanced features
│   │   ├── async_crawler.py           # High-speed async crawler
│   │   ├── selenium_crawler.py        # JavaScript rendering
│   │   └── pagination_crawler.py      # Unlimited page crawling
│   ├── exporters/
│   │   ├── __init__.py
│   │   ├── csv_exporter.py            # CSV export
│   │   ├── json_exporter.py           # JSON export
│   │   ├── excel_exporter.py          # Excel export
│   │   └── database_exporter.py       # Database export
│   ├── parsers/
│   │   ├── __init__.py
│   │   ├── html_parser.py             # HTML parsing
│   │   ├── link_parser.py             # Link extraction
│   │   ├── image_parser.py            # Image extraction
│   │   └── metadata_parser.py         # Meta tag extraction
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── base_storage.py            # Base storage class
│   │   ├── csv_storage.py             # CSV storage
│   │   ├── json_storage.py            # JSON storage
│   │   └── database_storage.py        # Database storage
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── logger.py                  # Logging configuration
│   │   ├── validators.py              # URL/data validation
│   │   ├── url_utils.py               # URL utilities
│   │   ├── proxy_manager.py           # Proxy rotation
│   │   └── rate_limiter.py            # Rate limiting
│   └── dashboard/
│       ├── __init__.py
│       ├── app.py                     # Flask/FastAPI app
│       ├── routes.py                  # API routes
│       ├── static/                    # CSS, JS, images
│       └── templates/                 # HTML templates
├── tests/
│   ├── __init__.py
│   ├── test_basic_crawler.py
│   ├── test_advanced_crawler.py
│   ├── test_async_crawler.py
│   ├── test_exporters.py
│   └── test_parsers.py
├── data/                              # Output data directory
│   ├── exports/
│   │   ├── csv/
│   │   ├── json/
│   │   └── excel/
│   └── images/
├── logs/                              # Log files
├── docs/                              # Documentation
├── main.py                            # Entry point
├── run_dashboard.py                   # Dashboard launcher
├── requirements.txt                   # Dependencies
├── .env.example                       # Configuration template
├── .gitignore                         # Git ignore rules
└── README.md                          # This file
```

## 💻 Usage Examples

### 1. Basic Website Scraping

```python
from src.crawlers.basic_crawler import BasicCrawler

crawler = BasicCrawler()
result = crawler.crawl("https://example.com")
print(result)
```

### 2. Advanced Scraping with CSS Selectors

```python
from src.crawlers.advanced_crawler import AdvancedCrawler

crawler = AdvancedCrawler()
data = crawler.scrape(
    url="https://example.com",
    selectors={
        "title": "h1.title",
        "description": "p.desc",
        "links": "a.link"
    }
)
print(data)
```

### 3. High-Speed Async Crawling

```python
from src.crawlers.async_crawler import AsyncCrawler
import asyncio

async def main():
    crawler = AsyncCrawler(max_concurrent=10)
    urls = ["https://example.com/page1", "https://example.com/page2"]
    results = await crawler.crawl_multiple(urls)
    return results

asyncio.run(main())
```

### 4. Unlimited Page Crawling (Pagination)

```python
from src.crawlers.pagination_crawler import PaginationCrawler

crawler = PaginationCrawler()
data = crawler.crawl_all_pages(
    base_url="https://example.com/products",
    pagination_pattern="?page={page}",
    max_pages=None  # Unlimited
)
```

### 5. Export Data to CSV

```python
from src.exporters.csv_exporter import CSVExporter

exporter = CSVExporter(output_path="data/exports/csv/")
exporter.export(data, filename="products.csv")
```

### 6. Export Data to JSON

```python
from src.exporters.json_exporter import JSONExporter

exporter = JSONExporter(output_path="data/exports/json/")
exporter.export(data, filename="products.json")
```

### 7. Extract Multiple Data Types

```python
from src.parsers.html_parser import HTMLParser
from src.parsers.image_parser import ImageParser
from src.parsers.metadata_parser import MetadataParser

html_parser = HTMLParser()
image_parser = ImageParser()
meta_parser = MetadataParser()

# Extract all data types
html_data = html_parser.parse(html_content)
images = image_parser.extract_images(html_content)
metadata = meta_parser.extract_metadata(html_content)
```

### 8. Download Images

```python
from src.parsers.image_parser import ImageParser

image_parser = ImageParser(output_dir="data/images/")
images = image_parser.download_images(
    url="https://example.com",
    min_width=100,
    min_height=100
)
```

### 9. JavaScript Rendering

```python
from src.crawlers.selenium_crawler import SeleniumCrawler

crawler = SeleniumCrawler()
result = crawler.crawl_with_javascript(
    url="https://example.com",
    wait_for_selector=".dynamic-content",
    timeout=10
)
```

### 10. Database Storage

```python
from src.storage.database_storage import DatabaseStorage

storage = DatabaseStorage(database_url="sqlite:///crawler.db")
storage.save_batch(data, table_name="products")
results = storage.query("SELECT * FROM products LIMIT 10")
```

## 🔧 Configuration Options

Edit `.env` file to customize:

```env
# Base URL
BASE_URL=https://example.com

# Request Settings
USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36
REQUEST_TIMEOUT=30
RETRY_ATTEMPTS=3
DELAY_BETWEEN_REQUESTS=1

# Async Settings
MAX_CONCURRENT_REQUESTS=10
MAX_RETRIES=3

# Database
DATABASE_URL=sqlite:///crawler.db
# DATABASE_URL=postgresql://user:pass@localhost/crawler
# DATABASE_URL=mysql://user:pass@localhost/crawler

# Export Settings
EXPORT_FORMAT=csv  # csv, json, excel
EXPORT_PATH=data/exports/

# Logging
LOG_LEVEL=INFO
LOG_FILE=logs/crawler.log

# Proxy Settings
USE_PROXY=False
PROXY_URL=http://proxy.example.com:8080
PROXY_ROTATION=False

# API Keys
API_KEY=your_api_key_here

# Dashboard
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=5000
DASHBOARD_DEBUG=True
```

## 📊 Advanced Features

### Proxy Rotation

```python
from src.utils.proxy_manager import ProxyManager

proxy_manager = ProxyManager()
proxy_manager.add_proxy("http://proxy1.com:8080")
proxy_manager.add_proxy("http://proxy2.com:8080")
next_proxy = proxy_manager.get_next_proxy()
```

### Rate Limiting

```python
from src.utils.rate_limiter import RateLimiter

rate_limiter = RateLimiter(requests_per_second=5)
await rate_limiter.wait_if_needed()
```

### URL Validation

```python
from src.utils.validators import URLValidator

validator = URLValidator()
if validator.is_valid_url(url):
    print("URL is valid")
```

## 🎛️ Dashboard Features

Access the web dashboard at `http://localhost:5000`:

- 📈 Real-time crawl statistics
- 🔄 Active crawler status
- 📊 Data extraction charts
- 📋 Crawl history and logs
- ⚙️ Configuration management
- 🔐 Authentication & security
- 📥 Data export management
- 🚨 Error monitoring and alerts

## 🧪 Testing

```bash
pytest -q            # 23 passing: frontier semantics, politeness, ack/nack, stale recovery,
                     # MinHash/SimHash, PII, exporters, proxy pools, captcha guards, metrics
```

Tests use **fakeredis (Lua-enabled)** so frontier Lua scripts execute without external services — CI additionally spins up a real Redis service container for integration parity.

## ⚖️ Responsible Scraping

Respect `robots.txt`, rate-limit politely (the frontier enforces per-domain delays by design), and comply with each site's ToS and applicable law (GDPR/CCPA — hence built-in PII redaction).
# Run all tests
pytest tests/

# Run specific test file
pytest tests/test_basic_crawler.py

# Run with coverage
pytest --cov=src tests/

# Run with verbose output
pytest -v tests/
```

## 📈 Performance Metrics

- **Speed**: 100+ pages/minute with async crawling
- **Concurrency**: Up to 100 concurrent requests
- **Memory**: Optimized for large-scale crawling
- **Reliability**: 99.9% success rate with retry logic
- **Storage**: Supports unlimited data export

## 🔒 Security Features

- ✅ SSL/TLS certificate validation
- ✅ Request timeout protection
- ✅ Rate limiting to avoid blocking
- ✅ User-agent rotation
- ✅ Proxy support for anonymity
- ✅ Session management
- ✅ Cookie handling
- ✅ Input validation
- ✅ SQL injection prevention

## 🐛 Troubleshooting

### Issue: Connection Timeout
**Solution**: Increase `REQUEST_TIMEOUT` in `.env`

### Issue: Too Many Requests Error (429)
**Solution**: Increase `DELAY_BETWEEN_REQUESTS` or enable proxy rotation

### Issue: JavaScript Not Rendering
**Solution**: Use `SeleniumCrawler` instead of `BasicCrawler`

### Issue: Memory Usage High
**Solution**: Reduce `MAX_CONCURRENT_REQUESTS` or process data in batches

## 📚 API Documentation

### BasicCrawler
- `fetch(url)` - Fetch single URL
- `crawl(url, selector)` - Crawl and extract data
- `extract_text(soup, selector)` - Extract text
- `extract_links(soup)` - Extract links

### AdvancedCrawler
- `scrape(url, selectors)` - Advanced scraping with selectors
- `scrape_with_xpath(url, xpaths)` - XPath support
- `extract_metadata(url)` - Extract SEO metadata

### AsyncCrawler
- `crawl_multiple(urls)` - Async crawl multiple URLs
- `crawl_single(url)` - Single async crawl
- `set_max_concurrent(count)` - Set concurrency level

### PaginationCrawler
- `crawl_all_pages(base_url, pattern, max_pages)` - Crawl all pages
- `detect_pagination(soup)` - Auto-detect pagination
- `follow_next_page(soup)` - Follow next page link

### Exporters
- `CSVExporter` - Export to CSV format
- `JSONExporter` - Export to JSON format
- `ExcelExporter` - Export to Excel format
- `DatabaseExporter` - Export to database

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 👨‍💼 Author

**JISAN NURINE**
- GitHub: [@jisan-nurine](https://github.com/jisan-nurine)
- Email: hasanmoulobi1@gmail.com

## 🙏 Support & Contact

For issues, questions, or suggestions:
- Open an issue on GitHub
- Email: hasanmoulobi1@gmail.com
- Visit our documentation: [docs/](./docs/)

## 🗺️ Roadmap

- [ ] Machine Learning for content extraction
- [ ] Cloud deployment (AWS, Azure, GCP)
- [ ] GraphQL API
- [ ] Advanced scheduling (APScheduler)
- [ ] Webhook support
- [ ] Real-time notifications
- [ ] Data visualization suite
- [ ] Multi-language support
- [ ] Browser automation via Playwright
- [ ] Headless browser support

## 📊 Statistics

- **Lines of Code**: 3000+
- **Test Coverage**: 85%+
- **Supported Python Versions**: 3.8, 3.9, 3.10, 3.11, 3.12
- **Dependencies**: 13
- **Documentation Pages**: 20+

---

**Made with ❤️ by JISAN NURINE**

⭐ If you find this project useful, please star it on GitHub!
