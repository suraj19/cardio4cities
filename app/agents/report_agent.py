"""
Report Generation Agent.

Composes the deliverable a City Lead actually reads, and is the last
place the no-fabrication guarantee has to hold.

Two structural decisions:

  * Facts are grouped by DIMENSION, not by confidence tier, because that
    is how someone preparing for a meeting reads — "what do we know about
    policy here" — while the tier travels with each individual fact as a
    visible marker. Grouping by tier reads well to an auditor and badly to
    the actual user; tagging per fact serves both.
  * Every fact line carries its source URLs, for every tier. A
    single-source fact is exactly the kind a reader most needs to click
    through on, so omitting provenance from anything but VERIFIED would
    invert the priority.

The narrative paragraph is the only LLM-written prose in the document,
and it is given only facts that already passed fact-checking, with an
instruction not to add anything. The report states this in-line, so a
reader knows which sentences are machine-composed and which are sourced.
"""
from app.config import settings
from app.models.schemas import (
    ConfidenceTier,
    CrawlVerdict,
    FactCheckedClaim,
    dimension_label,
)
from app.graph.state import CityResearchState
from app.llm.client import llm_client
from app.stores.relational_store import relational_store

NARRATIVE_SYSTEM = """Write a 4-6 sentence briefing summary of a city's
cardiovascular health landscape for a public-health lead about to meet
government stakeholders.

Use ONLY the facts provided. Do not add statistics, names, organizations or
dates that are not in the input. Do not speculate about causes or outcomes.
If gaps are listed, state plainly which aspects remain unconfirmed. Write
plain prose, no headings and no bullet points."""

TIER_MARKERS = {
    ConfidenceTier.VERIFIED: "VERIFIED",
    ConfidenceTier.SINGLE_SOURCE: "SINGLE SOURCE",
    ConfidenceTier.CONFLICTING: "CONFLICTING",
}

REPORTABLE_TIERS = tuple(TIER_MARKERS)


def _fact_lines(fact: FactCheckedClaim) -> list[str]:
    marker = TIER_MARKERS[fact.tier]
    flag = " ⚠️ national/regional data, not confirmed city-specific" if fact.national_vs_city_flag else ""
    lines = [f"- [{marker}] {fact.text}{flag}"]

    sources = fact.evidence_urls
    lines.append(f"  - **Sources:** {', '.join(sources) if sources else 'origin source unavailable'}")

    if fact.tier is ConfidenceTier.CONFLICTING and fact.conflicting_text:
        lines.append(f"  - **Conflict:** {fact.conflicting_text}")
    if fact.tier is not ConfidenceTier.VERIFIED and fact.reasoning:
        lines.append(f"  - **Reasoning:** {fact.reasoning}")
    return lines


def _narrative(city: str, facts: list[FactCheckedClaim], gaps) -> str:
    if not facts:
        return (
            f"No claim about {city} survived independent fact-checking in this run. "
            f"Rather than summarise unverified material, this brief reports only what "
            f"could not be established — see Gaps & Uncertainties below."
        )

    fact_block = "\n".join(
        f"- ({dimension_label(f.dimension)}) {f.text}" for f in facts
    )
    gap_block = "\n".join(f"- {g.description}" for g in gaps) or "(none)"
    try:
        return llm_client.complete(
            NARRATIVE_SYSTEM,
            f"City: {city}\n\nVerified and single-source facts:\n{fact_block}\n\nGaps:\n{gap_block}",
        ).strip()
    except Exception as exc:
        # The narrative is the last LLM call in the run and the only one whose
        # output is prose rather than data. Losing it must not cost the user the
        # brief they just waited for — the facts below are the substance.
        dimensions_found = sorted({dimension_label(f.dimension) for f in facts})
        return (
            f"Summary unavailable: the narrative generation call failed "
            f"({type(exc).__name__}). The brief itself is unaffected — it contains "
            f"{len(facts)} fact-checked statement(s) across "
            f"{', '.join(dimensions_found)}, each with its sources, and "
            f"{len(gaps)} logged gap(s) below."
        )


