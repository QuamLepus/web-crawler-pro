# Web Crawler Pro

A professional, extensible web crawler project.

## Project Status

### ✅ Implemented (foundation)
- Core synchronous crawler (`src/crawlers/basic_crawler.py`) with retry logic,
  session handling, text/link extraction
- Central configuration (`src/config.py`), env-var driven
- Logging groundwork (`src/utils/logger.py`)
- Unit tests (`tests/`) — all passing

### 🚧 Not yet implemented (roadmap)
**Crawling & scale**
- [ ] Async crawler (aiohttp / asyncio)
- [ ] Pagination crawler
- [ ] Celery + Redis distributed queue
- [ ] URL Frontier backed by Redis
- [ ] Worker scaling strategy

**Anti-bot / stealth**
- [ ] Playwright stealth mode
- [ ] CAPTCHA solver integration
- [ ] curl_cffi / TLS fingerprint spoofing

**Data quality & processing**
- [ ] MinHash / SimHash near-duplicate detection
- [ ] fastText language/text classification
- [ ] Presidio PII detection & redaction
- [ ] Image parser (EXIF, OCR hooks)

**Storage & search pipeline**
- [ ] CSV / JSON exporters
- [ ] Parquet export → MinIO (S3) → Elasticsearch indexing
- [ ] LangChain AI-powered page parser
- [ ] Vector DB semantic search

**Operations**
- [ ] Docker / Kubernetes manifests
- [ ] Prometheus metrics + Sentry error tracking
- [ ] CI/CD workflow (GitHub Actions)
- [ ] Production dashboard (WebSockets + live metrics)

## Quick Start

```bash
pip install -r requirements.txt
python main.py            # crawl https://example.com
pytest                    # run unit tests
```

## Configuration

All settings can be overridden via environment variables, e.g.:

| Variable | Default | Description |
|---|---|---|
| `CRAWLER_USER_AGENT` | Mozilla/5.0 (compatible; WebCrawlerPro/1.0) | HTTP User-Agent |
| `CRAWLER_REQUEST_TIMEOUT` | 30 | Request timeout (s) |
| `CRAWLER_RETRY_ATTEMPTS` | 3 | Max retries per request |
| `CRAWLER_LOG_LEVEL` | INFO | Logging level |
| `REDIS_URL` | redis://localhost:6379/0 | For future frontier/Celery |

## Structure

```
src/
  config.py               # env-driven configuration singleton
  crawlers/
    basic_crawler.py      # sync requests+BeautifulSoup crawler
  storage/                # data storage handlers (WIP)
  utils/
    logger.py             # logging setup
tests/
  test_basic_crawler.py
main.py                   # CLI entry point
```
