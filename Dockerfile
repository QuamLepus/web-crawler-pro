# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps kept minimal; browsers (playwright) live in the -stealth stage
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# ---- default target: Celery worker node (scales horizontally) ------------- #
FROM base AS worker
CMD ["celery", "-A", "src.celery_app", "worker", "-Q", "crawl.default,crawl.bulk", "-c", "8", "--loglevel=info"]

# ---- crawler API / seed service --------------------------------------------- #
FROM base AS crawler
EXPOSE 8000
CMD ["python", "main.py"]

# ---- dashboard --------------------------------------------------------------- #
FROM base AS dashboard
EXPOSE 8080
CMD ["uvicorn", "src.dashboard.app:app", "--host", "0.0.0.0", "--port", "8080"]

# ---- stealth browser worker (Playwright + curl_cffi) -------------------------- #
FROM base AS worker-stealth
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget gnupg fonts-liberation libnss3 libatk-bridge2.0-0 libdrm2 \
      libxkbcommon0 libgbm1 libasound2 && rm -rf /var/lib/apt/lists/* \
    && pip install playwright curl-cffi undetected-chromedriver \
    && playwright install chromium --with-deps
CMD ["celery", "-A", "src.celery_app", "worker", "-Q", "crawl.high", "-c", "2", "--loglevel=info"]
