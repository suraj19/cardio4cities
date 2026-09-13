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

# Assigned, not setdefault: an inherited RUN_MODE=LIVE would take this suite
# to the real internet and hang there. See tests/test_jobs.py.
os.environ["RUN_MODE"] = "MOCK"
# Keep test runs out of the development database.
os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test_cardio4cities.db")

import pytest

from app.config import settings
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
# Token cost: fact-checking dominates it, so claims share a context
# ---------------------------------------------------------------------
def _fc_claim(n: int, domain: str = "a.example.org") -> Claim:
    return Claim(
        claim_id=f"c-{n}",
        text=f"The city runs screening programme number {n}.",
        source_url=f"https://{domain}/{n}",
        source_domain=domain,
        is_city_level=True,
        dimension="healthcare_programmes",
    )


def _recording_checker(monkeypatch, responses: list[str]) -> list[str]:
    """Patch the checker's LLM to return `responses` in order, recording prompts."""
    from app.agents import fact_check_agent

    prompts: list[str] = []

    def fake_complete(system, user, **kwargs):
        prompts.append(user)
        return responses[min(len(prompts) - 1, len(responses) - 1)]

    monkeypatch.setattr(fact_check_agent.llm_client, "complete", fake_complete)
    return prompts


def test_same_domain_claims_are_adjudicated_in_one_call(monkeypatch):
    """The whole point of the batch: three claims from one source used to cost
    three copies of the same corroboration context and system prompt."""
    from app.agents import fact_check_agent

    prompts = _recording_checker(
        monkeypatch,
        [
            '[{"claim": "C1", "tier": "SINGLE_SOURCE", "reasoning": "a"},'
            ' {"claim": "C2", "tier": "SINGLE_SOURCE", "reasoning": "b"},'
            ' {"claim": "C3", "tier": "SINGLE_SOURCE", "reasoning": "c"}]'
        ],
    )

    result = fact_check_agent.fact_check_node(
        {"claims": [_fc_claim(n) for n in (1, 2, 3)], "passages": []}
    )

    assert len(prompts) == 1, f"expected one batched call, made {len(prompts)}"
    assert len(result["fact_checked"]) == 3
    assert {f.claim_id for f in result["fact_checked"]} == {"c-1", "c-2", "c-3"}


def test_claims_from_different_domains_are_not_batched_together(monkeypatch):
    """Grouping is only sound per origin domain, because the corroboration pool
    excludes the claim's own domain. Mixing domains would either leak a
    claim's own source back to it as evidence, or withhold a valid one."""
    from app.agents import fact_check_agent

    prompts = _recording_checker(
        monkeypatch, ['[{"claim": "C1", "tier": "SINGLE_SOURCE", "reasoning": "a"}]']
    )

    fact_check_agent.fact_check_node(
        {
            "claims": [_fc_claim(1, "a.example.org"), _fc_claim(2, "b.example.org")],
            "passages": [],
        }
    )

    assert len(prompts) == 2


def test_verdicts_are_matched_by_label_not_by_position(monkeypatch):
    """A model that answers out of order must not shift every verdict onto the
    wrong claim — that would attach one claim's sources to another."""
    from app.agents import fact_check_agent

    _recording_checker(
        monkeypatch,
        [
            '[{"claim": "C2", "tier": "CONFLICTING", "reasoning": "second"},'
            ' {"claim": "C1", "tier": "SINGLE_SOURCE", "reasoning": "first"}]'
        ],
    )

    result = fact_check_agent.fact_check_node(
        {"claims": [_fc_claim(1), _fc_claim(2)], "passages": []}
    )
    tiers = {f.claim_id: f.tier for f in result["fact_checked"]}

    assert tiers["c-1"] == ConfidenceTier.SINGLE_SOURCE
    assert tiers["c-2"] == ConfidenceTier.CONFLICTING


