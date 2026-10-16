"""Entity extraction, resolution and the knowledge graph.

Crawled pages are mostly names: companies, products, people, technologies.
Storing them as text makes them unqueryable — "which companies does this fund
own, and who founded them" is a graph question, not a keyword question.

Three parts, in order of difficulty:

1. :func:`extract` — cheap, deterministic mention extraction (organisation
   suffixes, known technology vocabulary, person patterns, product phrases).
   Deliberately conservative: a knowledge graph that is 30% wrong is worse
   than no graph, because people trust the picture.
2. :func:`resolve` — *entity resolution*, the part everyone underestimates.
   "Dhaka Bank", "Dhaka Bank Ltd", "DhakaBank" and "DBL" are one entity;
   "Apple Inc" and "Apple Hospitality" are not. Resolution normalises names,
   applies alias/suffix rules and refuses to merge distinct kinds.
3. :class:`GraphStore` — a tenant-scoped graph in Redis with neighbour,
   search and shortest-path queries, plus a `explain` payload for every edge
   (which URL, which sentence, which provenance record): an edge nobody can
   verify is decoration.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------- #
# vocabulary + patterns
# --------------------------------------------------------------------------- #
ENTITY_KINDS = ("company", "product", "person", "technology", "place", "topic")

_ORG_SUFFIXES = (
    "inc", "inc.", "llc", "ltd", "ltd.", "limited", "plc", "gmbh", "corp",
    "corp.", "corporation", "company", "co.", "group", "holdings", "technologies",
    "technology", "labs", "laboratories", "systems", "solutions", "software",
    "foundation", "university", "institute", "bank", "insurance", "ventures",
    "partners", "capital", "industries", "enterprise", "enterprises", "pvt",
    "private", "sa", "ag", "nv", "bv", "ab", "oy", "pte", "sdn",
)

_TECH = {
    "kubernetes", "docker", "postgres", "postgresql", "mysql", "redis", "kafka",
    "python", "rust", "golang", "typescript", "javascript", "react", "next.js",
    "vue", "django", "flask", "fastapi", "celery", "pytorch", "tensorflow",
    "langchain", "llama", "gpt", "bert", "terraform", "ansible", "grafana",
    "prometheus", "elasticsearch", "mongodb", "clickhouse", "spark", "flink",
    "airflow", "dbt", "snowflake", "databricks", "minio", "s3", "graphql",
    "grpc", "rest", "webassembly", "wasm", "wasmtime", "nvim", "linux", "nginx",
    "playwright", "selenium", "puppeteer", "opentelemetry", "sentry", "stripe",
    "chromadb", "qdrant", "milvus", "weaviate", "llamaindex", "huggingface",
}

_RELATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("acquired", re.compile(r"\b([A-Z][\w&.\- ]{2,40}?)\s+(?:has\s+)?acquired\s+([A-Z][\w&.\- ]{2,40})")),
    ("merged_with", re.compile(r"\b([A-Z][\w&.\- ]{2,40}?)\s+merged\s+with\s+([A-Z][\w&.\- ]{2,40})")),
    ("partners_with", re.compile(r"\b([A-Z][\w&.\- ]{2,40}?)\s+(?:has\s+)?partner(?:ed|s|ing)?\s+with\s+([A-Z][\w&.\- ]{2,40})")),
    ("competes_with", re.compile(r"\b([A-Z][\w&.\- ]{2,40}?)\s+competes?\s+with\s+([A-Z][\w&.\- ]{2,40})")),
    ("subsidiary_of", re.compile(r"\b([A-Z][\w&.\- ]{2,40}?)\s+is\s+(?:a\s+)?subsidiar(?:y|ies)\s+of\s+([A-Z][\w&.\- ]{2,40})")),
    ("founded_by", re.compile(r"\b([A-Z][\w&.\- ]{2,40}?)\s+(?:was\s+)?founded\s+by\s+([A-Z][\w'\- ]{2,40})")),
    ("uses", re.compile(r"\b([A-Z][\w&.\- ]{2,40}?)\s+uses?\s+([A-Z][\w&.\- ]{2,40})")),
    ("invested_in", re.compile(r"\b([A-Z][\w&.\- ]{2,40}?)\s+invest(?:ed|s)\s+in\s+([A-Z][\w&.\- ]{2,40})")),
    ("launched", re.compile(r"\b([A-Z][\w&.\- ]{2,40}?)\s+launch(?:ed|es)\s+([A-Z][\w&.\- ]{2,40})")),
]

_PERSON_RE = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})")
_CAP_PHRASE_RE = re.compile(r"\b([A-Z][\w&.\-]*(?:\s+(?:of|and|&|for|the)?\s*[A-Z][\w&.\-]*){0,4})")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n{2,}")

_MAX_NAME_WORDS = 6


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
@dataclass
class Entity:
    kind: str
    name: str
    canonical: str = ""
    aliases: list[str] = field(default_factory=list)
    url: str = ""
    confidence: float = 0.5
    mentions: int = 1
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    evidence: list[str] = field(default_factory=list)     # provenance record ids
    entity_id: str = ""
    tenant: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ENTITY_KINDS:
            self.kind = "topic"
        if not self.canonical:
            self.canonical = canonical_name(self.name, self.kind)
        if not self.entity_id:
            self.entity_id = entity_id(self.canonical, self.kind)
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Entity:
        base = cls(kind=row.get("kind", "topic"), name=row.get("name", ""))
        for k, v in row.items():
            if hasattr(base, k):
                setattr(base, k, v)
        base.__post_init__()
        return base


@dataclass
class Relation:
    subject: str                  # entity_id
    predicate: str
    object: str                   # entity_id
    weight: float = 1.0
    confidence: float = 0.6
    evidence: list[dict[str, Any]] = field(default_factory=list)  # {url, quote, record_id}
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    tenant: str = ""

    @property
    def relation_id(self) -> str:
        return hashlib.sha1(
            f"{self.subject}|{self.predicate}|{self.object}".encode()).hexdigest()[:20]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["relation_id"] = self.relation_id
        return d

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Relation:
        base = cls(subject=row.get("subject", ""), predicate=row.get("predicate", ""),
                   object=row.get("object", ""))
        for k, v in row.items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base


@dataclass
class Mention:
    text: str
    kind: str
    confidence: float
    quote: str = ""
    url: str = ""
    record_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# normalisation + resolution
# --------------------------------------------------------------------------- #
_WS = re.compile(r"\s+")


def canonical_name(name: str, kind: str = "topic") -> str:
    """A stable key for an entity name.

    Lowercases, strips punctuation, drops trailing legal suffixes and removes
    the spaces that people type inconsistently ("Dhaka Bank" / "DhakaBank").
    The suffix strip is why "Acme Ltd" and "Acme Limited" become one node —
    a legal suffix is not part of an identity.
    """
    text = _WS.sub(" ", (name or "").strip())
    text = text.strip(".,;:·•-–—|/()[]\"'")
    lowered = text.lower()
    words = re.split(r"[\s,]+", lowered)
    while words and words[-1].strip(".") in {s.strip(".") for s in _ORG_SUFFIXES}:
        words.pop()
    if not words:
        words = [w for w in re.split(r"[\s,]+", lowered) if w]
    joined = "".join(words) if len(words) > 1 else (words[0] if words else "")
    return joined or lowered


def entity_id(canonical: str, kind: str) -> str:
    return f"{kind[:3]}_{hashlib.sha1(f'{kind}:{canonical}'.encode()).hexdigest()[:16]}"


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2}


def similarity(a: str, b: str) -> float:
    """Token-set Jaccard with a prefix bonus — cheap, explainable, good enough."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    j = len(ta & tb) / len(ta | tb)
    if a and b and a[:4] == b[:4] and min(len(a), len(b)) > 5:
        j = min(1.0, j + 0.15)
    return round(j, 4)


