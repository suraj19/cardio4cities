"""
Persistence Agent — writes the pass's findings into all three datastores.

This is where the fact-checking agent's verdict gets its teeth (non-
negotiable #4's "consequences in the workflow"):

  VERIFIED / SINGLE_SOURCE / CONFLICTING  -> written to graph + relational
                                              audit trail as Facts
  UNSUPPORTED                             -> NEVER written as a fact.
                                              Converted into a Gap instead.

What goes where, and why:

  relational  every crawl decision (allowed AND denied) and every fact
              verdict with its reasoning. Exact, row-level, auditable.
  vector      raw passage text from allowed sources. Fuzzy recall for
              open-ended questions the schema never anticipated.
  graph       city, facts and their evidence, as temporal episodes. The
              only store that can answer relational questions across
              entities, and the only one that keeps history when a fact
              is later superseded.

Failure policy: the relational write is required — losing the audit trail
means losing the evidence guarantee, so that error propagates. The vector
and graph writes are best-effort and downgrade to a warning on the run,
because an expired Neo4j Sandbox should cost the user graph exploration,
not the entire research run they just waited two minutes for.
"""
import asyncio

from app.config import settings
from app.graph.state import CityResearchState
from app.models.schemas import ConfidenceTier, Gap
from app.stores.graph_store import graph_store
from app.stores.relational_store import relational_store
from app.stores.vector_store import vector_store

_RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "429", "quota", "resource_exhausted")


def _graph_failure_hint(exc: Exception) -> str:
    """Point the reader at the system that is actually broken.

    Three failures reach this path and they need three different answers. A
    TypeError is a Graphiti API mismatch, so naming the Sandbox would send
    someone to debug the wrong system entirely. A rate limit is the LLM
    provider, which is unobvious here: Graphiti runs its own entity
    extraction per episode, so writing the graph costs several model calls per
    fact and is usually the first thing to exhaust a free-tier quota — the
    symptom being a storm of `Retrying _generate_response_with_retry` lines.
    Anything else is most likely the Sandbox having expired.
    """
    if isinstance(exc, (TypeError, AttributeError, ImportError)):
        return (
            "This is an API mismatch, not an outage — check the installed "
            "graphiti-core against the version in requirements.txt."
        )

    message = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in message for marker in _RATE_LIMIT_MARKERS):
        return (
            "This is the LLM provider's rate limit, not Neo4j. Graphiti runs "
            "its own entity extraction for every episode, so each fact costs "
            "several model calls — it is normally the largest LLM consumer in "
            "a run and the first thing to hit a free-tier quota."
        )

    return "Check the Neo4j Sandbox has not expired."


