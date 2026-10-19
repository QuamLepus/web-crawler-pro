# Run published GHCR images with Compose

Images are tagged by commit SHA. After logging in to GHCR if required, select
the exact SHA with `WCP_IMAGE_TAG` and apply the published-image overlay:

```bash
export WCP_IMAGE_TAG=<published-commit-sha>
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml up --no-build -d
```

The overlay sets published GHCR image names while the base Compose file keeps
the local-build workflow. `--no-build` ensures Compose runs the pulled images.
The full stack includes Redis, Postgres, MinIO, Elasticsearch, worker pools,
Celery Beat, and the dashboard; it may need substantial memory and disk.

On pushes to `main`, CI waits for all four image-publish jobs, then pulls all
four just-published application images through the GHCR overlay, reports each
image's Docker virtual size, and starts the published dashboard and both
Celery worker images with Redis. The size is the uncompressed sum reported by
Docker; it is not the compressed registry transfer size, and shared layers may
be counted for each image. PR image-size reports are uploaded as per-image
artifacts with 30-day retention; the published report is uploaded as one
artifact with 90-day retention. The smoke checks the dashboard `/healthz`
endpoint and waits for both workers to log their Celery `ready.` message before
teardown. Pull requests validate the overlay's resolved Compose config and
build/smoke the dashboard with a validation-only tag through that same overlay.
The published-image smoke is intentionally bounded to Redis + dashboard + both
worker processes. The crawler image is pulled to
verify its published tag, but it is not started: its current container command
runs a sample crawl against an external site. The smoke still does not verify
crawl execution, service integrations, or the complete production Compose
stack.
