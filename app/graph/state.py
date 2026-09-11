"""
The LangGraph state object. Every node reads a slice of this and returns
a partial update (LangGraph merges dict updates into state automatically).

Two kinds of field live here, and the distinction matters because of the
planner retry loop:

  * Annotated[..., append_list] fields ACCUMULATE across retries. The
    evidence base only ever grows, so a second pass adds to what the first
    pass found instead of discarding it.
  * Plain fields are OVERWRITTEN by the most recent node to set them. Use
    these for anything describing the current pass (which dimensions we are
    re-attempting, how many retries we have spent).
"""
from __future__ import annotations
from typing import TypedDict, Annotated

from app.models.schemas import (
    PlannedQuery,
    SourceCandidate,
    CrawlabilityResult,
    ExtractedPassage,
    Claim,
    FactCheckedClaim,
    Gap,
)


def append_list(existing: list, new: list) -> list:
    """Reducer: nodes append to shared lists instead of overwriting them."""
    return existing + new


class CityResearchState(TypedDict, total=False):
    # --- inputs ---
    city: str
    country: str | None

    # --- planner output ---
    dimensions: list[str]        # every dimension in scope for this run
    focus_dimensions: list[str]  # the subset this pass should work on
    planned_queries: list[PlannedQuery]

    # --- pipeline artifacts (accumulate across retry loops) ---
    candidates: Annotated[list[SourceCandidate], append_list]
    crawl_results: Annotated[list[CrawlabilityResult], append_list]
    passages: Annotated[list[ExtractedPassage], append_list]
    claims: Annotated[list[Claim], append_list]
    fact_checked: Annotated[list[FactCheckedClaim], append_list]
    gaps: Annotated[list[Gap], append_list]

    # --- control flow ---
    retry_count: int
    coverage_sufficient: bool
    covered_dimensions: list[str]
    uncovered_dimensions: list[str]

    # --- write idempotency ---
    # The persistence node runs once per pass over state that accumulates, so
    # without these it would re-write everything an earlier pass already
    # stored: duplicate audit rows, duplicate graph episodes, re-embedded
    # passages.
    persisted_claim_ids: Annotated[list[str], append_list]
    indexed_urls: Annotated[list[str], append_list]

    # --- final output ---
    report_markdown: str
    warnings: Annotated[list[str], append_list]