def resolve(name: str, kind: str, known: Sequence[Entity] = (),
            *, threshold: float = 0.62) -> tuple[str, float, str]:
    """``(canonical, confidence, matched_via)``.

    Only entities of the *same kind* are compared: people and companies share
    names far too often, and a wrong merge is far more expensive than a
    duplicate node (it silently deletes an entity).
    """
    canonical = canonical_name(name, kind)
    best: tuple[float, str, str] = (1.0, canonical, "exact")
    for candidate in known:
        if candidate.kind != kind:
            continue
        for alias in [candidate.canonical, candidate.name, *candidate.aliases]:
            if canonical_name(alias, kind) == canonical:
                return candidate.canonical, 0.95, "alias"
            score = similarity(canonical, canonical_name(alias, kind))
            if score > best[0]:
                best = (min(0.9, score), candidate.canonical, "fuzzy")
    if best[0] >= threshold:
        return best[1], best[0], best[2]
    return canonical, 0.55, "new"


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def _is_org_phrase(phrase: str) -> bool:
    words = phrase.split()
    if not words:
        return False
    last = words[-1].lower()
    return last in {s.strip(".") for s in _ORG_SUFFIXES} or \
        any(len(w) > 2 and w.isupper() for w in words)


def _clean_phrase(phrase: str) -> str:
    phrase = _WS.sub(" ", phrase).strip(" ,;:·•-–—|/()[]\"'")
    words = phrase.split()
    if len(words) > _MAX_NAME_WORDS:
        phrase = " ".join(words[:_MAX_NAME_WORDS])
    return phrase


