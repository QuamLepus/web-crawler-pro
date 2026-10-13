"""RAG (Retrieval-Augmented Generation) chain over crawled corpora.

Backends (selected via ``RAG_BACKEND`` env or constructor):
    * ``chroma``  — ChromaDB persistent collection (default when installed)
    * ``qdrant``  — Qdrant client/REST (when installed / server running)
    * ``memory``  — pure-python cosine index (always available; CI default)

Embeddings: sentence-transformers or OpenAI when configured, otherwise the
deterministic hash embedder from :mod:`src.ai.vector_search` so the chain is
testable with zero downloads.

Answer synthesis uses an injected LLM callable; without one it degrades to an
*extractive* answer (best-matching sentences + citations), which keeps the
pipeline useful in air-gapped environments.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from src.utils.logger import setup_logger

logger = setup_logger(__name__)


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #
class HashEmbedder:
    """Deterministic bag-of-hashed-trigrams embedding (offline fallback)."""

    DIM = 512

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0] * self.DIM
            words = re.findall(r"[a-z0-9]+", t.lower())
            for w in words:
                for n in (w, w[:4]):  # word + prefix for robustness
                    idx = int(hashlib.md5(n.encode()).hexdigest(), 16) % self.DIM
                    vec[idx] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


def get_embedder(prefer: str | None = None):
    """Return an object with ``.embed(list[str]) -> list[list[float]]``."""
    prefer = prefer or os.getenv("EMBEDDING_PROVIDER", "auto")
    if prefer in ("auto", "sentence"):
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(
                os.getenv("SENTENCE_MODEL", "all-MiniLM-L6-v2"))

            class _ST:
                def embed(self, texts):
                    return [list(map(float, v)) for v in
                            model.encode(texts, normalize_embeddings=True)]

            logger.info("RAG embeddings: sentence-transformers")
            return _ST()
        except Exception:  # pragma: no cover - optional dep
            if prefer == "sentence":
                raise
    if prefer in ("auto", "openai") and os.getenv("OPENAI_API_KEY"):
        try:
            from openai import OpenAI

            client = OpenAI()

            class _OA:
                def embed(self, texts):
                    r = client.embeddings.create(
                        model=os.getenv("EMBED_MODEL",
                                        "text-embedding-3-small"), input=texts)
                    return [d.embedding for d in r.data]

            logger.info("RAG embeddings: openai")
            return _OA()
        except Exception as exc:  # pragma: no cover
            logger.warning("openai embeddings unavailable: %s", exc)
    logger.info("RAG embeddings: deterministic hash fallback")
    return HashEmbedder()


# --------------------------------------------------------------------------- #
# Vector-store backends
# --------------------------------------------------------------------------- #
@dataclass
class Chunk:
    doc_id: str
    text: str
    metadata: dict = field(default_factory=dict)
    vector: Optional[list[float]] = None


class MemoryVectorStore:
    """Tiny numpy-free cosine index. Always works; used in tests."""

    def __init__(self):
        self.chunks: list[Chunk] = []

    def add(self, chunks: Iterable[Chunk]) -> int:
        n = 0
        for c in chunks:
            assert c.vector is not None
            self.chunks.append(c)
            n += 1
        return n

    def query(self, vector: list[float], top_k: int = 5) -> list[tuple[Chunk, float]]:
        scored = []
        for c in self.chunks:
            sim = _cosine(vector, c.vector)
            scored.append((c, sim))
        scored.sort(key=lambda cs: -cs[1])
        return scored[:top_k]

    def count(self) -> int:
        return len(self.chunks)


class ChromaVectorStore:
    """ChromaDB-backed store (persistent local or server)."""

    def __init__(self, collection: str = "crawl_content",
                 persist_dir: str = "data/chroma"):
        import chromadb

        self._client = chromadb.PersistentClient(path=persist_dir)
        self._col = self._client.get_or_create_collection(
            name=collection, metadata={"hnsw:space": "cosine"})

    def add(self, chunks: Iterable[Chunk]) -> int:
        ids, docs, metas, vecs = [], [], [], []
        for i, c in enumerate(chunks):
            ids.append(f"{c.doc_id}:{i}:{_short_hash(c.text)}")
            docs.append(c.text)
            metas.append({**c.metadata, "doc_id": c.doc_id})
            vecs.append(c.vector)
        if ids:
            self._col.upsert(ids=ids, documents=docs, metadatas=metas,
                             embeddings=vecs)
        return len(ids)

    def query(self, vector: list[float], top_k: int = 5):
        res = self._col.query(query_embeddings=[vector], n_results=top_k)
        out = []
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0],
                                   res["distances"][0]):
            chunk = Chunk(doc_id=meta.get("doc_id", "?"), text=doc,
                          metadata=meta, vector=None)
            out.append((chunk, 1.0 - dist))  # cosine distance -> similarity
        return out

    def count(self) -> int:
        return self._col.count()


class QdrantVectorStore:
    """Qdrant-backed store (embedded client or remote REST)."""

    def __init__(self, collection: str = "crawl_content",
                 dim: int = 512, url: str | None = None):
        self.collection = collection
        self.dim = dim
        if url:  # remote server via plain REST (no grpc needed)
            self._rest_url = url.rstrip("/")
            self._client = None
            self._ensure_rest_collection()
        else:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams

            self._client = QdrantClient(path=os.getenv(
                "QDRANT_PATH", "data/qdrant"))
            if not self._client.collection_exists(collection):
                self._client.create_collection(
                    collection_name=collection,
                    vectors_config=VectorParams(size=dim,
                                                distance=Distance.COSINE))
            self._rest_url = None

    def _ensure_rest_collection(self) -> None:  # pragma: no cover
        import requests

        r = requests.get(f"{self._rest_url}/collections/{self.collection}",
                         timeout=10)
        if r.status_code == 404:
            requests.put(f"{self._rest_url}/collections/{self.collection}",
                         json={"vectors": {"size": self.dim,
                                           "distance": "Cosine"}},
                         timeout=10).raise_for_status()

    def add(self, chunks: Iterable[Chunk]) -> int:
        points = [{"id": _short_hash(c.text) + f"{i:08d}",
                   "vector": c.vector,
                   "payload": {**c.metadata, "doc_id": c.doc_id,
                               "text": c.text}}
                  for i, c in enumerate(chunks)]
        if not points:
            return 0
        if self._rest_url:  # pragma: no cover
            import requests

            requests.post(f"{self._rest_url}/collections/{self.collection}"
                          "/points?wait=true", json={"points": points},
                          timeout=30).raise_for_status()
        else:
            from qdrant_client.models import PointStruct

            self._client.upsert(collection_name=self.collection,
                                points=[PointStruct(**p) for p in points])
        return len(points)

    def query(self, vector: list[float], top_k: int = 5):
        if self._rest_url:  # pragma: no cover
            import requests

            r = requests.post(
                f"{self._rest_url}/collections/{self.collection}/points/search",
                json={"vector": vector, "limit": top_k}, timeout=15)
            hits = r.json()["result"]
            return [(Chunk(doc_id=h["payload"].get("doc_id", "?"),
                           text=h["payload"].get("text", ""),
                           metadata=h["payload"], vector=None), h["score"])
                    for h in hits]
        hits = self._client.query_points(collection_name=self.collection,
                                         query=vector, limit=top_k).points
        return [(Chunk(doc_id=h.payload.get("doc_id", "?"),
                       text=h.payload.get("text", ""),
                       metadata=h.payload, vector=None), h.score)
                for h in hits]

    def count(self) -> int:
        if self._rest_url:  # pragma: no cover
            import requests

            return requests.get(
                f"{self._rest_url}/collections/{self.collection}",
                timeout=10).json()["result"]["points_count"]
        return self._client.count(collection_name=self.collection).count


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Text splitting
# --------------------------------------------------------------------------- #
def split_text(text: str, chunk_size: int = 800, overlap: int = 120,
               ) -> list[str]:
    """Word-boundary chunker with overlap (LLM-context friendly)."""
    words = text.split()
    if not words:
        return []
    chunks, step = [], max(1, chunk_size - overlap)
    for start in range(0, len(words), step):
        piece = " ".join(words[start:start + chunk_size])
        if piece:
            chunks.append(piece)
        if start + chunk_size >= len(words):
            break
    return chunks


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #
RAG_PROMPT = """Answer the question using ONLY the provided context.
Cite sources as [doc_id]. If the context is insufficient, say so.

