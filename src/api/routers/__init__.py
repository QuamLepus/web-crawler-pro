"""Public v1 REST routers for the WebCrawlerPro API.

Each module exports an ``APIRouter`` with its own prefix (e.g. ``/jobs``);
:func:`src.api.main.create_api_app` mounts them all under ``/api/v1``.
"""

from src.api.routers import (
                             agent_marketplace,
                             billing,
                             discovery,
                             jobs,
                             marketplace,
                             search,
                             webhooks,
)

__all__ = ["agent_marketplace", "billing", "discovery", "jobs", "marketplace",
           "search", "webhooks"]
