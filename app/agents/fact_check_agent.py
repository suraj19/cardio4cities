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

This node dominates the run's token cost, and used to waste most of it.
One call per claim meant the same handful of corroborating passages and
the same system prompt were re-sent for every claim — with twenty sources
and sixty claims, roughly twelve times more text than the run contained.
Claims sharing an origin domain are therefore adjudicated together: the
corroboration pool excludes the origin domain, so same-domain claims are
judged against an identical set either way, and grouping changes what is
sent rather than what is available. FACT_CHECK_BATCH_SIZE=1 restores the
old behaviour.

Batching introduces one hazard worth naming: verdicts must be matched to
claims by the label the model echoes, never by position, or a dropped or
reordered entry silently attaches one claim's sources to another. See
`_verdicts_by_label`, and `_adjudicate` — retained as the per-claim
fallback so a batch that cannot be parsed costs an extra call rather than
a verdict.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.graph.state import CityResearchState
from app.llm.client import llm_client
from app.llm.json_utils import parse_json_list, parse_json_object
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

# The batch variant of the same instructions. Claims that share an origin
# domain are judged against the same candidate pool anyway — see
# `_shared_context` — so sending that pool once per group rather than once per
# claim removes the bulk of this node's token cost. The added instruction is
# load-bearing: without it, a model shown several claims together will happily
# cite one claim as support for the next, which would manufacture exactly the
# circular corroboration the single-claim design exists to prevent.
FACT_CHECK_BATCH_SYSTEM = """You are an INDEPENDENT fact-checker. You did not
extract these claims and you are not told which source produced them. You are
given claims labelled [C1], [C2], ... and passages from other sources labelled
[S1], [S2], and so on.

Judge each claim SEPARATELY, and only against the labelled [S] sources. The
claims are unrelated to each other: never treat one claim, or your verdict on
it, as evidence for another.

Decide, for each claim:
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

Return ONLY a JSON list with one object per claim, each echoing the claim's
label. Include every claim you were given:
[{"claim": "C1", "tier": "...", "supporting_sources": ["S1"],
  "conflicting_text": null, "reasoning": "...", "national_vs_city_flag": false}]"""