def extract(text: str, *, url: str = "", record_id: str = "",
            max_entities: int = 60) -> list[Mention]:
    """Extract entity mentions from page text.

    Conservatism over recall: only phrases with an organisation suffix, an
    uppercase word, a known technology name or a person pattern are returned.
    """
    if not text:
        return []
    found: dict[tuple[str, str], Mention] = {}
    sentences = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]

    for sentence in sentences[:400]:
        for reference in (sentence, sentence[:160]):
            for m in _PERSON_RE.finditer(reference):
                _add(found, m.group(1), "person", 0.6, sentence, url, record_id)

        for match in _CAP_PHRASE_RE.finditer(sentence):
            phrase = _clean_phrase(match.group(1))
            if len(phrase) < 3 or len(phrase.split()) > _MAX_NAME_WORDS:
                continue
            low = phrase.lower()
            if low in _TECH:
                _add(found, phrase, "technology", 0.85, sentence, url, record_id)
                continue
            if _is_org_phrase(phrase) and len(phrase.split()) >= 2:
                kind = "company" if not phrase.endswith(
                    ("Suite", "Pro", "Cloud", "Platform")) else "product"
                _add(found, phrase, kind, 0.62, sentence, url, record_id)
            elif 1 <= len(phrase.split()) <= 4 and phrase[0].isupper() and \
                    not phrase.isupper():
                # A bare capitalised phrase is weak evidence; keep the weaker
                # kind and the lower confidence so it never outranks a suffix hit.
                _add(found, phrase, "topic", 0.4, sentence, url, record_id)

    rows = sorted(found.values(), key=lambda m: (-m.confidence, m.text))
    return rows[:max_entities]


def _add(found: dict, text: str, kind: str, confidence: float, quote: str,
         url: str, record_id: str) -> None:
    text = _clean_phrase(text)
    if len(text) < 3:
        return
    key = (canonical_name(text, kind), kind)
    if key in found:
        found[key].mentions += 1
        found[key].confidence = min(0.95, found[key].confidence + 0.02)
        return
    found[key] = Mention(text=text, kind=kind, confidence=confidence,
                         quote=quote[:280], url=url, record_id=record_id)


def extract_relations(text: str, *, entities: Sequence[Entity] = (),
                      url: str = "", record_id: str = "") -> list[dict[str, Any]]:
    """Pattern-based relations between two named entities found in one sentence."""
    out: list[dict[str, Any]] = []
    for sentence in [s.strip() for s in _SENTENCE_RE.split(text or "") if s.strip()][:400]:
        for predicate, pattern in _RELATION_PATTERNS:
            for m in pattern.finditer(sentence):
                left, right = _clean_phrase(m.group(1)), _clean_phrase(m.group(2))
                if len(left) < 3 or len(right) < 3 or left.lower() == right.lower():
                    continue
                out.append({
                    "subject_name": left,
                    "object_name": right,
                    "predicate": predicate,
                    "quote": sentence[:280],
                    "url": url,
                    "record_id": record_id,
                    "confidence": 0.7,
                })
    return out


