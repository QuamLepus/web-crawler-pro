# RUNBOOK — WebCrawler Pro (Full Stack, Docker Compose)

How to build and run the entire system (API Gateway, Dashboard, Celery
workers incl. Stealth Engine, Redis, Postgres, MinIO, Elasticsearch,
Prometheus) with `docker compose`, including every required environment
variable and how to reach each service.

---

## 0. Prerequisites

- Docker Engine ≥ 24 + Docker Compose v2 (`docker compose version`)
- ~8 GB free RAM for the full stack (Elasticsearch alone reserves 512 MB heap)
- In **GitHub Codespaces**: use the *devcontainer* or install compose inside
  the codespace; DinD is available via `features: docker-in-docker` in
  `.devcontainer.json`.

---

## 1. Configure the environment

```bash
cp .env.example .env
$EDITOR .env                     # fill in the REQUIRED values below
```

### Required variables (system will not start / auth will fail without them)

| Variable | Why | Example / how to generate |
|---|---|---|
| `JWT_SECRET` | API-gateway token signing. The app **refuses to start** without it. | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `REDIS_URL` | Celery broker + frontier + rate limiter | `redis://localhost:6379/0` (compose overrides to `redis://redis:6379/0`) |
| `POSTGRES_HOST/DB/USER/PASSWORD` | Job & tenant persistence | defaults `postgres / crawler / crawler / crawler` are fine locally — **rotate before deploy** |
| `MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY` | Raw HTML / artifact object store | defaults `minioadmin / minioadmin` |
| `ELASTICSEARCH_URL` | Full-text search index | `http://localhost:9200` (in-network: `http://elasticsearch:9200`) |

### Optional but recommended (AI Layer + Stealth Engine)

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | GPT-4 data curation, LLM parsing, synthetic data, agent planner |
| `CURATOR_MODEL`, `SYNTH_MODEL`, `LLM_MODEL` | Model names (default `gpt-4o-mini` / `gpt-4o`) |
| `RAG_BACKEND` | `auto` \| `chroma` \| `qdrant` \| `memory` |
| `QDRANT_URL` | Only when using Qdrant as RAG vector store |
| `EMBEDDING_PROVIDER` | `hash` (offline default) \| `openai` \| `sentence-transformers` |
| `PROXY_PROVIDER` | `brightdata` \| `oxylabs` — enables live gateway rotation |
| `PROVIDER_BRIGHTDATA_USERNAME/PASSWORD`, `PROVIDER_BRIGHTDATA_ZONE` (+`_MOBILE_ZONE`) | Bright Data residential/mobile gateways |
| `PROVIDER_OXYLABS_USERNAME/PASSWORD` | Oxylabs residential/mobile/datacenter gateways |
| `PROXY_COUNTRY`, `PROXY_STICKY_TTL` | Geo-targeting + sticky-session minutes |
| `CAPSOLVER_API_KEY` | CapSolver captcha solving (Turnstile / Cloudflare / reCAPTCHA) |
| `STEALTH_ENGINE` | `1` (default) enables the plain → curl_cffi → playwright escalation ladder |
| `STEALTH_STICKY` | `1` reuses one fingerprint per process; default draws a fresh cluster per fetch |
| `HEADLESS` | `1` for servers/codespaces |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Billing endpoints (skip in dev) |
| `SENTRY_DSN` | Error reporting (skip in dev) |

> Inside containers the compose file injects network-correct values
> (`redis://redis:6379/0`, host `postgres`, `minio:9000`, `elasticsearch:9200`)
> via the `x-app-env` anchor — your local `.env` values only matter for
> processes you run directly on the host.

---

## 2. Build & start the whole stack

```bash
# build all image targets (worker, worker-stealth, dashboard, crawler)
docker compose build

# start everything in the background
docker compose up -d

# watch it come up
docker compose ps            # redis should be "healthy" first
docker compose logs -f worker dashboard
```

Useful variants:

```bash
docker compose up -d --scale worker=4          # more crawl throughput
docker compose --profile api up -d             # if you add the api service
docker compose down                            # stop (keeps volumes/data)
docker compose down -v                         # stop AND wipe all data
docker compose restart dashboard               # reload one service
```

The stealth worker image downloads Chromium during build
(`playwright install chromium --with-deps`) — first build can take several
minutes.

---

## 3. Service map — ports & access

| Service | Container port | Host port | URL / how to access |
|---|---|---|---|
| **Dashboard** | 8080 | **8080** | http://localhost:8080 — live job board, frontier stats, WebSocket feed |
| **API Gateway** (FastAPI, `src.api.main:app`) | 8000 | **8000** | http://localhost:8000/api/v1 (REST), http://localhost:8000/graphql (GraphiQL at `/graphql/playground`), health: `/healthz` `/readyz`, metrics: `/metrics` |
| Redis | 6379 | 6379 | `redis-cli -h localhost ping` |
| Postgres | 5432 | 5432 | `psql -h localhost -U crawler -d crawler` (pw `crawler`) |
| MinIO S3 API | 9000 | 9000 | AWS SDK endpoint `http://localhost:9000` |
| MinIO Console | 9001 | **9001** | http://localhost:9001 (minioadmin/minioadmin) |
| Elasticsearch | 9200 | 9200 | http://localhost:9200 (`curl localhost:9200/_cluster/health`) |
| Prometheus | 9090 | **9090** | http://localhost:9090 (queries + built-in alerts UI) |

**In GitHub Codespaces**: click the **Ports** tab — forwarded URLs look like
`https://<codespace>-8080.github.dev` (dashboard), `-8000` (API), `-9001`
(MinIO console), `-9090` (Prometheus). Or from the terminal simply use
`curl http://localhost:<port>`; VS Code port-forwarding handles browsers.

### Quick smoke test

```bash
curl -s localhost:8000/healthz                      # {"status":"ok"}
curl -s localhost:9200/_cluster/health | head -c 200
docker compose exec worker celery -A src.celery_app inspect ping
```

---

## 4. Running the API gateway

The compose file ships `dashboard`, workers and beat; to also run the public
API gateway container:

```bash
docker compose run --rm --service-ports --name api \
  --build -p 8000:8000 dashboard \
  uvicorn src.api.main:app --host 0.0.0.0 --port 8000
# or on the host (venv):
uvicorn src.api.main:app --reload --port 8000
```

Auth: send `Authorization: Bearer <JWT signed with JWT_SECRET>` or
`X-API-Key: <tenant key>` on every `/api/v1` call.

---

## 5. Stop / upgrade / reset

```bash
docker compose pull && docker compose up -d --build   # upgrade
docker compose down                                    # graceful stop
docker compose down -v                                 # full reset (loses data)
```

## 6. Troubleshooting

- **Codespaces can't bind ports** → check the Ports tab visibility is set to
  "Public"/"Private"; internal traffic still works via `localhost`.
- **Elasticsearch OOM / exit 137** → raise codespace to 8 GB, or drop the ES
  service for light runs: `docker compose up -d redis postgres worker dashboard`.
- **`JWT_SECRET` missing** → API refuses to start by design; set it in `.env`.
- **Stealth worker fails to launch Chromium** → rebuild image
  (`docker compose build worker-stealth`); codespaces need
  `--privileged`-style seccomp unconfined for Playwright:
  add `security_opt: ["seccomp:unconfined"]` under the service if needed.
- **Tests** → `JWT_SECRET=test-secret-key-for-ci pytest tests/ -q`
  (all offline, `WCP_OFFLINE=1` guards network access).