_WORD = re.compile(r"[a-z0-9]{4,}")
_CLAIM_LABEL = re.compile(r"C\d+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _relevant_window(text: str, claim_tokens: set[str], budget: int) -> str:
    """The `budget` characters of `text` with the most overlap with the claim.

    This used to take the first `budget` characters, which is the cheapest
    possible choice and often the wrong one: on a long programme page or an
    annual report, the sentence carrying the figure that would settle a claim
    is rarely in the opening paragraph. The cost is identical — the same number
    of characters is sent either way — so head-truncation was paying full price
    for boilerplate. Choosing the window is what lets the budget stay small.
    """
    if len(text) <= budget:
        return text
    step = max(budget // 4, 1)
    best_start, best_score = 0, -1
    for start in range(0, len(text) - budget + 1, step):
        score = len(claim_tokens & _tokens(text[start : start + budget]))
        if score > best_score:
            best_start, best_score = start, score
    return text[best_start : best_start + budget]


def _shared_context(
    claims: list[Claim], passages: list[ExtractedPassage]
) -> tuple[dict[str, ExtractedPassage], str]:
    """Labelled corroboration candidates for a group of claims, and the prompt
    block describing them.

    Passages from any origin domain in the group are excluded, because a site
    agreeing with itself is not corroboration. The rest are ranked by lexical
    overlap and cut to a few — which keeps the prompt affordable and, more
    importantly, keeps the model's attention on material that could actually
    settle the question rather than on twenty loosely related pages.

    One context per group rather than per claim is the main token saving in
    this node, and it is only sound because the exclusion above is per-domain:
    claims sharing an origin domain have the same candidate pool by
    construction, so nothing is withheld from any of them.
    """
    own_domains = {c.source_domain for c in claims}
    claim_tokens = _tokens(" ".join(c.text for c in claims))
    others = [p for p in passages if p.domain not in own_domains]
    ranked = sorted(
        others,
        key=lambda p: len(claim_tokens & _tokens(p.text)),
        reverse=True,
    )[: settings.FACT_CHECK_CONTEXT_PASSAGES]

    labels = {f"S{i + 1}": p for i, p in enumerate(ranked)}
    if not ranked:
        return labels, "(no independent sources were available to compare against)"

    block = "\n---\n".join(
        f"[{label}] "
        + _relevant_window(p.text, claim_tokens, settings.FACT_CHECK_CONTEXT_CHARS)
        for label, p in labels.items()
    )
    return labels, block


def _unadjudicated(claim: Claim, reasoning: str) -> FactCheckedClaim:
    """The fail-safe verdict, used whenever the checker did not actually rule.

    Always UNSUPPORTED, which one node later becomes a Gap rather than a fact.
    A call that failed is not evidence about the claim, and must never read as
    though it were.
    """
    return FactCheckedClaim(
        claim_id=claim.claim_id,
        text=claim.text,
        tier=ConfidenceTier.UNSUPPORTED,
        source_url=claim.source_url,
        source_domain=claim.source_domain,
        dimension=claim.dimension,
        reasoning=reasoning,
    )


def _verdict_to_fact(
    claim: Claim, verdict: dict, labels: dict[str, ExtractedPassage]
) -> FactCheckedClaim:
    """Apply the trust rules to one parsed verdict.

    Shared by the batched and single-claim paths deliberately: these rules —
    fail-safe on malformed output, discard labels that were never offered,
    re-derive VERIFIED from distinct domains — are the substance of
    non-negotiable #4, and having two copies of them would be two things to
    keep in agreement.
    """
    try:
        tier = ConfidenceTier(str(verdict["tier"]).strip().upper())
        reasoning = str(verdict.get("reasoning", ""))
        national_flag = bool(verdict.get("national_vs_city_flag", False))
    except (KeyError, ValueError):
        return _unadjudicated(
            claim,
            "Fact-check response was malformed; defaulting to UNSUPPORTED (fail-safe).",
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


def _call_failed(exc: Exception) -> str:
    return (
        f"Not adjudicated: the fact-checking call failed "
        f"({type(exc).__name__}: {exc}). Dropped rather than trusted."
    )


def _adjudicate(claim: Claim, passages: list[ExtractedPassage]) -> FactCheckedClaim:
    """Adjudicate one claim in its own call.

    Retained alongside the batched path as the fallback for it: a group whose
    response cannot be parsed is retried claim by claim, so a single confusing
    claim costs one extra call instead of silently discarding the verdicts of
    everything grouped with it.
    """
    labels, context = _shared_context([claim], passages)
    try:
        raw = llm_client.complete(
            FACT_CHECK_SYSTEM,
            f"Claim: {claim.text}\n\nLabelled sources:\n{context}",
        )
    except Exception as exc:
        return _unadjudicated(claim, _call_failed(exc))

    return _verdict_to_fact(claim, parse_json_object(raw), labels)


def _verdicts_by_label(raw: str, expected: int) -> dict[str, dict]:
    """Parsed verdicts keyed by the claim label the model echoed back.

    Keyed by label rather than by list position because a model that drops,
    reorders or duplicates an entry would otherwise shift every later verdict
    onto the wrong claim — attaching one claim's corroborating sources to a
    different claim, which is precisely the kind of quiet provenance error the
    fact-checker exists to prevent. Position is accepted only when the list is
    complete and no labels were given at all, where it is unambiguous.
    """
    items = [item for item in parse_json_list(raw) if isinstance(item, dict)]
    labelled = {}
    for index, item in enumerate(items):
        label = str(item.get("claim", "")).strip().upper()
        if not _CLAIM_LABEL.fullmatch(label):
            if len(items) != expected:
                continue
            label = f"C{index + 1}"
        labelled[label] = item
    return labelled


def _adjudicate_group(
    claims: list[Claim], passages: list[ExtractedPassage]
) -> list[FactCheckedClaim]:
    """Adjudicate claims that share a corroboration pool in a single call."""
    if len(claims) == 1:
        return [_adjudicate(claims[0], passages)]

    labels, context = _shared_context(claims, passages)
    claim_block = "\n".join(f"[C{i + 1}] {c.text}" for i, c in enumerate(claims))

    try:
        raw = llm_client.complete(
            FACT_CHECK_BATCH_SYSTEM,
            f"Claims:\n{claim_block}\n\nLabelled sources:\n{context}",
        )
    except Exception as exc:
        return [_unadjudicated(c, _call_failed(exc)) for c in claims]

    verdicts = _verdicts_by_label(raw, len(claims))
    results: list[FactCheckedClaim] = []
    for index, claim in enumerate(claims):
        verdict = verdicts.get(f"C{index + 1}")
        # A claim the batch did not answer for is re-asked on its own rather
        # than failed. Batching is an optimisation, so it must not be able to
        # cost a claim its verdict.
        results.append(
            _verdict_to_fact(claim, verdict, labels)
            if verdict is not None
            else _adjudicate(claim, passages)
        )
    return results


def _batches(pending: list[Claim]) -> list[list[Claim]]:
    """Group claims into calls that can legitimately share one context.

    Grouped by origin domain because `_shared_context` excludes the origin
    domain: claims from the same domain are judged against an identical
    candidate pool by construction, so grouping them changes what is sent, not
    what is available to the checker.

    This is where most of this node's cost went. Per claim, the same few
    passages were re-sent with the same system prompt every time — with twenty
    sources and sixty claims, roughly twelve times more text than the run
    contained. Grouping also shortens the run: at a provider metered per
    second, calls are wall-clock.
    """
    groups: dict[str, list[Claim]] = {}
    for claim in pending:
        groups.setdefault(claim.source_domain, []).append(claim)

    size = max(settings.FACT_CHECK_BATCH_SIZE, 1)
    return [
        group[start : start + size]
        for group in groups.values()
        for start in range(0, len(group), size)
    ]


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
        batched = list(pool.map(lambda b: _adjudicate_group(b, passages), _batches(pending)))

    return {"fact_checked": [fact for batch in batched for fact in batch]}