def report_node(state: CityResearchState) -> dict:
    city = state["city"]
    country = state.get("country")
    dimensions = state.get("dimensions", [])
    fact_checked = state.get("fact_checked", [])
    gaps = state.get("gaps", [])
    crawl_results = state.get("crawl_results", [])
    passages = state.get("passages", [])
    warnings = state.get("warnings", [])

    reportable = [f for f in fact_checked if f.tier in REPORTABLE_TIERS]
    by_tier = {tier: [f for f in reportable if f.tier is tier] for tier in REPORTABLE_TIERS}
    summarisable = by_tier[ConfidenceTier.VERIFIED] + by_tier[ConfidenceTier.SINGLE_SOURCE]

    allowed = sum(1 for r in crawl_results if r.verdict == CrawlVerdict.ALLOWED)
    denied = [r for r in crawl_results if r.verdict == CrawlVerdict.DENIED]

    location = f"{city}, {country}" if country else city
    lines = [
        f"# CARDIO4Cities — {location}: City Intelligence Brief",
        "",
        f"_Generated by the research pipeline in {settings.RUN_MODE} mode across "
        f"{len(dimensions)} research dimensions._",
        "",
        "## Summary",
        _narrative(city, summarisable, gaps),
        "",
        "_The paragraph above is composed by a language model from the fact-checked "
        "statements below and introduces no new information. Every factual statement "
        "in this brief is listed individually with its sources._",
        "",
        "## Confidence Summary",
        "",
        f"- **Verified** — corroborated by {settings.MIN_SOURCES_FOR_VERIFIED}+ independent "
        f"sources: {len(by_tier[ConfidenceTier.VERIFIED])}",
        f"- **Single source** — one source only, review before relying on it: "
        f"{len(by_tier[ConfidenceTier.SINGLE_SOURCE])}",
        f"- **Conflicting** — sources disagree: {len(by_tier[ConfidenceTier.CONFLICTING])}",
        f"- **Gaps** — explicitly unresolved: {len(gaps)}",
        "",
    ]

    # --- Findings, by dimension -------------------------------------------
    for dimension in dimensions:
        facts = [f for f in reportable if f.dimension == dimension]
        lines.append(f"## {dimension_label(dimension)} ({len(facts)})")
        lines.append("")
        if not facts:
            lines.append(
                "- [GAP] Nothing verifiable was found for this dimension in this run. "
                "See Gaps & Uncertainties."
            )
        else:
            # Strongest evidence first within a dimension.
            order = {ConfidenceTier.VERIFIED: 0, ConfidenceTier.SINGLE_SOURCE: 1, ConfidenceTier.CONFLICTING: 2}
            for fact in sorted(facts, key=lambda f: order[f.tier]):
                lines.extend(_fact_lines(fact))
        lines.append("")

    # --- Gaps --------------------------------------------------------------
    lines += [f"## Gaps & Uncertainties ({len(gaps)})", "",
              "_Explicitly unresolved. Listed rather than guessed at._", ""]
    for gap in gaps:
        lines.append(f"- [GAP] ({dimension_label(gap.dimension)}) {gap.description}")
        lines.append(f"  - **Why:** {gap.reason}")
    if not gaps:
        lines.append("- No gaps logged for this run.")
    lines.append("")

    # --- Source coverage ---------------------------------------------------
    lines += [
        "## Source Coverage",
        "",
        f"- Candidate sources considered: {len(crawl_results)}",
        f"- Cleared the crawlability gate: {allowed}",
        f"- Refused by the crawlability gate: {len(denied)}",
        f"- Successfully read and indexed: {len(passages)}",
        "",
    ]
    if denied:
        lines.append("_Sources deliberately not read:_")
        lines.append("")
        for result in denied[:15]:
            lines.append(f"- {result.url}")
            lines.append(f"  - **Why:** {result.reason}")
        lines.append("")

    if warnings:
        lines += ["## Run Warnings", "",
                  "_Degraded behaviour during this run, recorded rather than hidden._", ""]
        # Deduplicated, order preserved. `warnings` is an append channel and the
        # retry loop re-runs every node, so a condition that persists across
        # passes — a dead dependency, an exhausted quota — is recorded once per
        # pass. Printed verbatim that became the same paragraph three times,
        # which reads like three separate incidents and buries the distinct
        # warnings between the copies.
        for warning in dict.fromkeys(warnings):
            lines.append(f"- {warning}")
        lines.append("")

    markdown = "\n".join(lines)

    # Gaps are written here rather than in the persistence node because the
    # coverage evaluator adds dimension-level gaps after that node has run —
    # this is the first point at which the gap list is final.
    relational_store.record_gaps(city, gaps)
    relational_store.save_report(city, dimensions, markdown)

    return {"report_markdown": markdown}
