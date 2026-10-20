# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Shared, small runtime layer. Service-specific requirements are installed only
# in the target that needs them, so API/dashboard images do not inherit the
# worker data stack or development/browser extras.
COPY requirements-core.txt ./
RUN pip install --upgrade pip && pip install -r requirements-core.txt
COPY . .

# ---- default target: Celery worker node (scales horizontally) ------------- #
FROM base AS worker
RUN pip install -r requirements-worker.txt
CMD ["celery", "-A", "src.celery_app", "worker", "-Q", "crawl.high,crawl.default,crawl.bulk", "-c", "8", "--loglevel=info"]

# ---- crawler API / seed service --------------------------------------------- #
FROM base AS crawler
RUN pip install -r requirements-api.txt
EXPOSE 8000
CMD ["python", "main.py"]

# ---- dashboard --------------------------------------------------------------- #
FROM base AS dashboard
RUN pip install -r requirements-api.txt
EXPOSE 8080
CMD ["uvicorn", "src.dashboard.app:app", "--host", "0.0.0.0", "--port", "8080"]

# ---- stealth browser worker (Playwright + curl-cffi) -------------------------- #
FROM worker AS worker-stealth
RUN pip install -r requirements-browser.txt \
    && apt-get update && apt-get install -y --no-install-recommends \
      wget gnupg fonts-liberation libnss3 libatk-bridge2.0-0 libdrm2 \
      libxkbcommon0 libgbm1 libasound2 && rm -rf /var/lib/apt/lists/* \
    && playwright install chromium --with-deps
CMD ["celery", "-A", "src.celery_app", "worker", "-Q", "crawl.high", "-c", "2", "--loglevel=info"]
