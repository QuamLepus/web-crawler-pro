"""Semantic search over crawled content using ChromaDB + embeddings.

Embeddings default to sentence-transformers (local) or OpenAI; swap via
EMBEDDING_PROVIDER env var. Falls back to a hash-based embedding so the
module remains testable without network/model downloads.
"""

from __future__ import annotations

import hashlib
import math
import os


class _HashEmbedder:
    """Deterministic bag-of-hashed-ngrams embedding (offline fallback)."""

    DIM = 384

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0] * self.DIM
            words = t.lower().split()
            for w in words:
                idx = int(hashlib.md5(w.encode()).hexdigest(), 16) % self.DIM
                vec[idx] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class SemanticSearch:
    def __init__(self, collection: str = "crawl_content",
                 persist_dir: str = "data/chroma"):
        self.collection_name = collection
        self.persist_dir = persist_dir
        self._client = None
        self._collection = None
        self._embedder = None

    # -- embedder --------------------------------------------------------- #
    @property
    def embedder(self):
        if self._embedder is None:
            provider = os.getenv("EMBEDDING_PROVIDER", "hash")
            if provider == "openai":
                from langchain_openai import OpenAIEmbeddings

                self._embedder = _LCAdapter(OpenAIEmbeddings())
            elif provider == "sentence-transformers":
                from sentence_transformers import SentenceTransformer

                model = SentenceTransformer(os.getenv(
                    "ST_MODEL", "all-MiniLM-L6-v2"))
                self._embedder = _STAdapter(model)
            else:
                self._embedder = _HashEmbedder()
        return self._embedder

    # -- chroma ------------------------------------------------------------#
    @property
    def collection(self):
        if self._collection is None:
            import chromadb

            os.makedirs(self.persist_dir, exist_ok=True)
            self._client = chromadb.PersistentClient(path=self.persist_dir)
            self._collection = self._client.get_or_create_collection(
                self.collection_name)
        return self._collection

    def add_document(self, doc_id: str, text: str, metadata: dict | None = None):
        emb = self.embedder.embed([text])[0]
        self.collection.upsert(ids=[doc_id], embeddings=[emb],
                               documents=[text], metadatas=[metadata or {}])

    def add_documents(self, docs: list[dict]):
        ids = [d["id"] for d in docs]
        texts = [d["text"] for d in docs]
        metas = [d.get("metadata", {}) for d in docs]
        embs = self.embedder.embed(texts)
        self.collection.upsert(ids=ids, embeddings=embs,
                               documents=texts, metadatas=metas)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        q_emb = self.embedder.embed([query])[0]
        res = self.collection.query(query_embeddings=[q_emb], n_results=top_k)
        hits = []
        for i in range(len(res["ids"][0])):
            hits.append({
                "id": res["ids"][0][i],
                "text": res["documents"][0][i] if res["documents"] else "",
                "distance": res["distances"][0][i] if res["distances"] else None,
                "metadata": res["metadatas"][0][i] if res["metadatas"] else {},
            })
        return hits


class _LCAdapter:
    def __init__(self, lc_embeddings):
        self._e = lc_embeddings

    def embed(self, texts):
        return self._e.embed_documents(texts)


class _STAdapter:
    def __init__(self, model):
        self._m = model

    def embed(self, texts):
        return self._m.encode(texts, normalize_embeddings=True).tolist()