QUESTION: {question}

CONTEXT:
{context}

ANSWER:"""


@dataclass
class RAGAnswer:
    question: str
    answer: str
    sources: list[dict]
    scores: list[float]


class RAGChain:
    """Index-and-query retrieval-augmented QA pipeline."""

    def __init__(self, backend: str | None = None, llm: Callable[[str], str] | None = None,
                 embedder=None, collection: str = "crawl_content",
                 top_k: int = 4, chunk_size: int = 800):
        self.embedder = embedder or get_embedder()
        self.top_k = top_k
        self.chunk_size = chunk_size
        self.llm = llm if llm is not None else self._default_llm()
        backend = backend or os.getenv("RAG_BACKEND", "auto")
        self.store = self._make_store(backend, collection)

    @staticmethod
    def _make_store(backend: str, collection: str):
        if backend in ("auto", "chroma"):
            try:
                store = ChromaVectorStore(collection=collection)
                logger.info("RAG store: chromadb")
                return store
            except Exception as exc:
                if backend == "chroma":
                    raise
                logger.warning("chromadb unavailable (%s)", exc)
        if backend in ("auto", "qdrant"):
            try:
                store = QdrantVectorStore(collection=collection,
                                          url=os.getenv("QDRANT_URL") or None)
                logger.info("RAG store: qdrant")
                return store
            except Exception as exc:
                if backend == "qdrant":
                    raise
                logger.warning("qdrant unavailable (%s)", exc)
        logger.info("RAG store: in-memory cosine index")
        return MemoryVectorStore()

    @staticmethod
    def _default_llm():
        """OpenAI chat completion if key present, else None (extractive)."""
        if not os.getenv("OPENAI_API_KEY"):
            return None
        try:
            from openai import OpenAI

            client = OpenAI()

            def _call(prompt: str) -> str:
                r = client.chat.completions.create(
                    model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=512)
                return r.choices[0].message.content.strip()

            return _call
        except Exception:  # pragma: no cover
            return None

    # -- indexing ---------------------------------------------------------- #
    def ingest_document(self, doc_id: str, text: str,
                        metadata: dict | None = None) -> int:
        return self.ingest_documents(
            [{"doc_id": doc_id, "text": text, "metadata": metadata or {}}])

    def ingest_documents(self, docs: list[dict]) -> int:
        chunks: list[Chunk] = []
        for d in docs:
            for piece in split_text(d["text"], chunk_size=self.chunk_size):
                chunks.append(Chunk(doc_id=d["doc_id"], text=piece,
                                    metadata=d.get("metadata", {})))
        if not chunks:
            return 0
        vecs = self.embedder.embed([c.text for c in chunks])
        for c, v in zip(chunks, vecs):
            c.vector = v
        return self.store.add(chunks)

    # -- retrieval ---------------------------------------------------------- #
    def retrieve(self, question: str, top_k: int | None = None
                 ) -> tuple[list[Chunk], list[float]]:
        qv = self.embedder.embed([question])[0]
        hits = self.store.query(qv, top_k or self.top_k)
        return [c for c, _ in hits], [s for _, s in hits]

    # -- answering ------------------------------------------------------------ #
    def answer(self, question: str) -> RAGAnswer:
        chunks, scores = self.retrieve(question)
        sources = [{"doc_id": c.doc_id, "text": c.text[:200],
                   "score": round(s, 4)} for c, s in zip(chunks, scores)]
        if not chunks:
            return RAGAnswer(question, "No indexed context available.", [], [])
        context = "\n\n".join(f"[{c.doc_id}] {c.text}" for c in chunks)
        if self.llm is not None:
            try:
                ans = self.llm(RAG_PROMPT.format(question=question,
                                                 context=context))
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM answer failed (%s); extractive fallback",
                               exc)
                ans = self._extractive(question, chunks)
        else:
            ans = self._extractive(question, chunks)
        return RAGAnswer(question=question, answer=ans, sources=sources,
                         scores=scores)

    @staticmethod
    def _extractive(question: str, chunks: list[Chunk]) -> str:
        """Pick the sentence(s) with most lexical overlap with the question."""
        qw = set(re.findall(r"[a-z0-9]+", question.lower()))
        best: tuple[float, str, str] = (0.0, "", "")
        for c in chunks:
            for sent in re.split(r"(?<=[.!?])\s+", c.text):
                sw = set(re.findall(r"[a-z0-9]+", sent.lower()))
                ov = len(qw & sw)
                if ov > best[0]:
                    best = (ov, c.doc_id, sent.strip())
        if best[0] == 0:
            return f"Possible answer: {chunks[0].text[:200]} [{chunks[0].doc_id}]"
        return f"Possible answer: {best[2]} [{best[1]}]"

    def count(self) -> int:
        return self.store.count()


def build_rag_chain(**kwargs) -> RAGChain:
    """Factory used by Celery/API tasks (keeps call-sites decoupled)."""
    return RAGChain(**kwargs)
