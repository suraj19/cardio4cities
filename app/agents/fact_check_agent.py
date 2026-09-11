"""
Fact-Checking Agent.

Non-negotiable #4: independent of the agent that produced the claim, able
to conclude that something is unsupported — with consequences. The
consequence is enforced one node later, in graph_writer_agent: an
UNSUPPORTED verdict is never persisted as a fact, it becomes a Gap.

"Independent" here is structural, not just a different prompt. The
fact-checker is given the claim text and a set of passages from OTHER
domains, and is never told which passage the claim came from or what the
extractor concluded. It therefore cannot rubber-stamp; it has to find
support in material the extractor did not use.

Two rules keep the output honest:

  * The model must NAME the sources it believes support the claim, by
    label. Those labels are mapped back to real URLs, so
    `corroborating_urls` contains only sources the checker actually cited.
    Previously it was filled with every other-domain URL in the run, which
    made the report's "Sources:" line technically present and factually
    meaningless.
  * VERIFIED is then re-derived from that evidence rather than taken on
    the model's word. A claim cannot reach VERIFIED without the required
    number of distinct supporting domains, no matter what tier the model
    asked for.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.graph.state import CityResearchState
from app.llm.client import llm_client
from app.llm.json_utils import parse_json_object
from app.models.schemas import Claim, ConfidenceTier, ExtractedPassage, FactCheckedClaim

FACT_CHECK_SYSTEM = """You are an INDEPENDENT fact-checker. You did not extract
this claim and you are not told which source produced it. You are given the
claim, and passages from other sources labelled [S1], [S2], and so on.

Decide:
- VERIFIED       : at least one labelled source clearly supports the claim.
- SINGLE_SOURCE  : no labelled source supports it, but none contradicts it.
- CONFLICTING    : a labelled source contradicts it or disagrees on specifics.
- UNSUPPORTED    : the claim is vague, unverifiable, or contradicted outright
                   with nothing in its favour.

Only list a source under supporting_sources if it genuinely states something
that supports the claim. Topical similarity is not support. Listing a source
that does not support the claim is the worst error you can make here.

Set national_vs_city_flag=true if the claim presents national or regional
information as if it were specific to the city.

Return ONLY JSON:
{"tier": "...", "supporting_sources": ["S1"], "conflicting_text": null,
 "reasoning": "...", "national_vs_city_flag": false}"""

_WORD = re.compile(r"[a-z0-9]{4,}")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _context_passages(claim: Claim, passages: list[ExtractedPassage]) -> list[ExtractedPassage]:
    """The most claim-relevant passages from domains other than the claim's own.

    Same-domain passages are excluded because a site agreeing with itself is
    not corroboration. The rest are ranked by lexical overlap and truncated to
    a few, which keeps the prompt affordable and — more importantly — keeps
    the model's attention on material that could actually settle the question
    rather than on twenty loosely related pages.
    """
    claim_tokens = _tokens(claim.text)
    others = [p for p in passages if p.domain != claim.source_domain]
    ranked = sorted(
        others,
        key=lambda p: len(claim_tokens & _tokens(p.text)),
        reverse=True,
    )
    return ranked[: settings.FACT_CHECK_CONTEXT_PASSAGES]


def _adjudicate(claim: Claim, passages: list[ExtractedPassage]) -> FactCheckedClaim:
    context_passages = _context_passages(claim, passages)
    labels = {f"S{i + 1}": p for i, p in enumerate(context_passages)}

    if context_passages:
        context = "\n---\n".join(
            f"[S{i + 1}] {p.text[: settings.FACT_CHECK_CONTEXT_CHARS]}"
            for i, p in enumerate(context_passages)
        )
    else:
        context = "(no independent sources were available to compare against)"

    try:
        raw = llm_client.complete(
            FACT_CHECK_SYSTEM,
            f"Claim: {claim.text}\n\nLabelled sources:\n{context}",
        )
    except Exception as exc:
        # The call itself failed after retries — a dead key, an exhausted quota.
        # This is NOT evidence about the claim, so it must not read as one:
        # the claim is dropped as unverified and the cause is recorded.
        return FactCheckedClaim(
            claim_id=claim.claim_id,
            text=claim.text,
            tier=ConfidenceTier.UNSUPPORTED,
            source_url=claim.source_url,
            source_domain=claim.source_domain,
            dimension=claim.dimension,
            reasoning=f"Not adjudicated: the fact-checking call failed "
            f"({type(exc).__name__}: {exc}). Dropped rather than trusted.",
        )

    verdict = parse_json_object(raw)

    try:
        tier = ConfidenceTier(str(verdict["tier"]).strip().upper())
        reasoning = str(verdict.get("reasoning", ""))
        national_flag = bool(verdict.get("national_vs_city_flag", False))
    except (KeyError, ValueError):
        # Fail safe. A malformed verdict must never default to a trusted tier,
        # so it lands on UNSUPPORTED and becomes a gap rather than a fact.
        return FactCheckedClaim(
            claim_id=claim.claim_id,
            text=claim.text,
            tier=ConfidenceTier.UNSUPPORTED,
            source_url=claim.source_url,
            source_domain=claim.source_domain,
            dimension=claim.dimension,
            reasoning="Fact-check response was malformed; defaulting to UNSUPPORTED (fail-safe).",
        )

    named = verdict.get("supporting_sources") or []
    supporting = [labels[str(label).strip().upper()] for label in named if str(label).strip().upper() in labels]

    # Distinct domains, excluding the claim's own — this is the actual count
    # of independent corroboration, and the tier is derived from it.
    supporting_domains = {p.domain for p in supporting if p.domain != claim.source_domain}
    independent_sources = 1 + len(supporting_domains)  # origin + corroborators

    if tier == ConfidenceTier.VERIFIED and independent_sources < settings.MIN_SOURCES_FOR_VERIFIED:
        tier = ConfidenceTier.SINGLE_SOURCE
        reasoning = (
            f"{reasoning} [Downgraded automatically: the checker asked for VERIFIED but "
            f"cited {len(supporting_domains)} independent corroborating domain(s), and "
            f"{settings.MIN_SOURCES_FOR_VERIFIED} independent sources are required.]"
        ).strip()

    if not claim.is_city_level:
        national_flag = True

    conflicting_text = verdict.get("conflicting_text")

    return FactCheckedClaim(
        claim_id=claim.claim_id,
        text=claim.text,
        tier=tier,
        source_url=claim.source_url,
        source_domain=claim.source_domain,
        dimension=claim.dimension,
        corroborating_urls=[p.url for p in supporting],
        conflicting_text=str(conflicting_text) if conflicting_text else None,
        reasoning=reasoning,
        national_vs_city_flag=national_flag,
    )


def fact_check_node(state: CityResearchState) -> dict:
    claims: list[Claim] = state.get("claims", [])
    passages: list[ExtractedPassage] = state.get("passages", [])

    # Claims and verdicts both accumulate across retries, so only adjudicate
    # what has not been adjudicated yet.
    already_checked = {fc.claim_id for fc in state.get("fact_checked", [])}
    pending = [c for c in claims if c.claim_id not in already_checked]
    if not pending:
        return {"fact_checked": []}

    with ThreadPoolExecutor(max_workers=settings.LLM_MAX_CONCURRENCY) as pool:
        fact_checked = list(pool.map(lambda c: _adjudicate(c, passages), pending))

    return {"fact_checked": fact_checked}
