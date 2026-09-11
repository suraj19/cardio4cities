"""
Coverage Evaluator.

Answers "how do you know when research is sufficient, and what happens
when it isn't?" — and answers it per dimension rather than in aggregate.

A total fact count is the wrong measure: twenty facts about screening
programmes and nothing at all about policy is not an understood city, but
a threshold on volume would happily call it one. So coverage is the number
of DIMENSIONS with at least one usable fact, and a dimension that yielded
nothing is named in the report as a gap rather than quietly averaged away.

When coverage falls short the graph loops back to the planner, which
re-plans against the uncovered dimensions only. The loop is bounded by
MAX_PLANNER_RETRIES so an under-documented city terminates in an honest
"here is what we found and here is what is missing" report instead of
spinning — the failure mode this system is most careful to avoid is
producing content to fill a section that has no evidence behind it.
"""
from collections import Counter

from app.config import settings
from app.graph.state import CityResearchState
from app.models.schemas import (
    DIMENSIONS,
    ConfidenceTier,
    CrawlVerdict,
    Gap,
    dimension_label,
)

USABLE_TIERS = (ConfidenceTier.VERIFIED, ConfidenceTier.SINGLE_SOURCE)


def coverage_evaluator_node(state: CityResearchState) -> dict:
    dimensions = state.get("dimensions") or DIMENSIONS
    fact_checked = state.get("fact_checked", [])
    crawl_results = state.get("crawl_results", [])
    retry_count = state.get("retry_count", 0)

    usable_per_dimension = Counter(
        fc.dimension for fc in fact_checked if fc.tier in USABLE_TIERS
    )
    covered = [d for d in dimensions if usable_per_dimension.get(d, 0) > 0]
    uncovered = [d for d in dimensions if usable_per_dimension.get(d, 0) == 0]

    target = min(settings.MIN_DIMENSIONS_COVERED, len(dimensions))
    sufficient = len(covered) >= target
    retries_exhausted = retry_count >= settings.MAX_PLANNER_RETRIES

    if not (sufficient or retries_exhausted):
        return {
            "coverage_sufficient": False,
            "retry_count": retry_count + 1,
            "covered_dimensions": covered,
            "uncovered_dimensions": uncovered,
        }

    # Terminal pass. Every dimension still empty becomes an explicit gap, so
    # the report can state what was not established rather than omitting the
    # section and letting absence read as absence of anything to find.
    attempts = retry_count + 1
    allowed = sum(1 for r in crawl_results if r.verdict == CrawlVerdict.ALLOWED)
    denied = sum(1 for r in crawl_results if r.verdict == CrawlVerdict.DENIED)

    if allowed == 0 and denied > 0:
        why = (
            f"Every one of the {denied} candidate source(s) was blocked by the "
            f"crawlability gate, so no content could legitimately be read."
        )
    elif not crawl_results:
        why = "Search returned no candidate sources for this dimension."
    else:
        why = (
            f"No claim for this dimension survived independent fact-checking "
            f"after {attempts} research pass(es)."
        )

    gaps = [
        Gap(
            description=f"No verifiable information found on {dimension_label(d).lower()}.",
            dimension=d,
            reason=f"{why} Reported as unknown rather than filled in.",
        )
        for d in uncovered
    ]

    return {
        "coverage_sufficient": True,
        "covered_dimensions": covered,
        "uncovered_dimensions": uncovered,
        "gaps": gaps,
    }