def test_a_claim_the_batch_omitted_is_re_asked_on_its_own(monkeypatch):
    """Batching is an optimisation, so it must never cost a claim its verdict."""
    from app.agents import fact_check_agent

    prompts = _recording_checker(
        monkeypatch,
        [
            # C2 is simply missing from the batch response.
            '[{"claim": "C1", "tier": "SINGLE_SOURCE", "reasoning": "a"}]',
            '{"tier": "CONFLICTING", "reasoning": "asked alone"}',
        ],
    )

    result = fact_check_agent.fact_check_node(
        {"claims": [_fc_claim(1), _fc_claim(2)], "passages": []}
    )
    tiers = {f.claim_id: f.tier for f in result["fact_checked"]}

    assert len(prompts) == 2, "the omitted claim should have been re-asked"
    assert tiers["c-1"] == ConfidenceTier.SINGLE_SOURCE
    assert tiers["c-2"] == ConfidenceTier.CONFLICTING


def test_one_sentence_repeated_by_one_site_is_adjudicated_once(monkeypatch):
    """Portals restate a programme across several pages, and a retry pass finds
    them one at a time. Those copies share an origin domain, so they share a
    corroboration pool and must reach the same verdict — paying for each
    separately bought nothing."""
    from app.agents import fact_check_agent

    same_text = "The city runs a subsidised blood pressure screening programme."
    claims = [
        Claim(
            claim_id=f"dup-{n}",
            text=same_text if n != 3 else "A different claim entirely.",
            source_url=f"https://a.example.org/page-{n}",
            source_domain="a.example.org",
            is_city_level=True,
            dimension="healthcare_programmes",
        )
        for n in (1, 2, 3)
    ]

    prompts = _recording_checker(
        monkeypatch,
        [
            '[{"claim": "C1", "tier": "SINGLE_SOURCE", "reasoning": "a"},'
            ' {"claim": "C2", "tier": "CONFLICTING", "reasoning": "b"}]'
        ],
    )

    result = fact_check_agent.fact_check_node({"claims": claims, "passages": []})
    facts = {f.claim_id: f for f in result["fact_checked"]}

    # Two distinct questions were asked, not three.
    assert prompts and same_text in prompts[0]
    assert prompts[0].count(same_text) == 1, "the restatement was sent twice"

    # Every claim still gets its own verdict row with its own origin URL —
    # the dedupe must not cost a claim its place in the audit trail.
    assert set(facts) == {"dup-1", "dup-2", "dup-3"}
    assert facts["dup-1"].tier == facts["dup-2"].tier
    assert facts["dup-2"].source_url == "https://a.example.org/page-2"


def test_a_collapsed_restatement_keeps_its_own_national_flag(monkeypatch):
    """`is_city_level` is decided by a separate extraction call per page, so two
    pages on one domain can carry the same sentence and disagree about whether
    it describes the city or the country.

    The flag is therefore re-derived for each copy rather than inherited from
    the representative. Copying it would publish country-level data as
    city-specific, which is the one thing the field exists to prevent."""
    from app.agents import fact_check_agent

    same_text = "Hypertension prevalence among adults is 28 percent."
    claims = [
        Claim(
            claim_id="city-level",
            text=same_text,
            source_url="https://a.example.org/city-page",
            source_domain="a.example.org",
            is_city_level=True,
            dimension="cv_burden",
        ),
        Claim(
            claim_id="national-level",
            text=same_text,
            source_url="https://a.example.org/country-page",
            source_domain="a.example.org",
            is_city_level=False,
            dimension="cv_burden",
        ),
    ]

    _recording_checker(
        monkeypatch,
        ['[{"claim": "C1", "tier": "SINGLE_SOURCE", "reasoning": "a",'
         ' "national_vs_city_flag": false}]'],
    )

    result = fact_check_agent.fact_check_node({"claims": claims, "passages": []})
    facts = {f.claim_id: f for f in result["fact_checked"]}

    assert set(facts) == {"city-level", "national-level"}
    assert facts["national-level"].national_vs_city_flag is True, (
        "a national claim collapsed onto a city-level representative was "
        "published as city-specific"
    )
    # And the flag is never lowered on the representative by the copy.
    assert facts["city-level"].national_vs_city_flag is False


def test_the_same_sentence_from_two_domains_is_not_collapsed(monkeypatch):
    """The complement, and the one that matters for correctness: an identical
    sentence from a different domain is corroboration, not a duplicate.
    Collapsing those would destroy the independent-source count VERIFIED is
    derived from."""
    from app.agents import fact_check_agent

    same_text = "The city runs a subsidised blood pressure screening programme."
    claims = [
        Claim(
            claim_id=f"cross-{i}",
            text=same_text,
            source_url=f"https://{domain}/p",
            source_domain=domain,
            is_city_level=True,
            dimension="healthcare_programmes",
        )
        for i, domain in enumerate(("a.example.org", "b.example.org"))
    ]

    prompts = _recording_checker(
        monkeypatch, ['{"tier": "SINGLE_SOURCE", "reasoning": "a"}']
    )

    result = fact_check_agent.fact_check_node({"claims": claims, "passages": []})

    assert len(prompts) == 2, "each domain must be adjudicated on its own"
    assert len(result["fact_checked"]) == 2


