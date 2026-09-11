"""
Runs the FULL LangGraph pipeline in MOCK mode and asserts on the behaviour
the case study's non-negotiables depend on:

  - the crawlability gate actually excludes denied sources from extraction
  - every reportable fact carries provenance, at every confidence tier
  - VERIFIED is derived from cited evidence, not taken on the model's word
  - the fact-checker's UNSUPPORTED verdict has real consequences
  - gaps are surfaced, not hidden
  - the graph can be read back at query time
  - retrying does not duplicate facts
  - a total loss of internet still yields an honest all-gaps brief

Requires RUN_MODE=MOCK and needs NO internet access, NO API keys and NO
running Neo4j instance.
"""
import os

os.environ.setdefault("RUN_MODE", "MOCK")
# Keep test runs out of the development database.
os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test_cardio4cities.db")

import pytest

from app.graph.workflow import WORKFLOW_CONFIG, workflow
from app.models.schemas import (
    DIMENSIONS,
    Claim,
    ConfidenceTier,
    CrawlVerdict,
    ExtractedPassage,
    FactCheckedClaim,
)


# ---------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_covers_every_planned_dimension():
    state = await workflow.ainvoke({"city": "Testopolis", "retry_count": 0}, config=WORKFLOW_CONFIG)

    assert state["dimensions"] == DIMENSIONS
    assert state["coverage_sufficient"] is True
    # The mock returns usable facts for every dimension, so nothing should be
    # left uncovered and the planner should not have needed a retry.
    assert state["uncovered_dimensions"] == []
    assert state["retry_count"] == 0


# ---------------------------------------------------------------------
# Non-negotiable #3 — crawlability gate
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_crawlability_gate_blocks_extraction():
    state = await workflow.ainvoke({"city": "Testopolis", "retry_count": 0}, config=WORKFLOW_CONFIG)
    crawl_results = state["crawl_results"]

    verdicts = {r.verdict for r in crawl_results}
    assert CrawlVerdict.ALLOWED in verdicts
    assert CrawlVerdict.DENIED in verdicts, (
        "Expected at least one DENIED verdict (mock includes a ToS-restricted "
        "and a paywalled-style URL) — the gate should not allow everything."
    )

    denied_urls = {r.url for r in crawl_results if r.verdict == CrawlVerdict.DENIED}
    extracted_urls = {p.url for p in state["passages"]}
    assert denied_urls.isdisjoint(extracted_urls), (
        "A DENIED url produced an extracted passage — the crawlability gate "
        "is not actually gating extraction."
    )


# ---------------------------------------------------------------------
# Non-negotiable #7 — evidence on every fact
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_reportable_fact_has_provenance():
    state = await workflow.ainvoke({"city": "Testopolis", "retry_count": 0}, config=WORKFLOW_CONFIG)

    reportable = [
        f for f in state["fact_checked"] if f.tier != ConfidenceTier.UNSUPPORTED
    ]
    assert reportable, "No facts survived fact-checking in the mock scenario."

    for fact in reportable:
        assert fact.source_url, f"{fact.claim_id} reached the report with no source URL."
        assert fact.evidence_urls, f"{fact.claim_id} has no evidence URLs to render."

    # And the rendered report must actually show them, at every tier.
    report = state["report_markdown"]
    for fact in reportable:
        assert fact.source_url in report, (
            f"{fact.claim_id} is a {fact.tier.value} fact whose source URL never "
            f"made it into the report body."
        )


@pytest.mark.asyncio
async def test_report_structure():
    state = await workflow.ainvoke({"city": "Testopolis", "retry_count": 0}, config=WORKFLOW_CONFIG)
    report = state["report_markdown"]

    assert "# CARDIO4Cities — Testopolis" in report
    assert "## Confidence Summary" in report
    assert "## Gaps & Uncertainties" in report
    assert "## Source Coverage" in report
    # One section per dimension, so an empty dimension is visibly empty
    # rather than silently absent.
    from app.models.schemas import dimension_label

    for dimension in DIMENSIONS:
        assert f"## {dimension_label(dimension)}" in report


# ---------------------------------------------------------------------
# Non-negotiable #4 — the fact-checker's verdict has consequences
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unsupported_claims_never_become_facts():
    """An UNSUPPORTED tier must never be persisted as a fact; it becomes a Gap."""
    from app.agents.graph_writer_agent import graph_writer_node

    fake_state = {
        "city": "GapCity",
        "dimensions": ["healthcare_programmes"],
        "fact_checked": [
            FactCheckedClaim(
                claim_id="x-1",
                text="This claim has no corroboration anywhere.",
                tier=ConfidenceTier.UNSUPPORTED,
                source_url="https://example.org/a",
                source_domain="example.org",
                dimension="healthcare_programmes",
                reasoning="No other source mentions this at all.",
            )
        ],
        "passages": [],
        "crawl_results": [],
        "candidates": [],
    }

    result = await graph_writer_node(fake_state)

    assert len(result["gaps"]) == 1, "UNSUPPORTED claim should become exactly one Gap."
    assert "could not corroborate" in result["gaps"][0].reason.lower()


