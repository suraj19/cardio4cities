"""
Vector store: embedded passage chunks for semantic Q&A retrieval.

This is the "answer open-ended questions about what's being done" layer —
it doesn't need strict schema, just good nearest-neighbor recall over the
raw text extracted from ALLOWED sources.

Uses Milvus with the shared local ONNX embedding model, so no
external embedding API is required. MILVUS_URI decides the topology
without any code change: a path ending in .db runs Milvus Lite embedded
(what the demo deployment uses), while an http:// URI or a Zilliz Cloud
endpoint talks to a real cluster.

In MOCK mode we skip embedding entirely and keep an in-memory list, since
the point of mock mode is to validate ORCHESTRATION, not recall quality.
"""
from __future__ import annotations

import hashlib
import logging

from app.config import settings
from app.llm import embeddings
from app.stores.lazy import LazyStore
from app.telemetry import span

logger = logging.getLogger(__name__)


def _type_name(field: dict) -> str:
    """A field's data type as an uppercase name.

    Resolved rather than compared directly, because `describe_collection`
    reports this as a `DataType` enum in some pymilvus versions and as the
    raw integer in others. An identity check against `DataType.VARCHAR`
    silently fails on the integer form, which for the caller below would mean
    deciding a perfectly good collection was broken.
    """
    raw = field.get("type")
    try:
        from pymilvus import DataType

        if isinstance(raw, DataType):
            return raw.name
        return DataType(raw).name
    except Exception:
        return str(raw).upper()


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
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        """Create the collection, or replace one whose schema cannot be written to.

        `has_collection()` answers "does this name exist", which is not the
        question that matters. A collection left behind by an earlier build can
        exist with an **Int64** primary key, and then every upsert fails with
        `DataNotMatchException: {id} field should be a int64` — because passage
        ids are URL hashes. Checking only the name made that state permanent:
        no amount of restarting fixed it, because the create call was skipped
        every time.

        Repairing it by dropping and recreating is safe here in a way it would
        not be for the relational store. This collection is a derived index
        over passages the pipeline re-fetches, not a system of record. The cost
        is `/ask` recall for cities already researched, until they are
        researched again.
        """
        if self.client.has_collection(collection_name=self.collection_name):
            problem = self._schema_problem()
            if problem is None:
                return
            logger.warning(
                "Milvus collection %r is unusable: %s. Dropping and recreating "
                "it. Passage embeddings for previously researched cities are "
                "lost and will be rebuilt the next time those cities are run.",
                self.collection_name,
                problem,
            )
            self.client.drop_collection(collection_name=self.collection_name)

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

    def _schema_problem(self) -> str | None:
        """Why the existing collection is unusable, or None if it is fine.

        Every check fails *open*: anything this cannot positively identify as
        wrong is reported as fine. Dropping a collection is destructive, so a
        false positive here would delete working data on a guess, whereas a
        false negative merely lets the original upsert error surface — loudly,
        and with a schema hint now attached to it.
        """
        try:
            described = self.client.describe_collection(
                collection_name=self.collection_name
            )
        except Exception:
            return None

        fields = described.get("fields") or []

        primary = next((f for f in fields if f.get("is_primary")), None)
        if primary is not None:
            primary_type = _type_name(primary)
            if primary_type in ("INT64", "INT32", "INT16", "INT8"):
                return (
                    f"its primary key {primary.get('name')!r} is {primary_type}, "
                    f"but passage ids are URL hashes and need VARCHAR"
                )

        vector = next((f for f in fields if f.get("name") == "vector"), None)
        if vector is None:
            names = [f.get("name") for f in fields]
            return f"it has no 'vector' field (fields present: {names})"

        try:
            dim = int((vector.get("params") or {}).get("dim"))
        except (TypeError, ValueError):
            return None
        if dim != self.dimension:
            return (
                f"its vectors are {dim}-dimensional, but "
                f"{settings.EMBEDDING_MODEL} produces {self.dimension}"
            )
        return None

    @staticmethod
    def _passage_id(url: str) -> str:
        """Stable 64-char id so re-researching a city upserts the same row
        rather than duplicating it, and long URLs can't overflow the key."""
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def add_passages(self, city: str, passages):
        if not passages:
            return
        with span(
            "milvus.upsert",
            **{
                "db.system": "milvus",
                "db.collection.name": self.collection_name,
                "cardio4cities.city": city,
                "cardio4cities.passages": len(passages),
            },
        ):
            self._upsert(city, passages)

    def _upsert(self, city: str, passages):
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
        with span(
            "milvus.search",
            **{
                "db.system": "milvus",
                "db.collection.name": self.collection_name,
                "cardio4cities.city": city,
                "cardio4cities.limit": n_results,
            },
        ) as current:
            matches = self._search(city, question, n_results)
            current.set_attribute("cardio4cities.matches", len(matches))
            return matches

    def _search(self, city: str, question: str, n_results: int):
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