def test_context_window_is_chosen_for_relevance_not_taken_from_the_head():
    """Head-truncation costs the same tokens and routinely cut away the one
    sentence that could settle the claim.

    Now shared by both call sites that send page text — the fact-checker
    against a claim and the extractor against its dimension brief — so it
    lives in app.util rather than in either agent.
    """
    from app.util import relevant_window, tokens

    needle = "The municipal hypertension screening programme covered 36 lakh residents."
    text = ("unrelated boilerplate navigation text. " * 60) + needle

    window = relevant_window(text, tokens(needle), 200)

    assert "hypertension screening programme" in window
    assert len(window) == 200, "the budget must be spent, not shrunk"


def test_extraction_prompt_is_windowed_to_the_dimension_not_the_page_top():
    """The extraction prompt is the run's largest single payload — one call
    per source, carrying the page. Sending the dimension-relevant window
    instead of the first N characters is what makes a smaller budget safe."""
    from app.agents.extraction_agent import _focused_window
    from app.models.schemas import ExtractedPassage

    needle = (
        "The city health department runs a hypertension screening drive at "
        "primary health centres, launched in 2023."
    )
    passage = ExtractedPassage(
        url="https://health.example.gov/programmes",
        domain="example.gov",
        title="Programmes",
        text=("cookie notice and site navigation menu. " * 200) + needle,
        dimension="healthcare_programmes",
    )

    window = _focused_window(passage, "Testopolis")

    assert len(window) <= settings.EXTRACTION_CHAR_LIMIT
    assert "hypertension screening drive" in window, (
        "the window must follow the dimension brief, not the top of the page"
    )


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


def test_a_url_that_failed_to_fetch_is_not_retried_next_pass():
    """A failed fetch produces no passage, so `passages` alone cannot tell a
    later pass that the URL was already tried. Without `attempted_urls`, every
    remaining pass re-fetches every dead link and repeats its warning."""
    from app.agents.extraction_agent import extraction_node
    from app.models.schemas import CrawlabilityResult, CrawlVerdict, SourceCandidate

    url = "https://example.org/unreadable"
    state = {
        "city": "Testopolis",
        "candidates": [
            SourceCandidate(url=url, title="t", snippet="s", dimension="cv_burden")
        ],
        "crawl_results": [
            CrawlabilityResult(url=url, verdict=CrawlVerdict.ALLOWED, reason="test")
        ],
        "passages": [],
        "attempted_urls": [url],
    }

    result = extraction_node(state)

    assert result["passages"] == []
    assert result["claims"] == []
    assert not result.get("warnings"), (
        "The URL was already attempted, so this pass should not have fetched "
        "it again — nor warned about it a second time."
    )


def test_missing_html_parser_is_named_once_not_per_source(monkeypatch):
    """A missing dependency is an environment fault, not a property of any
    source. Reported per-source it becomes one indistinguishable warning per
    candidate, which reads like a web outage and never names the cause."""
    import app.agents.extraction_agent as extraction
    from app.models.schemas import CrawlabilityResult, CrawlVerdict, SourceCandidate

    monkeypatch.setattr(extraction.settings, "RUN_MODE", "LIVE")
    monkeypatch.setattr(extraction, "_missing_parser_dependency", lambda: "beautifulsoup4")

    urls = [f"https://example.org/{n}" for n in range(5)]
    state = {
        "city": "Testopolis",
        "candidates": [
            SourceCandidate(url=u, title="t", snippet="s", dimension="cv_burden")
            for u in urls
        ],
        "crawl_results": [
            CrawlabilityResult(url=u, verdict=CrawlVerdict.ALLOWED, reason="test")
            for u in urls
        ],
        "passages": [],
    }

    result = extraction.extraction_node(state)

    assert len(result["warnings"]) == 1, (
        f"Five sources produced {len(result['warnings'])} warnings; a missing "
        "package should be stated once."
    )
    warning = result["warnings"][0]
    assert "beautifulsoup4" in warning
    assert "pip install" in warning, "The warning must say how to fix it."
    assert result["passages"] == []