# --------------------------------------------------------------------------- #
# graph store
# --------------------------------------------------------------------------- #
class GraphStore:
    """Tenant-scoped knowledge graph (Redis-backed, adjacency-list shape).

    Keys::

        wcp:graph:{tenant}:ent:{entity_id}      entity JSON
        wcp:graph:{tenant}:entities             ZSET entity_id -> last_seen
        wcp:graph:{tenant}:out:{entity_id}      ZSET relation_id -> weight
        wcp:graph:{tenant}:rel:{relation_id}    relation JSON
        wcp:graph:{tenant}:alias:{canonical}    entity_id (resolution index)
    """

    def __init__(self, redis_client, *, prefix: str = "wcp:graph") -> None:
        self.r = redis_client
        self.prefix = prefix

    def _k(self, tenant: str, *parts: str) -> str:
        return ":".join([self.prefix, tenant or "public", *parts])

    @staticmethod
    def _dec(raw) -> str:
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    # -- writes -------------------------------------------------------------- #
    def upsert_entity(self, tenant: str, entity: Entity) -> Entity:
        existing = self.get_entity(tenant, entity.entity_id)
        if existing is None:
            matched = self._match_alias(tenant, entity)
            if matched:
                existing = self.get_entity(tenant, matched)
        if existing:
            entity.mentions = existing.mentions + entity.mentions
            entity.aliases = sorted({*existing.aliases, *entity.aliases, existing.name}
                                    - {entity.name})[:12]
            entity.evidence = (existing.evidence + entity.evidence)[-25:]
            entity.first_seen = min(existing.first_seen, entity.first_seen)
            entity.confidence = max(existing.confidence, entity.confidence)
        import json

        entity.tenant = tenant
        payload = json.dumps(entity.to_dict(), separators=(",", ":"), default=str)
        pipe = self.r.pipeline()
        pipe.set(self._k(tenant, "ent", entity.entity_id), payload)
        pipe.zadd(self._k(tenant, "entities"), {entity.entity_id: entity.last_seen})
        pipe.set(self._k(tenant, "alias", entity.canonical), entity.entity_id)
        for alias in entity.aliases:
            pipe.set(self._k(tenant, "alias", canonical_name(alias, entity.kind)),
                     entity.entity_id)
        pipe.execute()
        return entity

    def upsert_relation(self, tenant: str, rel: Relation) -> Relation:
        import json

        existing = self.get_relation(tenant, rel.relation_id)
        if existing:
            rel.weight = existing.weight + rel.weight
            rel.evidence = (existing.evidence + rel.evidence)[-25:]
            rel.first_seen = min(existing.first_seen, rel.first_seen)
            rel.confidence = min(0.95, max(existing.confidence, rel.confidence) + 0.05)
        pipe = self.r.pipeline()
        pipe.set(self._k(tenant, "rel", rel.relation_id),
                 json.dumps(rel.to_dict(), separators=(",", ":"), default=str))
        pipe.zadd(self._k(tenant, "out", rel.subject), {rel.relation_id: rel.weight})
        pipe.zadd(self._k(tenant, "in", rel.object), {rel.relation_id: rel.weight})
        pipe.execute()
        return rel

    def _match_alias(self, tenant: str, entity: Entity) -> str:
        for candidate in [entity.canonical, canonical_name(entity.name, entity.kind),
                          *entity.aliases]:
            raw = self.r.get(self._k(tenant, "alias", canonical_name(candidate,
                                                                    entity.kind)))
            if raw:
                return self._dec(raw)
        return ""

    # -- reads --------------------------------------------------------------- #
    def get_entity(self, tenant: str, entity_id_value: str) -> Entity | None:
        import json

        raw = self.r.get(self._k(tenant, "ent", entity_id_value))
        return Entity.from_dict(json.loads(raw)) if raw else None

    def get_relation(self, tenant: str, relation_id: str) -> Relation | None:
        import json

        raw = self.r.get(self._k(tenant, "rel", relation_id))
        return Relation.from_dict(json.loads(raw)) if raw else None

    def find(self, tenant: str, name: str, kind: str = "topic") -> Entity | None:
        """Resolve a name to an entity (exact alias first, then fuzzy)."""
        canonical = canonical_name(name, kind)
        raw = self.r.get(self._k(tenant, "alias", canonical))
        if raw:
            return self.get_entity(tenant, self._dec(raw))
        best, best_score = None, 0.0
        for entity in self.entities(tenant, limit=2000, kind=kind):
            score = similarity(canonical, entity.canonical)
            if score > best_score:
                best, best_score = entity, score
        return best if best_score >= 0.62 else None

    def entities(self, tenant: str, *, limit: int = 100, kind: str = "",
                 ) -> list[Entity]:
        import json

        ids = [self._dec(i) for i in
               self.r.zrevrange(self._k(tenant, "entities"), 0, max(0, limit * 3 - 1))]
        out: list[Entity] = []
        for eid in ids:
            raw = self.r.get(self._k(tenant, "ent", eid))
            if not raw:
                continue
            entity = Entity.from_dict(json.loads(raw))
            if kind and entity.kind != kind:
                continue
            out.append(entity)
            if len(out) >= limit:
                break
        return out

    def edges(self, tenant: str, entity_id_value: str, *, direction: str = "both",
              limit: int = 50) -> list[dict[str, Any]]:
        import json

        ids: list[str] = []
        if direction in ("out", "both"):
            ids += [self._dec(i) for i in
                    self.r.zrevrange(self._k(tenant, "out", entity_id_value),
                                     0, limit - 1)]
        if direction in ("in", "both"):
            ids += [self._dec(i) for i in
                    self.r.zrevrange(self._k(tenant, "in", entity_id_value),
                                     0, limit - 1)]
        rows = []
        for rid in ids[:limit]:
            raw = self.r.get(self._k(tenant, "rel", rid))
            if not raw:
                continue
            rel = Relation.from_dict(json.loads(raw))
            other = rel.object if rel.subject == entity_id_value else rel.subject
            other_entity = self.get_entity(tenant, other)
            rows.append({
                "relation": {"predicate": rel.predicate, "weight": round(rel.weight, 2),
                             "confidence": rel.confidence,
                             "evidence": rel.evidence[-3:]},
                "direction": "out" if rel.subject == entity_id_value else "in",
                "neighbor": other_entity.to_dict() if other_entity else {"entity_id": other},
            })
        return rows

    def search(self, tenant: str, query: str, *, limit: int = 20) -> list[Entity]:
        q = canonical_name(query)
        out = []
        for entity in self.entities(tenant, limit=2000):
            if q in entity.canonical or q in canonical_name(" ".join(entity.aliases)) or \
                    similarity(q, entity.canonical) >= 0.5:
                out.append(entity)
        out.sort(key=lambda e: (-(similarity(q, e.canonical)), -e.mentions))
        return out[:limit]

    def subgraph(self, tenant: str, entity_id_value: str, *, depth: int = 2,
                 limit: int = 40) -> dict[str, Any]:
        """Breadth-first neighbourhood for the visual graph."""
        seen = {entity_id_value: 0}
        frontier = [entity_id_value]
        nodes: dict[str, dict] = {}
        links: list[dict] = []
        while frontier and len(seen) <= limit:
            current = frontier.pop(0)
            if seen[current] > depth:
                continue
            entity = self.get_entity(tenant, current)
            if entity:
                nodes[current] = {**entity.to_dict(), "depth": seen[current]}
            for row in self.edges(tenant, current, limit=limit):
                neighbor = row["neighbor"].get("entity_id")
                if not neighbor:
                    continue
                links.append({"source": current, "target": neighbor,
                              **row["relation"]})
                if neighbor not in seen and len(seen) < limit:
                    seen[neighbor] = seen[current] + 1
                    frontier.append(neighbor)
        return {"nodes": list(nodes.values()), "links": links,
                "root": entity_id_value}

    def stats(self, tenant: str) -> dict[str, Any]:
        entities = self.entities(tenant, limit=5000)
        by_kind = Counter(e.kind for e in entities)
        with_evidence = sum(1 for e in entities if e.evidence)
        return {
            "entities": len(entities),
            "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
            "with_evidence": with_evidence,
            "top": [{"name": e.name, "kind": e.kind, "mentions": e.mentions,
                     "confidence": e.confidence,
                     "entity_id": e.entity_id} for e in
                    sorted(entities, key=lambda e: -e.mentions)[:15]],
        }


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #
class GraphService:
    """Turn collected records into graph nodes and edges, with evidence."""

    def __init__(self, redis_client, *, store: GraphStore | None = None) -> None:
        self.store = store or GraphStore(redis_client)

    def ingest_text(self, tenant: str, text: str, *, url: str = "",
                    record_id: str = "") -> dict[str, Any]:
        mentions = extract(text, url=url, record_id=record_id)
        entities: list[Entity] = []
        for mention in mentions:
            known = self.store.search(tenant, mention.text, limit=5)
            canonical, confidence, matched_via = resolve(mention.text, mention.kind, known)
            entity = Entity(
                kind=mention.kind, name=mention.text, canonical=canonical,
                url=url, confidence=max(confidence, mention.confidence),
                mentions=mention.mentions, evidence=[record_id] if record_id else [],
                attributes={"matched_via": matched_via},
            )
            entities.append(self.store.upsert_entity(tenant, entity))

        by_canonical = {e.canonical: e for e in entities}
        relations = []
        for row in extract_relations(text, entities=entities, url=url,
                                     record_id=record_id):
            subject = by_canonical.get(canonical_name(row["subject_name"], "company")) \
                or self.store.find(tenant, row["subject_name"])
            obj = by_canonical.get(canonical_name(row["object_name"], "company")) \
                or self.store.find(tenant, row["object_name"])
            if not subject or not obj or subject.entity_id == obj.entity_id:
                continue
            rel = Relation(
                subject=subject.entity_id, predicate=row["predicate"],
                object=obj.entity_id, confidence=row["confidence"],
                evidence=[{"url": url, "quote": row["quote"],
                           "record_id": record_id}] if url or record_id else [],
            )
            relations.append(self.store.upsert_relation(tenant, rel))

        return {"entities": [e.to_dict() for e in entities],
                "relations": [r.to_dict() for r in relations],
                "mentions": [m.to_dict() for m in mentions]}

    def explain_edge(self, tenant: str, relation_id: str) -> dict[str, Any]:
        rel = self.store.get_relation(tenant, relation_id)
        if rel is None:
            raise KeyError(relation_id)
        subject = self.store.get_entity(tenant, rel.subject)
        obj = self.store.get_entity(tenant, rel.object)
        return {"relation": rel.to_dict(),
                "subject": subject.to_dict() if subject else None,
                "object": obj.to_dict() if obj else None,
                "evidence": rel.evidence,
                "explanation": (
                    f"{subject.name if subject else rel.subject} "
                    f"{rel.predicate.replace('_', ' ')} "
                    f"{obj.name if obj else rel.object}, "
                    f"seen {rel.weight:.0f}×, source: "
                    + (rel.evidence[-1].get("url", "?") if rel.evidence else "unknown")
                )}