# ---------------------------------------------------------------------
# Trust: VERIFIED is derived from evidence, not asserted by the model
# ---------------------------------------------------------------------
def _adjudicate_with(monkeypatch, response: str) -> FactCheckedClaim:
    from app.agents import fact_check_agent

    monkeypatch.setattr(
        fact_check_agent.llm_client, "complete", lambda system, user, **kw: response
    )
    claim = Claim(
        claim_id="c-1",
        text="The city runs a hypertension screening programme.",
        source_url="https://health.example.gov/p",
        source_domain="health.example.gov",
        is_city_level=True,
        dimension="healthcare_programmes",
    )
    passages = [
        ExtractedPassage(
            url="https://ngo.example.org/p",
            domain="ngo.example.org",
            title="NGO",
            text="The city runs a hypertension screening programme with our support.",
            dimension="healthcare_programmes",
        )
    ]
    return fact_check_agent._adjudicate(claim, passages)


def test_verified_requires_a_cited_corroborating_source(monkeypatch):
    """The model asking for VERIFIED without naming support gets downgraded."""
    fact = _adjudicate_with(
        monkeypatch,
        '{"tier": "VERIFIED", "supporting_sources": [], "reasoning": "looks right"}',
    )
    assert fact.tier == ConfidenceTier.SINGLE_SOURCE
    assert fact.corroborating_urls == []
    assert "downgraded" in fact.reasoning.lower()


def test_verified_survives_when_support_is_actually_cited(monkeypatch):
    fact = _adjudicate_with(
        monkeypatch,
        '{"tier": "VERIFIED", "supporting_sources": ["S1"], "reasoning": "corroborated"}',
    )
    assert fact.tier == ConfidenceTier.VERIFIED
    assert fact.corroborating_urls == ["https://ngo.example.org/p"]


def test_hallucinated_source_label_is_discarded(monkeypatch):
    """A label that was never offered cannot become provenance."""
    fact = _adjudicate_with(
        monkeypatch,
        '{"tier": "VERIFIED", "supporting_sources": ["S7"], "reasoning": "corroborated"}',
    )
    assert fact.corroborating_urls == []
    assert fact.tier == ConfidenceTier.SINGLE_SOURCE


def test_malformed_verdict_fails_safe(monkeypatch):
    fact = _adjudicate_with(monkeypatch, "I could not determine this.")
    assert fact.tier == ConfidenceTier.UNSUPPORTED
    assert fact.source_url  # still traceable, even when unusable


# ---------------------------------------------------------------------
# Non-negotiable #5 — the graph is readable at query time
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_graph_is_queryable_after_a_run():
    from app.stores.graph_store import graph_store

    await workflow.ainvoke({"city": "Testopolis", "retry_count": 0}, config=WORKFLOW_CONFIG)
    facts = await graph_store.query_facts_for_city("Testopolis", question="screening programme")

    assert facts, "Nothing could be read back out of the graph after a run."
    assert all("fact" in f for f in facts)


# ---------------------------------------------------------------------
# Idempotency: the retry loop must not duplicate work
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_claim_ids_are_unique_within_a_run():
    state = await workflow.ainvoke({"city": "Testopolis", "retry_count": 0}, config=WORKFLOW_CONFIG)
    claim_ids = [c.claim_id for c in state["claims"]]
    assert len(claim_ids) == len(set(claim_ids)), (
        "Duplicate claim ids mean a source was extracted twice — the audit "
        "trail and the graph would both double-count it."
    )


@pytest.mark.asyncio
async def test_nodes_skip_already_processed_items_on_a_second_pass():
    """Re-entering extraction with state that already contains the passages
    must produce nothing new, which is what makes the retry loop safe."""
    from app.agents.extraction_agent import extraction_node

    state = await workflow.ainvoke({"city": "Testopolis", "retry_count": 0}, config=WORKFLOW_CONFIG)
    again = extraction_node(state)

    assert again["passages"] == []
    assert again["claims"] == []


# ---------------------------------------------------------------------
# Offline resilience: no internet at all
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_total_outage_still_produces_an_honest_report(monkeypatch):
    """A full blackout — no search results AND no reachable LLM — must finish
    with an all-gaps brief rather than a 500.

    This is the realistic outage: the LLM is hosted, so whatever kills search
    usually kills the model too. The run has to survive its very first LLM
    call to get far enough to report anything at all.
    """
    from app.agents import search_agent
    from app.llm.client import llm_client

    def _unreachable(*args, **kwargs):
        raise ConnectionError("Network is unreachable")

    # One patch covers every agent: they all import the same client singleton.
    monkeypatch.setattr(llm_client, "complete", _unreachable)
    monkeypatch.setattr(search_agent, "_run_query", lambda query, city: [])

    state = await workflow.ainvoke(
        {"city": "Darkville", "retry_count": 0}, config=WORKFLOW_CONFIG
    )

    # Nothing was found, and nothing was invented to cover for it.
    assert state["candidates"] == []
    assert state["fact_checked"] == []
    assert state["covered_dimensions"] == []
    assert state["uncovered_dimensions"] == DIMENSIONS

    # It still terminates, and only after spending its full retry budget.
    from app.config import settings

    assert state["retry_count"] == settings.MAX_PLANNER_RETRIES
    assert state["coverage_sufficient"] is True  # "done", not "satisfied"

    # The brief exists, names every dimension as a gap, and attributes the
    # cause to search rather than reporting a bare absence.
    report = state["report_markdown"]
    assert "## Gaps & Uncertainties" in report
    assert "[GAP]" in report
    assert "Search returned no candidate sources" in report
    assert {g.dimension for g in state["gaps"]} == set(DIMENSIONS)

    # The degradation is disclosed, not hidden.
    assert any("fell back to keyword templates" in w for w in state["warnings"])
    assert "## Run Warnings" in report