# ---------------------------------------------------------------------
# Fetching: what we agree to read, and with which parser
# ---------------------------------------------------------------------
_FEED = (
    "<?xml version='1.0'?><rss version='2.0'><channel>"
    "<item><title>City launches hypertension screening drive</title>"
    "<description>The municipal health department will screen adults for "
    "raised blood pressure at every primary health centre in the city during "
    "the coming quarter, and will refer confirmed cases to district hospitals "
    "for treatment and follow-up monitoring.</description></item>"
    "</channel></rss>"
)


def _fetch_with(monkeypatch, body: str, content_type: str) -> str:
    import requests

    from app.agents.extraction_agent import _real_fetch

    class _Response:
        text = body
        headers = {"Content-Type": content_type}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Response())
    return _real_fetch("https://example.org/doc")


def test_xml_is_parsed_as_xml_not_as_html(monkeypatch):
    """bs4 warns when markup is parsed in the wrong mode, and it was right to:
    text/xml slipped through the old substring content-type check and went to
    the HTML parser."""
    import warnings

    from bs4 import XMLParsedAsHTMLWarning

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        text = _fetch_with(monkeypatch, _FEED, "text/xml; charset=utf-8")

    assert "hypertension screening" in text
    assert not [w for w in caught if issubclass(w.category, XMLParsedAsHTMLWarning)], (
        "the document was still parsed in HTML mode"
    )


def test_equivalent_xml_content_types_are_treated_alike(monkeypatch):
    """The same feed used to be read or refused depending on which of two
    interchangeable content types the server chose to send."""
    as_text_xml = _fetch_with(monkeypatch, _FEED, "text/xml")
    as_rss = _fetch_with(monkeypatch, _FEED, "application/rss+xml")

    assert as_text_xml == as_rss
    assert as_text_xml


def test_csv_is_refused_rather_than_read_as_a_page(monkeypatch):
    """"text" in content_type accepted text/csv and text/javascript, which an
    HTML parser turns into a plausible-looking blob of prose that then reaches
    the model as though it were a page."""
    rows = "indicator,year,value\n" + "\n".join(
        f"bp_prevalence,{year},28.{year % 10}" for year in range(1990, 2030)
    )

    with pytest.raises(ValueError, match="unsupported content type"):
        _fetch_with(monkeypatch, rows, "text/csv")


def test_a_page_with_no_readable_text_is_dropped_before_the_model_call(monkeypatch):
    """Extraction costs one model call per source, so an empty page would buy
    an empty answer at full price."""
    with pytest.raises(ValueError, match="readable text"):
        _fetch_with(monkeypatch, "<html><body><p>Hi</p></body></html>", "text/html")


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
    from app.config import settings as cfg
    from app.llm.client import llm_client

    def _unreachable(*args, **kwargs):
        raise ConnectionError("Network is unreachable")

    # One patch covers every agent: they all import the same client singleton.
    monkeypatch.setattr(llm_client, "complete", _unreachable)
    # (results, warning) — no warning, because this simulates search working
    # and legitimately finding nothing, not search being broken.
    monkeypatch.setattr(search_agent, "_run_query", lambda query, city: ([], None))
    # The official statistics APIs are a separate dependency from the model,
    # so they have to be turned off explicitly for this to be a *total*
    # outage rather than a partial one. That they survive an LLM-only outage
    # is the point of the next test.
    monkeypatch.setattr(cfg, "ENABLE_OFFICIAL_DATA", False)

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


# ---------------------------------------------------------------------
# Vector store: a stale collection must be detected, not upserted into
# ---------------------------------------------------------------------
class _FakeMilvus:
    """Just enough MilvusClient to exercise the schema check offline."""

    def __init__(self, described):
        self._described = described

    def describe_collection(self, collection_name):
        if isinstance(self._described, Exception):
            raise self._described
        return self._described


def _store_with_schema(described, dimension=384):
    """A _MilvusVectorStore with its schema check wired to a fake, bypassing
    __init__ so no Milvus server or pymilvus connection is needed."""
    from app.stores import vector_store as vs

    store = object.__new__(vs._MilvusVectorStore)
    store.collection_name = "city_passages"
    store.dimension = dimension
    store.client = _FakeMilvus(described)
    return store


