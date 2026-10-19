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
image's Docker virtual size, and runs the dashboard with Redis. The size is the
uncompressed sum reported by Docker; it is not the compressed registry
transfer size, and shared layers may be counted for each image. The smoke
checks the dashboard `/healthz` endpoint and tears down those services. Pull
requests validate the overlay's resolved Compose config and build/smoke the
dashboard with a validation-only tag through
that same overlay. The published-image smoke is intentionally bounded to
dashboard + Redis; pulling the worker, stealth-worker, and crawler images
verifies their published tags are available, but does not verify crawl
execution, service integrations, or the complete production Compose stack.
