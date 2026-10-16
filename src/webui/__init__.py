"""The web console: a server-rendered product surface over the public API.

    from src.api.main import app
    from src.webui import mount_webui
    mount_webui(app)          # -> True when jinja2 is available

Everything the console can do, a customer can do with ``curl``: it is an
ordinary HTTP client of the documented API, with the caller's JWT held in an
httpOnly cookie so browser JavaScript never sees it.

Deliberately: no CDN, no npm, no build step, no external fonts. One
stylesheet, one script, system fonts — the console has to render on a box
that has never touched the internet, the same claim the crawler makes.
"""

from __future__ import annotations

from src.webui.app import FEATURES, NAV, PLANS, mount_webui, router

__all__ = ["FEATURES", "NAV", "PLANS", "mount_webui", "router"]