def entity_kinds() -> list[str]:
    return list(ENTITY_KINDS)


def predicate_vocabulary() -> list[str]:
    return [p for p, _ in _RELATION_PATTERNS]


def tech_vocabulary() -> list[str]:
    return sorted(_TECH)


def merge_duplicates(tenant: str, store: GraphStore, *, threshold: float = 0.82
                     ) -> list[dict[str, Any]]:
    """Find (never silently apply) near-duplicate entities for review.

    Merging is the one operation that destroys information, so the platform
    proposes merges with the similarity that triggered them and lets a human
    decide. That is also the honest engineering answer to entity resolution:
    report your uncertainty.
    """
    entities = store.entities(tenant, limit=3000)
    proposals: list[dict[str, Any]] = []
    for i, a in enumerate(entities):
        for b in entities[i + 1:]:
            if a.kind != b.kind:
                continue
            score = similarity(a.canonical, b.canonical)
            if score >= threshold:
                proposals.append({
                    "keep": a.entity_id, "keep_name": a.name,
                    "merge": b.entity_id, "merge_name": b.name,
                    "kind": a.kind, "similarity": score,
                    "reason": f"names normalise to {a.canonical} / {b.canonical}",
                })
    proposals.sort(key=lambda p: -p["similarity"])
    return proposals


def graph_to_mermaid(subgraph: dict[str, Any], *, max_nodes: int = 25) -> str:
    """Mermaid rendering — the graph in a README or a report, no JS needed."""
    nodes = subgraph.get("nodes", [])[:max_nodes]
    ids = {n["entity_id"] for n in nodes}
    lines = ["graph LR"]
    for n in nodes:
        label = str(n.get("name", "?")).replace('"', "'")[:40]
        lines.append(f'  {n["entity_id"]}["{label}"]')
    for link in subgraph.get("links", []):
        if link.get("source") in ids and link.get("target") in ids:
            lines.append(f'  {link["source"]} -->|{link.get("predicate", "")}| '
                         f'{link["target"]}')
    return "\n".join(lines)
