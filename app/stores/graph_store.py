"""
Graph store: Neo4j Graph Database Sandbox, via Graphiti.

This is the institutional-memory layer: entities (City, Organization,
Programme) and relationships (IMPLEMENTS, PARTNERS_WITH, TARGETS), each
backed by an Evidence node so "where did this come from?" is always
answerable via a graph traversal, not just a lookup in another store.

Graphiti gives us temporal edges for free — if a programme is superseded
later, we don't overwrite the old edge, we add a new one with its own
validity window, so history is preserved (this is what "institutional
memory over time" concretely means in this system).

Graphiti normally wants an OpenAI key for three separate things: chat,
embeddings and reranking. Here only the chat client is remote — the
embedder and cross-encoder both run locally off the shared
sentence-transformers model, so the graph works with any chat provider
and needs no second API key. See app/llm/embeddings.py.

In MOCK mode we use an in-memory adjacency-list stand-in with the same
interface, so the ORCHESTRATION can be verified without a live Neo4j
Sandbox connection. LIVE mode requires a real Neo4j Sandbox URI.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.llm import embeddings
from app.stores.lazy import LazyStore


def _iso(value) -> str | None:
    """Graphiti returns datetimes; the API returns JSON."""
    return value.isoformat() if isinstance(value, datetime) else (value or None)


class _MockGraphStore:
    """In-memory stand-in with the SAME ASYNC interface as the real
    Graphiti-backed store, so calling code (graph_writer_agent) never has
    to branch on RUN_MODE."""

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []

    async def upsert_city(self, city: str):
        self.nodes.setdefault(f"City::{city}", {"type": "City", "name": city})

    async def add_programme_fact(
        self,
        city: str,
        claim_text: str,
        dimension: str,
        tier: str,
        source_urls: list[str],
        claim_id: str,
    ):
        """Represents a fact as: (City)-[:HAS_EVIDENCE]->(Evidence) with the
        Evidence node carrying the tier + source URLs. A fuller schema would
        extract a named Organization/Programme entity from the claim text via
        NER/LLM, which is exactly what Graphiti does in LIVE mode; this
        stand-in models the fact directly against the City node with full
        evidence, which is enough to exercise the traversal + provenance
        pattern end-to-end offline."""
        await self.upsert_city(city)
        evidence_id = f"Evidence::{claim_id}"
        self.nodes[evidence_id] = {
            "fact": claim_text,
            "claim_id": claim_id,
            "dimension": dimension,
            "tier": tier,
            "source_urls": source_urls,
            "valid_at": datetime.now(timezone.utc).isoformat(),
            "invalid_at": None,
        }
        self.edges.append({"from": f"City::{city}", "rel": "HAS_EVIDENCE", "to": evidence_id})

    async def query_facts_for_city(
        self, city: str, question: str | None = None, limit: int = 10
    ) -> list[dict]:
        city_key = f"City::{city}"
        evidence = [
            self.nodes[e["to"]]
            for e in self.edges
            if e["from"] == city_key and e["to"] in self.nodes
        ]
        if question:
            words = [w.lower() for w in question.split() if len(w) > 3]
            ranked = sorted(
                evidence,
                key=lambda n: sum(w in n["fact"].lower() for w in words),
                reverse=True,
            )
            evidence = ranked
        return evidence[:limit]


def _build_local_clients():
    """Graphiti's embedder and cross-encoder, both backed by the local
    sentence-transformers model. Defined inside a function so graphiti_core
    is only imported when LIVE mode actually needs it."""
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.embedder.client import EmbedderClient, EmbedderConfig

    class LocalEmbedder(EmbedderClient):
        def __init__(self):
            self.config = EmbedderConfig(embedding_dim=embeddings.embedding_dim())

        async def create(self, input_data) -> list[float]:
            if isinstance(input_data, str):
                text = input_data
            elif isinstance(input_data, list) and all(isinstance(i, str) for i in input_data):
                text = " ".join(input_data)
            else:
                text = str(input_data)
            return await embeddings.aembed_one(text)

        async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
            return await embeddings.aembed_many(list(input_data_list))

    class LocalReranker(CrossEncoderClient):
        """Cosine similarity against the same bi-encoder. A dedicated
        cross-encoder would rank better, but it is a second model download
        for marginal gain at this corpus size, and this keeps the demo VM
        to one embedding model."""

        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            if not passages:
                return []
            vectors = await embeddings.aembed_many([query, *passages])
            query_vec, passage_vecs = vectors[0], vectors[1:]

            def cosine(a: list[float], b: list[float]) -> float:
                dot = sum(x * y for x, y in zip(a, b))
                na = sum(x * x for x in a) ** 0.5
                nb = sum(y * y for y in b) ** 0.5
                return dot / (na * nb) if na and nb else 0.0

            # Map [-1, 1] onto the [0, 1] range Graphiti expects.
            scored = [
                (passage, (cosine(query_vec, vec) + 1.0) / 2.0)
                for passage, vec in zip(passages, passage_vecs)
            ]
            return sorted(scored, key=lambda pair: pair[1], reverse=True)

    return LocalEmbedder(), LocalReranker()


class _GraphitiGraphStore:
    """Real Neo4j Graph Database Sandbox integration via graphiti-core.

    Requires NEO4J_URI/USER/PASSWORD pointed at a running Sandbox instance.
    """

    def __init__(self):
        from graphiti_core import Graphiti
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

        if not settings.LLM_API_KEY:
            raise ValueError(
                "LLM_API_KEY is required for Graphiti LIVE mode — it drives "
                "entity extraction. Set it in your .env before starting the app."
            )
        if not settings.NEO4J_URI:
            raise ValueError(
                "NEO4J_URI is required for Graphiti LIVE mode. Create a free "
                "instance at https://sandbox.neo4j.com and copy its Bolt URI."
            )

        # OpenAIGenericClient rather than OpenAIClient: it injects the JSON
        # schema into the prompt instead of relying on OpenAI's native
        # structured-output API, which most compatible providers don't implement.
        llm_config = LLMConfig(
            api_key=settings.LLM_API_KEY,
            model=settings.LLM_MODEL,
            small_model=settings.LLM_SMALL_MODEL,
            base_url=settings.LLM_BASE_URL,
        )
        embedder, cross_encoder = _build_local_clients()

        self.graphiti = Graphiti(
            uri=settings.NEO4J_URI,
            user=settings.NEO4J_USER,
            password=settings.NEO4J_PASSWORD,
            llm_client=OpenAIGenericClient(config=llm_config),
            embedder=embedder,
            cross_encoder=cross_encoder,
        )
        self._indices_ready = False

    async def _ensure_indices(self):
        """Graphiti needs its indices and constraints built once per database
        before the first episode is ingested, and a new Sandbox starts empty.
        It is idempotent, but guarded so it only costs a round trip once."""
        if self._indices_ready:
            return
        await self.graphiti.build_indices_and_constraints()
        self._indices_ready = True

    async def upsert_city(self, city: str):
        await self._ensure_indices()
        # Graphiti's `add_episode` ingests unstructured text and lets it
        # extract/merge entities itself; for a City anchor node we give it
        # a minimal, unambiguous episode.
        await self.graphiti.add_episode(
            name=f"city_anchor_{city}",
            episode_body=f"{city} is a city participating in the CARDIO4Cities programme.",
            source_description="system_bootstrap",
        )

    async def add_programme_fact(
        self,
        city: str,
        claim_text: str,
        dimension: str,
        tier: str,
        source_urls: list[str],
        claim_id: str,
    ):
        await self._ensure_indices()
        # Graphiti extracts entities and relationships from the episode text,
        # so the tier and sources are written into the body rather than passed
        # as metadata: that way they come back attached to the fact when the
        # edge is retrieved, and provenance survives the extraction step.
        episode_body = (
            f"In {city}: {claim_text} "
            f"[dimension={dimension}; confidence_tier={tier}; claim_id={claim_id}; "
            f"sources={' ; '.join(source_urls) if source_urls else 'none'}]"
        )
        await self.graphiti.add_episode(
            name=f"fact_{claim_id}",
            episode_body=episode_body,
            source_description=f"fact_check_agent/{dimension}",
        )

    async def query_facts_for_city(
        self, city: str, question: str | None = None, limit: int = 10
    ) -> list[dict]:
        """Hybrid retrieval over the graph: Graphiti combines semantic search
        on edge embeddings with a BM25 pass and reranks the union — which is
        why it needs the local embedder and cross-encoder configured above.

        Returns edges, not nodes, because in Graphiti the edge IS the fact
        ("X runs programme Y"), and the edge is what carries the temporal
        validity window. A superseded fact is not deleted, it gains an
        invalid_at — which is what lets this store answer "what did we
        believe about this city six months ago" rather than only "now".
        """
        await self._ensure_indices()
        focus = question or (
            f"healthcare programmes, policy, stakeholders and cardiovascular "
            f"burden in {city}"
        )
        results = await self.graphiti.search(query=f"{city}: {focus}", num_results=limit)

        facts = []
        for edge in results:
            facts.append(
                {
                    "fact": getattr(edge, "fact", ""),
                    "relationship": getattr(edge, "name", ""),
                    "valid_at": _iso(getattr(edge, "valid_at", None)),
                    "invalid_at": _iso(getattr(edge, "invalid_at", None)),
                    "recorded_at": _iso(getattr(edge, "created_at", None)),
                }
            )
        return facts


graph_store = LazyStore(
    lambda: _MockGraphStore() if settings.is_mock else _GraphitiGraphStore()
)
