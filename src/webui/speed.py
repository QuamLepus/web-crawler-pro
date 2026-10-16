"""Transport-level speed for the console.

Four cheap, independent wins, in the order they pay off:

* **Compression.** Pages are HTML and gzip cuts them by roughly three
  quarters. Nothing else in this file matters as much on a slow link.
* **Conditional GET.** A page that did not change answers ``304 Not
  Modified`` with no body. Navigating back and forth stops costing anything.
* **Immutable assets.** ``/static/*`` is versioned with ``?v=``, so the
  browser is told to cache it for a year and never revalidate.

Every piece is optional and fails open: if anything here misbehaves the page
still renders, just without the optimisation.

There is deliberately no response cache here. Almost every console page shows
one workspace's own numbers — jobs, usage, keys, datasets — so a cache shared
between requests is one bad key away from showing tenant A's data to tenant B.
Keying it correctly (tenant + endpoint + params) would be a real feature with
real invalidation rules, not a perf garnish; until that exists, the honest
speed wins are the three above, which cannot leak anything.
"""

from __future__ import annotations

import hashlib

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp

# Paths whose content is fingerprinted in the URL, so it can never go stale.
STATIC_PREFIX = "/static/"

# Responses bigger than this are not hashed for an ETag — the saving from a
# 304 is not worth hashing megabytes on every request.
MAX_ETAG_BYTES = 2 * 1024 * 1024


# --------------------------------------------------------------------------- #
# conditional GET
# --------------------------------------------------------------------------- #
class ConditionalGetMiddleware(BaseHTTPMiddleware):
    """Answer ``If-None-Match`` with a bodiless 304 for HTML responses.

    Only HTML, and only when we can hash the body cheaply: a JSON API caller
    gains nothing, and a large file would cost more to hash than to send.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)

        if request.method not in ("GET", "HEAD"):
            return response

        ctype = response.headers.get("content-type", "")
        if "text/html" not in ctype:
            return response

        chunks = []
        total = 0
        too_big = False
        async for chunk in response.body_iterator:
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_ETAG_BYTES:
                too_big = True
        if too_big:
            # Rebuild the stream we just consumed.
            async def replay():
                for chunk in chunks:
                    yield chunk

            response.body_iterator = replay()
            return response

        body = b"".join(chunks)
        digest = hashlib.blake2b(body, digest_size=8).hexdigest()
        etag = f'W/"{digest}"'
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, no-cache"

        if request.headers.get("if-none-match") == etag:
            from starlette.responses import Response

            headers = {
                "ETag": etag,
                "Cache-Control": "private, no-cache",
                "X-WCP-Cache": "hit",
            }
            return Response(status_code=304, headers=headers)

        async def again():
            yield body

        response.body_iterator = again()
        return response


# --------------------------------------------------------------------------- #
# immutable static assets
# --------------------------------------------------------------------------- #
class StaticCacheMiddleware(BaseHTTPMiddleware):
    """Tell the browser to keep ``/static/*`` for a year.

    Safe because every reference to a static file carries ``?v=<n>``; bumping
    the version changes the URL, so the cached copy can never shadow a change.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(STATIC_PREFIX):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# --------------------------------------------------------------------------- #
# installer
# --------------------------------------------------------------------------- #
def install_speed(app: ASGIApp, *, gzip_minimum_size: int = 700) -> None:
    """Add the compression and caching middleware in the right order.

    Starlette makes the *most recently added* middleware the outermost one, so
    these are added innermost-first: the ETag layer has to sit inside
    compression, because it must hash the HTML, not the gzip stream — and gzip
    output can differ between runs for byte-identical HTML.
    """
    app.add_middleware(StaticCacheMiddleware)
    app.add_middleware(ConditionalGetMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=gzip_minimum_size)
