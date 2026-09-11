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
from app.graph.state import CityResearchState
from app.models.schemas import ConfidenceTier, Gap
from app.stores.graph_store import graph_store
from app.stores.relational_store import relational_store
from app.stores.vector_store import vector_store


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
    try:
        vector_store.add_passages(city, new_passages)
    except Exception as exc:
        warnings.append(
            f"Vector store unavailable ({type(exc).__name__}: {exc}) — semantic "
            f"question answering will be degraded for this run."
        )

    # --- Graph: Neo4j/Graphiti, evidence-linked facts ---
    try:
        await graph_store.upsert_city(city)
        for fc in persistable:
            await graph_store.add_programme_fact(
                city=city,
                claim_text=fc.text,
                dimension=fc.dimension,
                tier=fc.tier.value,
                source_urls=fc.evidence_urls,
                claim_id=fc.claim_id,
            )
    except Exception as exc:
        warnings.append(
            f"Graph store unavailable ({type(exc).__name__}: {exc}) — facts are "
            f"still in the relational audit trail, but graph exploration is "
            f"unavailable for this run. Check the Neo4j Sandbox has not expired."
        )

    return {
        "gaps": new_gaps,
        "warnings": warnings,
        "persisted_claim_ids": [fc.claim_id for fc in new_facts],
        "indexed_urls": [p.url for p in new_passages],
    }