async def graph_writer_node(state: CityResearchState) -> dict:
    city = state["city"]
    fact_checked = state.get("fact_checked", [])
    crawl_results = state.get("crawl_results", [])
    candidates_by_url = {c.url: c for c in state.get("candidates", [])}

    already_persisted = set(state.get("persisted_claim_ids", []))
    already_indexed = set(state.get("indexed_urls", []))

    new_facts = [fc for fc in fact_checked if fc.claim_id not in already_persisted]
    new_passages = [p for p in state.get("passages", []) if p.url not in already_indexed]

    warnings: list[str] = []

    # --- Relational: source registry (every crawl decision, allowed or denied) ---
    relational_store.record_sources(city, crawl_results, candidates_by_url)

    # --- Split by consequence: unsupported claims become gaps, not facts ---
    persistable = [fc for fc in new_facts if fc.tier != ConfidenceTier.UNSUPPORTED]
    new_gaps = [
        Gap(
            description=fc.text,
            dimension=fc.dimension,
            reason=f"Fact-check agent could not corroborate this claim: {fc.reasoning}",
        )
        for fc in new_facts
        if fc.tier == ConfidenceTier.UNSUPPORTED
    ]

    # --- Relational: fact audit trail (only persistable facts) ---
    relational_store.record_facts(city, persistable)

    # --- Vector: raw passages from allowed sources, for semantic Q&A ---
    # On a worker thread because embedding a batch of passages is CPU-bound
    # and synchronous: called directly, it blocks the event loop for as long
    # as the encode takes, which stalls every other request — including the
    # progress polls asking how this very run is getting on.
    #
    # The lambda is load-bearing, for the same reason it is in /ask:
    # `vector_store` is lazy, so *touching* `.add_passages` is what builds the
    # store and loads the embedding model. Passing the bound method to
    # to_thread would resolve it on the event loop and do that load there —
    # moving the encode off the loop while leaving the worse stall on it.
    try:
        await asyncio.to_thread(lambda: vector_store.add_passages(city, new_passages))
    except Exception as exc:
        warnings.append(
            f"Vector store unavailable ({type(exc).__name__}: {exc}) — semantic "
            f"question answering will be degraded for this run."
        )

    # --- Graph: Neo4j/Graphiti, evidence-linked facts ---
    # The city node and the individual facts are handled separately because
    # their failures mean different things. Without the city node there is
    # nothing for facts to attach to, so that aborts the graph step; but one
    # fact failing should cost that fact alone. These used to share a single
    # try block, which meant the first bad episode abandoned every fact after
    # it — so on a rate-limited provider a single 429 produced an entirely
    # empty graph instead of a partial one.
    try:
        await graph_store.upsert_city(city)
    except Exception as exc:
        warnings.append(
            f"Graph store unavailable ({type(exc).__name__}: {exc}) — facts are "
            f"still in the relational audit trail, but graph exploration is "
            f"unavailable for this run. {_graph_failure_hint(exc)}"
        )
    else:
        # This loop is the run's longest stretch of wall-clock time and the
        # reason `graph_writer` dominates the node timings: Graphiti runs its
        # own entity and relationship extraction for every episode, so each
        # fact is several model calls, and forty facts done strictly one after
        # another is minutes.
        #
        # GRAPH_WRITE_CONCURRENCY defaults to 1, which keeps exactly the old
        # behaviour, because concurrency here is not free: `add_episode`
        # resolves each entity against what the graph already holds, so two
        # episodes naming the same organization at once can both decide it is
        # new. The bound is a semaphore rather than a plain gather so raising
        # it cannot turn into an unbounded fan-out against the provider on a
        # city that yielded eighty facts.
        slots = asyncio.Semaphore(max(settings.GRAPH_WRITE_CONCURRENCY, 1))

        async def write(fc) -> Exception | None:
            async with slots:
                try:
                    await graph_store.add_programme_fact(
                        city=city,
                        claim_text=fc.text,
                        dimension=fc.dimension,
                        tier=fc.tier.value,
                        source_urls=fc.evidence_urls,
                        claim_id=fc.claim_id,
                    )
                except Exception as exc:
                    return exc
                return None

        # `write` returns its exception instead of raising, so gather never
        # has one to propagate and one bad episode cannot abandon the facts
        # queued behind it.
        outcomes = await asyncio.gather(*(write(fc) for fc in persistable))
        failures: list[Exception] = [exc for exc in outcomes if exc is not None]

        if failures:
            # One warning for the batch, not one per fact: these fail for the
            # same reason in practice, and 40 identical lines would bury the
            # rest of the run's warnings.
            example = failures[0]
            warnings.append(
                f"{len(failures)} of {len(persistable)} fact(s) could not be "
                f"written to the knowledge graph ({type(example).__name__}: "
                f"{example}). The others were written, and all of them are in "
                f"the relational audit trail. {_graph_failure_hint(example)}"
            )

    return {
        "gaps": new_gaps,
        "warnings": warnings,
        "persisted_claim_ids": [fc.claim_id for fc in new_facts],
        "indexed_urls": [p.url for p in new_passages],
    }
