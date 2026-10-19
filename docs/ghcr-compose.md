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
The default core stack includes Redis, Postgres, MinIO, Elasticsearch,
Prometheus, worker pools, Celery Beat, and the dashboard; it may need
substantial memory and disk. The `crawler` service is opt-in via the `on-demand`
profile because its current command runs an external sample crawl. Enable it
only when you intend to send that request.

On pushes to `main`, CI waits for all four image-publish jobs, then pulls all
four just-published application images through the GHCR overlay, reports each
image's Docker virtual size, and starts the published core stack including
Redis, Postgres, MinIO, Elasticsearch, Prometheus, Celery Beat, both workers,
and the dashboard. The size is the uncompressed sum reported by
Docker; it is not the compressed registry transfer size, and shared layers may
be counted for each image. PR image-size reports are uploaded as per-image
artifacts with 30-day retention; the published report is uploaded as one
artifact with 90-day retention. The smoke checks the dashboard `/healthz`
endpoint and waits for both workers to log their Celery `ready.` message before
teardown. Pull requests validate the overlay's resolved Compose config and
build/smoke the core stack with validation-only tags through that same overlay.
The smoke verifies each core service's readiness, but does not execute a crawl
or prove that application code integrates with Postgres, MinIO, Elasticsearch,
or Prometheus. It pulls the crawler image to verify its published tag but does
not start it: the current container command runs a sample crawl against an
external site. The opt-in crawler profile avoids triggering that request during
normal `compose up` and CI.
