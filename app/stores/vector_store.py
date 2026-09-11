"""
Vector store: embedded passage chunks for semantic Q&A retrieval.

This is the "answer open-ended questions about what's being done" layer —
it doesn't need strict schema, just good nearest-neighbor recall over the
raw text extracted from ALLOWED sources.

Uses Milvus with the shared local sentence-transformers model, so no
external embedding API is required. MILVUS_URI decides the topology
without any code change: a path ending in .db runs Milvus Lite embedded
(what the demo deployment uses), while an http:// URI or a Zilliz Cloud
endpoint talks to a real cluster.

In MOCK mode we skip embedding entirely and keep an in-memory list, since
the point of mock mode is to validate ORCHESTRATION, not recall quality.
"""
from __future__ import annotations

import hashlib

from app.config import settings
from app.llm import embeddings
from app.stores.lazy import LazyStore


class _MockVectorStore:
    def __init__(self):
        self._docs: list[dict] = []

    def add_passages(self, city: str, passages):
        for p in passages:
            self._docs.append(
                {
                    "city": city,
                    "dimension": p.dimension,
                    "url": p.url,
                    "title": p.title,
                    "text": p.text,
                }
            )

    def query(self, city: str, question: str, n_results: int = 5):
        # Trivial keyword overlap "retrieval" — good enough to prove the
        # plumbing works in mock mode; real semantic search happens in LIVE.
        words = [w.lower() for w in question.split() if len(w) > 3]
        scored = [
            dict(d, score=sum(w in d["text"].lower() for w in words))
            for d in self._docs
            if d["city"] == city
        ]
        scored = [d for d in scored if d["score"] > 0] or scored
        return sorted(scored, key=lambda d: d["score"], reverse=True)[:n_results]


class _MilvusVectorStore:
    def __init__(self):
        from pymilvus import MilvusClient

        self.collection_name = settings.MILVUS_COLLECTION
        self.dimension = embeddings.embedding_dim()
        self.client = MilvusClient(
            uri=settings.MILVUS_URI,
            token=settings.MILVUS_TOKEN or None,
        )

        if not self.client.has_collection(collection_name=self.collection_name):
            # Quick-setup defaults would give an Int64 primary key and a vector
            # field called "vector"; passages are keyed by URL hash instead, so
            # the id type and field name are both set explicitly here.
            self.client.create_collection(
                collection_name=self.collection_name,
                dimension=self.dimension,
                metric_type="COSINE",
                id_type="string",
                max_length=64,
                auto_id=False,
                vector_field_name="vector",
            )

    @staticmethod
    def _passage_id(url: str) -> str:
        """Stable 64-char id so re-researching a city upserts the same row
        rather than duplicating it, and long URLs can't overflow the key."""
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def add_passages(self, city: str, passages):
        if not passages:
            return

        vectors = embeddings.embed_many([p.text for p in passages])
        data = [
            {
                "id": self._passage_id(p.url),
                "vector": vector,
                "city": city,
                "dimension": p.dimension,
                "url": p.url,
                "title": p.title,
                "text": p.text,
            }
            for p, vector in zip(passages, vectors)
        ]
        self.client.upsert(collection_name=self.collection_name, data=data)

    def query(self, city: str, question: str, n_results: int = 5):
        results = self.client.search(
            collection_name=self.collection_name,
            data=[embeddings.embed_one(question)],
            # Scoped to the city so one city's passages can never be cited as
            # evidence about another — the collection is shared across cities.
            filter=f'city == "{self._escape(city)}"',
            limit=n_results,
            output_fields=["city", "dimension", "url", "title", "text"],
        )

        matches = []
        for hit in results[0]:
            entity = hit.get("entity", {})
            matches.append({
                "text": entity.get("text"),
                "city": entity.get("city"),
                "dimension": entity.get("dimension"),
                "url": entity.get("url"),
                "title": entity.get("title"),
                "score": hit.get("distance"),
            })
        return matches

    @staticmethod
    def _escape(value: str) -> str:
        """City names are user input and go into a Milvus filter expression."""
        return value.replace("\\", "\\\\").replace('"', '\\"')


vector_store = LazyStore(
    lambda: _MockVectorStore() if settings.is_mock else _MilvusVectorStore()
)
