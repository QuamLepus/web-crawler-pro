"""Celery application for distributed crawling.

Broker & result backend: Redis. Workers scale horizontally by simply
running `celery -A src.celery_app worker` on as many nodes as needed.
"""

import os

from celery import Celery
from kombu import Exchange, Queue

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

app = Celery(
    "web_crawler_pro",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["workers.crawl_tasks", "workers.discovery_tasks", "workers.research_tasks"],
)

default_exchange = Exchange("crawl", type="direct")

app.conf.update(
    task_queues=(
        Queue("crawl.high", default_exchange, routing_key="crawl.high"),
        Queue("crawl.default", default_exchange, routing_key="crawl.default"),
        Queue("crawl.bulk", default_exchange, routing_key="crawl.bulk"),
    ),
    task_default_queue="crawl.default",
    task_default_routing_key="crawl.default",
    task_acks_late=True,                  # re-deliver on worker crash
    worker_prefetch_multiplier=1,         # fair scheduling across workers
    task_reject_on_worker_lost=True,
    result_expires=3600,
    task_time_limit=600,
    task_soft_time_limit=540,
    worker_max_tasks_per_child=1000,      # guard against memory leaks
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    # Beat schedule example (enable with `celery -A src.celery_app beat`)
    beat_schedule={
        "wcp-watch-poll": {
            "task": "workers.crawl_tasks.watch_tick",
            "schedule": 60.0,
        },
        # "nightly-full-crawl": {
        #     "task": "workers.crawl_tasks.seed_frontier",
        #     "schedule": 86400.0,
        #     "args": (["https://example.com"],),
        # },
    },
)