def _schema(primary_type="VARCHAR", dim=384, vector_field="vector"):
    return {
        "fields": [
            {"name": "id", "type": primary_type, "is_primary": True},
            {"name": vector_field, "type": "FloatVector", "params": {"dim": dim}},
        ]
    }


def test_int64_primary_key_is_detected_as_unusable():
    """The exact production failure: a collection left over from an earlier
    build has an Int64 primary key, so every upsert of a URL-hash id dies with
    DataNotMatchException. has_collection() cannot see this, which is why the
    broken state used to survive every restart.
    """
    problem = _store_with_schema(_schema(primary_type="Int64"))._schema_problem()

    assert problem, "An Int64 primary key was not flagged."
    assert "INT64" in problem
    assert "VARCHAR" in problem


def test_matching_schema_is_left_alone():
    """The important negative case — dropping is destructive."""
    assert _store_with_schema(_schema())._schema_problem() is None


def test_embedding_dimension_change_is_detected():
    problem = _store_with_schema(_schema(dim=768), dimension=384)._schema_problem()
    assert problem and "768" in problem and "384" in problem


def test_missing_vector_field_is_detected():
    problem = _store_with_schema(_schema(vector_field="embedding"))._schema_problem()
    assert problem and "vector" in problem


def test_unreadable_schema_fails_open():
    """If the schema cannot be read, assume it is fine. A false positive here
    deletes working data on a guess; a false negative only lets the upsert
    error surface, which is recoverable."""
    store = _store_with_schema(RuntimeError("connection reset"))
    assert store._schema_problem() is None


