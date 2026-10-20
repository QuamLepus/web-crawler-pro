# Run published GHCR images with Compose

Images are tagged by commit SHA. After logging in to GHCR if required, select
the exact SHA with `WCP_IMAGE_TAG` and apply the published-image overlay. The
MinIO image is hosted on Quay.io; authenticate there first if your environment
requires registry credentials:

```bash
# Run `docker login quay.io` first when Quay credentials are required.
export WCP_IMAGE_TAG=<published-commit-sha>
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml up --no-build -d
```

The overlay sets published GHCR image names while the base Compose file keeps
the local-build workflow. Its default image namespace is
`ghcr.io/quamlepus/web-intelligence-os`; set `WCP_GHCR_REPO` to override it.
The main-branch CI workflow derives that variable from the lowercased
`GITHUB_REPOSITORY`, matching the image-publish tags. `--no-build` ensures
Compose runs the pulled images.
The default core stack includes Redis, Postgres, MinIO, Elasticsearch,
Prometheus, worker pools, Celery Beat, and the dashboard; it may need
substantial memory and disk. The `crawler` service is opt-in via the `on-demand`
profile because its current command runs an external sample crawl. Enable it
only when you intend to send that request.

On pushes to `main`, CI waits for all four image-publish jobs, then pulls all
four just-published application images through the GHCR overlay, reports each
image's Docker virtual size, and starts the published core stack including
Redis, Postgres, Elasticsearch, Prometheus, Celery Beat, both workers, and the
dashboard. It also includes MinIO when the `QUAY_USERNAME` and `QUAY_PASSWORD`
GitHub Actions secrets are configured; without them, CI skips MinIO with a
warning. The size is the uncompressed sum reported by
Docker; it is not the compressed registry transfer size, and shared layers may
be counted for each image. PR image-size reports are uploaded as per-image
artifacts with 30-day retention; the published report is uploaded as one
artifact with 90-day retention. The smoke checks backing-service readiness,
the dashboard `/healthz` endpoint, and both workers' Celery `ready.` message.
MinIO is checked only when Quay credentials are available. Pull requests and manually dispatched CI runs validate the overlay's resolved
Compose config and build/smoke the core stack with validation-only tags through
that same overlay. Use **Actions → CI/CD → Run workflow** on a trusted branch to
repeat the full local-build Compose smoke after configuring Quay repository
secrets; MinIO is exercised only when both secrets are available.
The smoke verifies each core service's readiness, but does not execute a crawl
or prove that application code integrates with Postgres, MinIO, Elasticsearch,
or Prometheus. It pulls the crawler image to verify its published tag but does
not start it: the current container command runs a sample crawl against an
external site. The opt-in crawler profile avoids triggering that request during
normal `compose up` and CI.
