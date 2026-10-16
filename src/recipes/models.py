"""Extraction recipes — the self-healing, shareable unit of scraping work.

A recipe is a small, versioned, testable description of how to read one kind
of page:

    wcp recipe install bdnews24-article@2
    wcp recipe test bdnews24-article

Three ideas make it more than a selector file:

**It carries its own tests.** Every recipe ships HTML fixtures with expected
output, so "does this still work" is a command, not an opinion — and a recipe
that fails its tests refuses to publish.

**It heals itself, and says so.** When a required field stops matching, the
runtime tries the field's other selectors, then label-based fallbacks, and
records what it had to do (``healed``). A silent self-heal is a silent
regression; a reported one is a maintenance queue.

**It is content-addressed.** The recipe's digest covers every field and test,
so an installed recipe can be verified and shared by hash — the foundation for
the public recipe registry.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

RECIPE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
FIELD_KINDS = ("text", "html", "attr", "jsonld", "regex")
TRANSFORMS = ("none", "strip", "collapse", "number", "price", "url", "date",
              "lower", "list")


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
@dataclass
class Field:
    name: str
    selectors: list[str] = field(default_factory=list)
    attr: str = "text"
    kind: str = "text"
    pattern: str = ""
    transform: str = "collapse"
    required: bool = False
    fallback_labels: list[str] = field(default_factory=list)
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in FIELD_KINDS:
            self.kind = "text"
        if self.transform not in TRANSFORMS:
            self.transform = "collapse"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Field:
        base = cls(name=row.get("name", ""))
        for k, v in row.items():
            if hasattr(base, k):
                setattr(base, k, v)
        base.__post_init__()
        return base


@dataclass
class TestCase:
    name: str
    html: str
    expect: dict[str, Any] = field(default_factory=dict)
    strict: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> TestCase:
        return cls(name=row.get("name", "case"), html=row.get("html", ""),
                   expect=dict(row.get("expect") or {}),
                   strict=bool(row.get("strict")))


@dataclass
class Recipe:
    name: str
    version: str = "1.0.0"
    description: str = ""
    hosts: list[str] = field(default_factory=list)
    fields: list[Field] = field(default_factory=list)
    tests: list[TestCase] = field(default_factory=list)
    author: str = ""
    license: str = "MIT"
    tags: list[str] = field(default_factory=list)
    seed_urls: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    checksum: str = ""
    signature: str = ""

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip().lower().replace(" ", "-")
        self.fields = [f if isinstance(f, Field) else Field.from_dict(f)
                       for f in self.fields]
        self.tests = [t if isinstance(t, TestCase) else TestCase.from_dict(t)
                      for t in self.tests]
        self.hosts = [h.lower() for h in self.hosts]
        if not self.checksum:
            self.checksum = self.digest()

    # -- integrity ----------------------------------------------------------- #
    def body(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("checksum", None)
        d.pop("signature", None)
        d.pop("updated_at", None)
        return d

    def digest(self) -> str:
        blob = json.dumps(self.body(), sort_keys=True, separators=(",", ":"),
                          default=str).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()[:32]

    def verify(self) -> bool:
        return (not self.checksum) or self.checksum == self.digest()

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.name:
            problems.append("recipe needs a name")
        if not RECIPE_VERSION_RE.match(self.version or ""):
            problems.append("version must be semver (e.g. 1.2.3)")
        if not self.fields:
            problems.append("recipe has no fields")
        if not any(f.required for f in self.fields):
            problems.append("no required field: an empty extraction would look healthy")
        for f in self.fields:
            if not f.selectors and f.kind not in ("jsonld",):
                problems.append(f"field {f.name!r} has no selectors")
        if not self.tests:
            problems.append("recipe has no tests (fixtures with expected output)")
        if not self.hosts:
            problems.append("recipe declares no hosts — it would never apply")
        if self.checksum and self.checksum != self.digest():
            problems.append("checksum does not match the body (recipe was edited)")
        return problems

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fields"] = [f.to_dict() for f in self.fields]
        d["tests"] = [t.to_dict() for t in self.tests]
        d["ref"] = self.ref
        d["digest"] = self.digest()
        return d

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Recipe:
        row = dict(row)
        row.pop("ref", None)
        row.pop("digest", None)
        return cls(
            name=row.get("name", ""),
            version=row.get("version", "1.0.0"),
            description=row.get("description", ""),
            hosts=list(row.get("hosts") or []),
            fields=[Field.from_dict(f) for f in row.get("fields") or []],
            tests=[TestCase.from_dict(t) for t in row.get("tests") or []],
            author=row.get("author", ""),
            license=row.get("license", "MIT"),
            tags=list(row.get("tags") or []),
            seed_urls=list(row.get("seed_urls") or []),
            created_at=float(row.get("created_at") or time.time()),
            updated_at=float(row.get("updated_at") or time.time()),
            checksum=row.get("checksum", ""),
            signature=row.get("signature", ""),
        )


@dataclass
class Extraction:
    """The result of applying a recipe, including how much it had to improvise."""

    values: dict[str, Any] = field(default_factory=dict)
    found: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    healed: list[str] = field(default_factory=list)
    confidence: float = 0.0
    recipe: str = ""
    checksum_ok: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and self.confidence > 0

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok,
                "confidence": round(self.confidence, 3)}


@dataclass
class RecipeHealth:
    recipe: str
    passed: int = 0
    failed: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    field_coverage: dict[str, float] = field(default_factory=dict)
    verdict: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "field_coverage": {k: round(v, 3) for k, v in
                                                   self.field_coverage.items()}}


def semver_key(version: str) -> tuple:
    parts = []
    for chunk in (version or "0.0.0").split("."):
        try:
            parts.append(int(re.sub(r"\D", "", chunk) or 0))
        except ValueError:
            parts.append(0)
    return tuple(parts + [0, 0, 0])[:3]


def sort_versions(versions: Sequence[str], *, reverse: bool = True) -> list[str]:
    return sorted(versions, key=semver_key, reverse=reverse)
