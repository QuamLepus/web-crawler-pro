"""Agent Marketplace — publish, install and bill specialised agents.

The dataset marketplace sells data. This one sells *behaviour*: a competitor
monitor, a price intelligence agent, a literature reviewer — each a packaged
workflow template plus the recipe/config it needs, published by a developer
and installed in one click.

Three pieces make it a marketplace rather than a directory:

* **Manifests with versions and checksums** — what you install is what the
  publisher tested (:class:`AgentManifest`).
* **A revenue ledger with an explicit split** — every install and run writes
  an immutable entry with the publisher share, so payouts are arithmetic and
  arguable instead of a spreadsheet someone maintains by hand.
* **Install == configure, not execute.** Installing writes a workflow the
  customer owns and can edit; nothing from a publisher runs on our behalf.
  That is the only defensible security posture for third-party automation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

logger = logging.getLogger("wcp.marketplace.agents")

PLATFORM_SHARE = 0.30           # platform cut on paid agents
CATEGORIES = ("monitoring", "research", "commerce", "news", "compliance",
              "academic", "custom")

FEATURED: list[dict[str, Any]] = [
    {
        "slug": "competitor-monitor",
        "name": "Competitor Monitoring Agent",
        "category": "monitoring",
        "description": "Watches competitor pricing, product and policy pages and "
                       "reports only changes that matter.",
        "workflow_template": "monitor_market",
        "price_cents_month": 1900,
        "runs_priced": False,
    },
    {
        "slug": "price-intelligence",
        "name": "Product Price Intelligence Agent",
        "category": "commerce",
        "description": "Tracks SKU prices across marketplaces, keeps a price history "
                       "and flags moves above a threshold.",
        "workflow_template": "monitor_market",
        "price_cents_month": 2900,
        "runs_priced": False,
    },
    {
        "slug": "academic-research",
        "name": "Academic Research Agent",
        "category": "academic",
        "description": "Plans a literature sweep, gathers sources with citations and "
                       "writes a report with per-claim sources.",
        "workflow_template": "research_report",
        "price_cents_month": 0,
        "runs_priced": False,
    },
    {
        "slug": "news-intelligence",
        "name": "News Intelligence Agent",
        "category": "news",
        "description": "Follows beats you care about, deduplicates syndicated copies "
                       "and summarises what is genuinely new.",
        "workflow_template": "research_report",
        "price_cents_month": 1900,
        "runs_priced": False,
    },
    {
        "slug": "data-hygiene",
        "name": "Dataset Hygiene Agent",
        "category": "compliance",
        "description": "Keeps a dataset clean: normalises, deduplicates and versions "
                       "it daily with a changelog.",
        "workflow_template": "dataset_hygiene",
        "price_cents_month": 0,
        "runs_priced": False,
    },
]


@dataclass
class AgentManifest:
    """What a publisher ships."""

    slug: str
    name: str
    version: str = "1.0.0"
    author: str = ""
    category: str = "custom"
    description: str = ""
    workflow_template: str = ""
    workflow: dict[str, Any] | None = None
    config_schema: dict[str, Any] = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)   # e.g. ["jobs:create"]
    data_scope: str = "public-web"     # public-web | tenant-corpus | private-sources
    price_cents_month: int = 0
    created_at: float = field(default_factory=time.time)
    checksum: str = ""
    tags: list[str] = field(default_factory=list)
    source_url: str = ""

    def __post_init__(self) -> None:
        self.slug = (self.slug or "").strip().lower().replace(" ", "-")
        if self.category not in CATEGORIES:
            self.category = "custom"
        if not self.checksum:
            self.checksum = self.digest()

    def body(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("checksum", None)
        return d

    def digest(self) -> str:
        blob = json.dumps(self.body(), sort_keys=True, separators=(",", ":"),
                          default=str).encode()
        return "sha256:" + hashlib.sha256(blob).hexdigest()[:32]

    def verify(self) -> bool:
        return (not self.checksum) or self.checksum == self.digest()

    @property
    def ref(self) -> str:
        return f"{self.slug}@{self.version}"

    def validate(self) -> list[str]:
        problems = []
        if not self.slug:
            problems.append("agent needs a slug")
        if not self.workflow and not self.workflow_template:
            problems.append("agent ships neither a workflow nor a template reference")
        if not self.checksum or self.checksum != self.digest():
            problems.append("checksum mismatch — the manifest was edited after signing")
        if self.price_cents_month and not self.author:
            problems.append("a paid agent needs an author for payouts")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ref": self.ref, "digest": self.digest()}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> AgentManifest:
        row = dict(row)
        row.pop("ref", None)
        row.pop("digest", None)
        base = cls(slug=row.get("slug", ""), name=row.get("name", ""))
        for k, v in row.items():
            if hasattr(base, k):
                setattr(base, k, v)
        base.__post_init__()
        return base


class AgentMarket:
    """Registry + installs + revenue ledger (Redis-backed, tenant-scoped)."""

    def __init__(self, redis_client, *, prefix: str = "wcp:agents") -> None:
        self.r = redis_client
        self.prefix = prefix

    def _k(self, *parts: str) -> str:
        return ":".join([self.prefix, *parts])

    @staticmethod
    def _dec(raw) -> str:
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    # -- publishing ---------------------------------------------------------- #
    def publish(self, manifest: AgentManifest) -> dict[str, Any]:
        problems = manifest.validate()
        if problems:
            raise ValueError(f"{manifest.ref} invalid: " + "; ".join(problems))
        self.r.hset(self._k("registry", manifest.slug), manifest.version,
                    json.dumps(manifest.to_dict(), separators=(",", ":"),
                               default=str))
        self.r.zadd(self._k("index"), {manifest.slug: time.time()})
        self.r.sadd(self._k("tags", manifest.category), manifest.slug)
        logger.info("agent published: %s", manifest.ref)
        return {"published": manifest.ref, "checksum": manifest.checksum}

    def catalog(self, *, category: str = "", query: str = "") -> list[dict[str, Any]]:
        slugs = [self._dec(s) for s in self.r.zrevrange(self._k("index"), 0, -1)]
        out: list[dict[str, Any]] = []
        for slug in slugs:
            versions = self.r.hgetall(self._k("registry", slug)) or {}
            if not versions:
                continue
            latest = sorted((self._dec(v) for v in versions), reverse=True)[0]
            raw = versions.get(latest) or versions.get(latest.encode())
            manifest = AgentManifest.from_dict(json.loads(self._dec(raw)))
            if category and manifest.category != category:
                continue
            if query and query.lower() not in f"{manifest.name} {manifest.description}".lower():
                continue
            out.append({**manifest.to_dict(),
                        "installs": int(self.r.hget(self._k("installs", slug),
                                                   "count") or 0),
                        "verified": manifest.verify()})
        if not out and not query:
            out = [{**row, "ref": f"{row['slug']}@1.0.0", "installs": 0,
                    "verified": True, "featured": True} for row in FEATURED
                   if not category or row["category"] == category]
        return out

    def get(self, slug: str, version: str = "") -> AgentManifest:
        versions = self.r.hgetall(self._k("registry", slug)) or {}
        if not versions:
            featured = next((f for f in FEATURED if f["slug"] == slug), None)
            if featured:
                return AgentManifest(**{k: v for k, v in featured.items()
                                        if k in AgentManifest.__dataclass_fields__})
            raise KeyError(slug)
        chosen = version or sorted((self._dec(v) for v in versions), reverse=True)[0]
        raw = versions.get(chosen) or versions.get(chosen.encode())
        return AgentManifest.from_dict(json.loads(self._dec(raw)))

    # -- installing ---------------------------------------------------------- #
    def install(self, tenant: str, slug: str, *, version: str = "",
                config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Install = create a workflow the tenant owns (nothing else runs)."""
        manifest = self.get(slug, version)
        if not manifest.verify():
            raise ValueError(f"{manifest.ref}: checksum does not match — refusing to install")
        from src.workflows.models import Workflow

        workflow = None
        if manifest.workflow:
            workflow = Workflow.from_dict(manifest.workflow)
        elif manifest.workflow_template:
            from src.workflows.models import template

            payload = template(manifest.workflow_template)
            payload["name"] = f"{manifest.name} (installed)"
            payload["description"] = manifest.description
            workflow = Workflow.from_dict(payload)
        else:
            raise ValueError(f"{manifest.ref} ships no workflow")

        workflow.tenant = tenant
        workflow.workflow_id = "wf_" + uuid.uuid4().hex[:14]
        workflow.state = "draft"
        if config:
            for node in workflow.nodes:
                for key, value in config.items():
                    if key in node.config:
                        node.config[key] = value
        stored = self.r.hset(self._k("installs", slug, tenant), manifest.version,
                             json.dumps(workflow.to_dict(), separators=(",", ":"),
                                        default=str))
        self.r.hincrby(self._k("installs", slug), "count", 1)
        self._revenue(tenant, manifest, kind="install")
        return {
            "install_id": f"{slug}:{manifest.version}:{tenant}",
            "agent": manifest.ref,
            "workflow": workflow.to_dict(),
            "checksum_ok": manifest.verify(),
            "permissions_required": manifest.permissions,
            "data_scope": manifest.data_scope,
            "previous": bool(stored),
            "note": "the workflow is yours: edit, schedule or delete it freely",
        }

    def installed(self, tenant: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for slug in (self._dec(s) for s in self.r.zrevrange(self._k("index"), 0, -1)):
            raw = self.r.hget(self._k("installs", slug, tenant), )
            all_versions = self.r.hgetall(self._k("installs", slug, tenant)) or {}
            if not all_versions:
                continue
            for version, payload in all_versions.items():
                try:
                    workflow = json.loads(self._dec(payload))
                except json.JSONDecodeError:
                    continue
                out.append({"agent": f"{slug}@{self._dec(version)}",
                            "workflow_id": workflow.get("workflow_id"),
                            "name": workflow.get("name"),
                            "state": workflow.get("state"),
                            "nodes": len(workflow.get("nodes") or [])})
        return out

    # -- revenue ------------------------------------------------------------- #
    def _revenue(self, tenant: str, manifest: AgentManifest, *, kind: str,
                 units: int = 1) -> dict[str, Any]:
        gross = int(manifest.price_cents_month * units)
        publisher = int(round(gross * (1 - PLATFORM_SHARE)))
        entry = {
            "entry_id": "rev_" + uuid.uuid4().hex[:14],
            "agent": manifest.ref,
            "slug": manifest.slug,
            "author": manifest.author,
            "tenant": tenant,
            "kind": kind,
            "units": units,
            "gross_cents": gross,
            "publisher_cents": publisher,
            "platform_cents": gross - publisher,
            "split": {"publisher": 1 - PLATFORM_SHARE, "platform": PLATFORM_SHARE},
            "at": time.time(),
        }
        self.r.zadd(self._k("revenue", manifest.slug),
                    {json.dumps(entry, separators=(",", ":")): entry["at"]})
        self.r.zadd(self._k("revenue_all"),
                    {json.dumps(entry, separators=(",", ":")): entry["at"]})
        return entry

    def record_run(self, tenant: str, slug: str, *, units: int = 1) -> dict[str, Any]:
        manifest = self.get(slug)
        self.r.hincrby(self._k("runs", slug), tenant, units)
        return self._revenue(tenant, manifest, kind="run", units=units)

    def revenue(self, slug: str = "", *, limit: int = 200) -> dict[str, Any]:
        key = self._k("revenue", slug) if slug else self._k("revenue_all")
        rows = [json.loads(self._dec(r)) for r in self.r.zrevrange(key, 0, limit - 1)]
        return {
            "entries": rows,
            "gross_cents": sum(r["gross_cents"] for r in rows),
            "publisher_cents": sum(r["publisher_cents"] for r in rows),
            "platform_cents": sum(r["platform_cents"] for r in rows),
            "split": {"publisher": 1 - PLATFORM_SHARE, "platform": PLATFORM_SHARE},
        }

    def stats(self) -> dict[str, Any]:
        slugs = [self._dec(s) for s in self.r.zrange(self._k("index"), 0, -1)]
        installs = sum(int(self.r.hget(self._k("installs", s), "count") or 0)
                       for s in slugs)
        return {
            "agents": len(slugs),
            "installs": installs,
            "paid_agents": sum(1 for s in slugs
                               if self._manifest_safe(s).price_cents_month),
            "featured": len(FEATURED),
            "platform_share": PLATFORM_SHARE,
        }

    def _manifest_safe(self, slug: str) -> AgentManifest:
        try:
            return self.get(slug)
        except Exception:  # noqa: BLE001
            return AgentManifest(slug=slug, name=slug)


def featured_catalogue() -> list[dict[str, Any]]:
    return [dict(row) for row in FEATURED]