# ---------------------------------------------------------------------
# Discovery failures are reported, not swallowed
# ---------------------------------------------------------------------
def _force_live_ddg(monkeypatch):
    """Take the node out of MOCK mode and off Tavily, so the DuckDuckGo branch
    is the one under test. `is_mock` is derived from RUN_MODE, so setting
    RUN_MODE is enough."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "RUN_MODE", "LIVE")
    monkeypatch.setattr(cfg, "TAVILY_API_KEY", "")


def test_search_provider_failure_is_reported_not_swallowed(monkeypatch):
    """A broken search provider must not be indistinguishable from a city
    nobody has written about.

    Regression guard for a real bug: `_run_query` returned a bare empty list
    on any exception, so a renamed dependency or a rate limit produced exactly
    the same output as a genuine absence of sources — and the cause appeared
    in no warning, no report and nowhere else either.
    """
    from app.agents import search_agent
    from app.models.schemas import PlannedQuery

    _force_live_ddg(monkeypatch)

    def _renamed_package(query):
        raise ImportError("No DuckDuckGo client is installed.")

    monkeypatch.setattr(search_agent, "_duckduckgo_search", _renamed_package)

    results, warning = search_agent._run_query(
        PlannedQuery(text="pune hypertension prevalence", dimension="cv_burden"), "Pune"
    )

    assert results == []
    assert warning, "A provider exception produced no warning at all."
    assert "DuckDuckGo search failed" in warning
    # The exception type has to survive into the warning, because that is what
    # tells an operator whether to reinstall a package or wait out a limit.
    assert "ImportError" in warning


def test_total_search_failure_is_stated_once_and_unambiguously(monkeypatch):
    """Identical provider errors collapse to one warning, plus a summary that
    distinguishes "not established" from "nothing to find"."""
    from app.agents import search_agent
    from app.models.schemas import PlannedQuery

    _force_live_ddg(monkeypatch)

    def _rate_limited(query):
        raise RuntimeError("Ratelimit")

    monkeypatch.setattr(search_agent, "_duckduckgo_search", _rate_limited)

    queries = [
        PlannedQuery(text=f"pune query {i}", dimension="cv_burden") for i in range(4)
    ]
    result = search_agent.search_node({"city": "Pune", "planned_queries": queries})

    assert result["candidates"] == []

    warnings = result["warnings"]
    # Four identical failures, so: one deduplicated provider error and one
    # total-failure summary. Not four, and not zero.
    assert len(warnings) == 2, warnings
    assert sum("Ratelimit" in w for w in warnings) == 1
    assert "not established" in warnings[-1]
    assert "Pune" in warnings[-1]


# ---------------------------------------------------------------------
# Official statistics: structured APIs alongside web discovery
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_official_statistics_survive_an_llm_outage(monkeypatch):
    """With search and the model both dead, the two API-backed dimensions
    still produce real facts.

    This is the whole reason the official-data path exists as a separate
    source type: nothing in it needs a language model, because a field is
    read out of JSON rather than extracted from prose. So an outage that
    empties the web path leaves cv_burden and health_system intact, and only
    the other three dimensions become gaps.
    """
    from app.agents import search_agent
    from app.agents.official_data_agent import DIMENSIONS_SERVED
    from app.llm.client import llm_client

    def _unreachable(*args, **kwargs):
        raise ConnectionError("Network is unreachable")

    monkeypatch.setattr(llm_client, "complete", _unreachable)
    monkeypatch.setattr(search_agent, "_run_query", lambda query, city: ([], None))

    state = await workflow.ainvoke(
        {"city": "Dimapur", "country": "India", "retry_count": 0}, config=WORKFLOW_CONFIG
    )

    official = state["fact_checked"]
    assert official, "The official-data path produced nothing without the LLM."
    assert {f.dimension for f in official} == DIMENSIONS_SERVED
    assert sorted(state["covered_dimensions"]) == sorted(DIMENSIONS_SERVED)

    # The remaining dimensions are gaps, not filler.
    assert set(state["uncovered_dimensions"]) == set(DIMENSIONS) - DIMENSIONS_SERVED
    assert {g.dimension for g in state["gaps"]} == set(DIMENSIONS) - DIMENSIONS_SERVED


@pytest.mark.asyncio
async def test_official_statistics_are_flagged_as_national_not_city():
    """Country-level data in a city brief has to read as country-level data.

    These APIs publish by country, so presenting their numbers as findings
    about the city would be the exact failure the national_vs_city_flag
    exists to prevent — and it would be invisible to a reader.
    """
    from app.agents.official_data_agent import DIMENSIONS_SERVED

    state = await workflow.ainvoke(
        {"city": "Pune", "country": "India", "retry_count": 0}, config=WORKFLOW_CONFIG
    )

    official = [f for f in state["fact_checked"] if f.dimension in DIMENSIONS_SERVED
                and f.source_url.startswith(("https://ghoapi", "https://api.worldbank"))]
    assert official, "No official-API facts in the run."

    for fact in official:
        assert fact.national_vs_city_flag is True, (
            f"{fact.claim_id} is country-level data presented without a flag."
        )
        assert fact.source_url, "An official fact reached the report with no source."
        # The reasoning must disclose that no model adjudicated this, so the
        # two provenance paths are distinguishable in the audit trail.
        assert "not sent for LLM adjudication" in fact.reasoning

    report = state["report_markdown"]
    assert "national/regional data, not confirmed city-specific" in report


@pytest.mark.asyncio
async def test_official_data_does_not_duplicate_on_a_second_pass():
    """Claim ids are deterministic, so a retry pass must recognise its own
    earlier work and emit nothing."""
    from app.agents.official_data_agent import official_data_node

    state = await workflow.ainvoke(
        {"city": "Pune", "country": "India", "retry_count": 0}, config=WORKFLOW_CONFIG
    )
    again = official_data_node(state)

    assert again.get("fact_checked", []) == []
    assert again.get("passages", []) == []


@pytest.mark.asyncio
async def test_official_data_can_be_turned_off(monkeypatch):
    """The web-only path stays demoable, and stays the thing the
    crawlability gate is judged on."""
    from app.config import settings as cfg
    from app.agents.official_data_agent import DIMENSIONS_SERVED

    monkeypatch.setattr(cfg, "ENABLE_OFFICIAL_DATA", False)
    state = await workflow.ainvoke(
        {"city": "Pune", "country": "India", "retry_count": 0}, config=WORKFLOW_CONFIG
    )

    assert not any(
        f.source_url.startswith(("https://ghoapi", "https://api.worldbank"))
        for f in state["fact_checked"]
    )
    # And the dimensions are still covered, by the web path, so the toggle
    # changes provenance rather than silently shrinking the brief.
    assert DIMENSIONS_SERVED <= set(state["covered_dimensions"])


# ---------------------------------------------------------------------
# Provider portability: the run is metered per second, not per day
# ---------------------------------------------------------------------
class _RecordingCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs

        class _Msg:
            content = "[]"

        class _Choice:
            message = _Msg()

        class _Response:
            choices = [_Choice()]

        return _Response()


def _patched_client(monkeypatch):
    """An LLMClient wired to a recording stub, in LIVE mode."""
    from app.llm.client import LLMClient

    recorder = _RecordingCompletions()

    class _Chat:
        completions = recorder

    class _Fake:
        chat = _Chat()

    client = LLMClient()
    monkeypatch.setattr(client, "_get_client", lambda: _Fake())
    monkeypatch.setattr("app.llm.client.settings.RUN_MODE", "LIVE")
    return client, recorder


def test_reasoning_effort_is_omitted_when_unset(monkeypatch):
    """An OpenAI-compatible shim that does not implement `reasoning_effort`
    rejects the request outright rather than ignoring the field, so sending it
    to a non-thinking provider fails 100% of calls. Empty must mean absent,
    not present-and-empty."""
    client, recorder = _patched_client(monkeypatch)
    monkeypatch.setattr("app.llm.client.settings.LLM_REASONING_EFFORT", "")

    client.complete("sys", "user")

    assert "reasoning_effort" not in recorder.kwargs


def test_reasoning_effort_is_sent_when_configured(monkeypatch):
    """The complement: on a thinking model the cap must actually be applied,
    or a long extraction prompt reasons until the budget is gone and returns
    an empty string."""
    client, recorder = _patched_client(monkeypatch)
    monkeypatch.setattr("app.llm.client.settings.LLM_REASONING_EFFORT", "low")

    client.complete("sys", "user")

    assert recorder.kwargs["reasoning_effort"] == "low"


def test_recursion_limit_covers_every_pass_the_retry_budget_allows():
    """A limit too low fails as GraphRecursionError several minutes into a run,
    after the research has been paid for. It is derived from the node table so
    that adding a node cannot silently shorten the last pass."""
    from app.config import settings as cfg
    from app.graph.workflow import NODES, NODES_PER_PASS, WORKFLOW_CONFIG

    assert NODES_PER_PASS == len(NODES) - 1  # report runs once, at the end
    needed = NODES_PER_PASS * (cfg.MAX_PLANNER_RETRIES + 1) + 1
    assert WORKFLOW_CONFIG["recursion_limit"] >= needed


def test_blank_numeric_settings_fall_back_instead_of_crashing(monkeypatch):
    """Commenting a line out and blanking it are the two obvious ways to say
    "use the default" in a .env file, and `int(os.getenv(...))` only honoured
    the first — the second raised ValueError at import, with a traceback
    pointing at config.py rather than at the edit."""
    from app.config import _int_env

    monkeypatch.setenv("C4C_TEST_NUMBER", "")
    assert _int_env("C4C_TEST_NUMBER", 7) == 7

    monkeypatch.setenv("C4C_TEST_NUMBER", "   ")
    assert _int_env("C4C_TEST_NUMBER", 7) == 7

    monkeypatch.setenv("C4C_TEST_NUMBER", "not-a-number")
    assert _int_env("C4C_TEST_NUMBER", 7) == 7

    monkeypatch.setenv("C4C_TEST_NUMBER", " 12 ")
    assert _int_env("C4C_TEST_NUMBER", 7) == 12


def test_graphiti_internal_concurrency_is_bounded():
    """Graphiti reads SEMAPHORE_LIMIT once at its own module scope and defaults
    to 20 concurrent extraction calls. It is the largest consumer of calls in a
    run, so on a provider metered per second that default puts the retry storm
    in the worst possible place. Importing our graph store must have bounded it
    before graphiti_core could be imported."""
    import os

    import app.stores.graph_store  # noqa: F401  (import is the thing under test)
    from app.config import settings as cfg

    assert "SEMAPHORE_LIMIT" in os.environ, (
        "Graphiti's concurrency was never bounded; it will default to 20."
    )
    # Equality rather than an inequality: the export uses setdefault, so a
    # value that disagrees means either the export did not run or something
    # set SEMAPHORE_LIMIT directly, and both are worth knowing about.
    assert os.environ["SEMAPHORE_LIMIT"] == str(cfg.GRAPHITI_SEMAPHORE_LIMIT)
