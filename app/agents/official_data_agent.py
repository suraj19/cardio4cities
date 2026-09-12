"""
Official Data Agent — structured indicators from public-health REST APIs.

This runs *alongside* web discovery rather than instead of it, and the split
is by dimension because the two source types answer different questions.
Web search is the only way to learn that Pune's municipal corporation runs a
hypertension screening drive at its primary health centres — no API publishes
that. But it is a poor way to learn India's hospital-bed density, which the
World Bank maintains as a time series. So this agent serves `cv_burden` and
`health_system`, and the web path keeps `healthcare_programmes`,
`policy_initiatives` and `stakeholders`.

Three properties make these claims different from extracted ones, and the
design leans on all three:

  * **They are country-level, never city-level.** Every claim is emitted with
    `is_city_level=False`, which the brief renders with an explicit
    "national/regional data, not confirmed city-specific" warning. This is
    national context for a city brief, not evidence about the city, and it
    has to read that way or it is worse than having nothing.
  * **They are read, not extracted.** No LLM sees the payload; a field is
    copied out of a JSON document. The hallucination risk that the
    fact-checking agent exists to catch simply is not present, so these
    bypass LLM adjudication and are tiered by rule. The reasoning string
    says so explicitly, so a reader can tell the two provenance paths apart
    rather than having to trust that they are equivalent.
  * **They need no API key and no LLM.** That makes these two dimensions the
    only ones that still produce facts when the model is unreachable.

The passages are indexed like any other, which means the fact-checking agent
sees them as candidate corroboration for web claims. An official statistic is
a strong independent source, so this should raise VERIFIED rates on the web
path as a side effect.

Every indicator code below was verified to return data for India before being
hard-coded. Two that looked obvious do not work and are deliberately absent:
`SH.UHC.SRVS.CV.XD` (the World Bank UHC service coverage index) has been
archived and returns "indicator not found", and `WHS2_161` (age-standardised
cardiovascular mortality) has zero rows for India despite being the most
on-topic name in the GHO catalogue.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from app.config import settings
from app.graph.state import CityResearchState
from app.models.schemas import (
    ConfidenceTier,
    CrawlabilityResult,
    CrawlVerdict,
    ExtractedPassage,
    FactCheckedClaim,
    SourceCandidate,
)
from app.util import domain_of

GHO_BASE = "https://ghoapi.azureedge.net/api"
WORLDBANK_BASE = "https://api.worldbank.org/v2"

# These are pure network waits, so they overlap. Kept modest and separate
# from LLM_MAX_CONCURRENCY, which exists to respect a provider rate limit —
# a different constraint that happens to be expressed in the same units.
_MAX_PARALLEL_FETCHES = 4

# (indicator code, dimension, what it measures, unit)
_GHO_INDICATORS: list[tuple[str, str, str, str]] = [
    (
        "BP_04",
        "cv_burden",
        "prevalence of raised blood pressure (systolic >=140 or diastolic >=90) "
        "among adults aged 30-79, age-standardised",
        "% of adults",
    ),
    (
        "NCDMORT3070",
        "cv_burden",
        "probability of dying between ages 30 and 70 from cardiovascular disease, "
        "cancer, diabetes or chronic respiratory disease",
        "%",
    ),
    (
        "NCD_BMI_30A",
        "cv_burden",
        "prevalence of obesity (BMI >=30) among adults, age-standardised",
        "% of adults",
    ),
]

_WORLDBANK_INDICATORS: list[tuple[str, str, str, str]] = [
    ("SH.MED.BEDS.ZS", "health_system", "hospital beds", "per 1,000 people"),
    ("SH.MED.PHYS.ZS", "health_system", "physicians", "per 1,000 people"),
    ("SH.MED.NUMW.P3", "health_system", "nurses and midwives", "per 1,000 people"),
    (
        "SH.XPD.CHEX.GD.ZS",
        "health_system",
        "current health expenditure",
        "% of GDP",
    ),
]

DIMENSIONS_SERVED = {"cv_burden", "health_system"}

_TIER_REASONING = (
    "Read directly from an official API response rather than extracted from prose "
    "by a language model, so it was not sent for LLM adjudication — there is no "
    "extraction step here that could have invented it. Tiered SINGLE_SOURCE "
    "because one authoritative publisher reports it; the URL returns the same "
    "value on request."
)


def _current_year() -> int:
    return datetime.now(timezone.utc).year


_iso3_cache: dict[str, str] = {}


def _resolve_iso3(country: str) -> str | None:
    """Map a country name to its ISO3 code using the World Bank's own country
    list, so the mapping stays correct without shipping a lookup table that
    silently goes stale.

    Only *successful* lookups are cached, which is the whole reason this is a
    dict rather than an lru_cache. Caching a failure would let one transient
    network blip disable official statistics for the lifetime of the process,
    and the symptom would be a brief quietly missing two dimensions.
    """
    wanted = country.strip().lower()
    if wanted in _iso3_cache:
        return _iso3_cache[wanted]

    try:
        resp = requests.get(
            f"{WORLDBANK_BASE}/country",
            params={"format": "json", "per_page": "400"},
            timeout=settings.HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return None

    if not isinstance(payload, list) or len(payload) < 2:
        return None

    for entry in payload[1] or []:
        if str(entry.get("name", "")).strip().lower() == wanted:
            code = entry.get("id")
            if code:
                _iso3_cache[wanted] = code
            return code
    return None


def _fetch_gho(code: str, iso3: str) -> tuple[str, int, str] | None:
    """Latest published value for a GHO indicator. Returns (value, year, url).

    Two filters matter. Rows are disaggregated by sex, so only the both-sexes
    row is comparable to a headline figure. And some GHO series include
    *projections* — the tobacco series currently carries a 2030 row — which
    must never be reported as an observed fact, so future years are dropped.
    """
    params = {"$filter": f"SpatialDim eq '{iso3}'"}
    resp = requests.get(
        f"{GHO_BASE}/{code}", params=params, timeout=settings.HTTP_TIMEOUT_SECONDS
    )
    resp.raise_for_status()
    rows = resp.json().get("value") or []

    this_year = _current_year()
    usable = [
        r
        for r in rows
        if r.get("NumericValue") is not None
        and r.get("Dim1") in (None, "", "SEX_BTSX")
        and isinstance(r.get("TimeDim"), int)
        and r["TimeDim"] <= this_year
    ]
    if not usable:
        return None

    latest = max(usable, key=lambda r: r["TimeDim"])
    # `Value` carries the confidence interval ("26.5 [22.1-31.2]"); reporting
    # the interval is more honest than the bare point estimate, so prefer it.
    rendered = str(latest.get("Value") or round(latest["NumericValue"], 2))
    return rendered, latest["TimeDim"], resp.url


def _fetch_worldbank(code: str, iso3: str) -> tuple[str, int, str] | None:
    """Latest non-null value for a World Bank indicator. Returns (value, year, url)."""
    resp = requests.get(
        f"{WORLDBANK_BASE}/country/{iso3}/indicator/{code}",
        params={"format": "json", "per_page": "100"},
        timeout=settings.HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    payload = resp.json()

    # An archived or mistyped indicator returns a one-element list holding a
    # message object rather than a two-element [metadata, data] list.
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        return None

    usable = [r for r in payload[1] if r.get("value") is not None and r.get("date")]
    if not usable:
        return None

    latest = max(usable, key=lambda r: int(r["date"]))
    return str(round(float(latest["value"]), 3)), int(latest["date"]), resp.url


def _mock_records() -> list[dict]:
    """Deterministic stand-ins so MOCK mode exercises this node with no
    network, matching the shape the live fetchers return. The values are the
    real ones for India, so the mock brief reads like a real one."""
    return [
        {
            "claim_id": "gho-bp_04-mock",
            "dimension": "cv_burden",
            "publisher": "WHO Global Health Observatory",
            "code": "BP_04",
            "measure": "prevalence of raised blood pressure among adults aged 30-79, "
            "age-standardised",
            "unit": "% of adults",
            "value": "26.5 [22.1-31.2]",
            "year": 2019,
            "url": f"{GHO_BASE}/BP_04?$filter=SpatialDim%20eq%20%27MCK%27",
        },
        {
            "claim_id": "wb-shmedbedszs-mock",
            "dimension": "health_system",
            "publisher": "World Bank Open Data",
            "code": "SH.MED.BEDS.ZS",
            "measure": "hospital beds",
            "unit": "per 1,000 people",
            "value": "1.59",
            "year": 2021,
            "url": f"{WORLDBANK_BASE}/country/MCK/indicator/SH.MED.BEDS.ZS?format=json",
        },
    ]


def _gather(country: str, iso3: str, dimensions: set[str]) -> tuple[list[dict], list[str]]:
    """Fetch every in-scope indicator in parallel. One failure costs one
    indicator, never the run."""
    tasks: list[tuple] = []
    for code, dimension, measure, unit in _GHO_INDICATORS:
        if dimension in dimensions:
            tasks.append((_fetch_gho, code, dimension, measure, unit,
                          "WHO Global Health Observatory", f"gho-{code.lower()}-{iso3.lower()}"))
    for code, dimension, measure, unit in _WORLDBANK_INDICATORS:
        if dimension in dimensions:
            tasks.append((_fetch_worldbank, code, dimension, measure, unit,
                          "World Bank Open Data",
                          f"wb-{code.lower().replace('.', '')}-{iso3.lower()}"))

    def run(task):
        fetch, code, dimension, measure, unit, publisher, claim_id = task
        try:
            result = fetch(code, iso3)
        except Exception as exc:
            return None, f"{publisher} indicator {code} unavailable ({type(exc).__name__})."
        if result is None:
            return None, f"{publisher} publishes no usable {code} value for {country}."
        value, year, url = result
        return {
            "claim_id": claim_id,
            "dimension": dimension,
            "publisher": publisher,
            "code": code,
            "measure": measure,
            "unit": unit,
            "value": value,
            "year": year,
            "url": url,
        }, None

    records: list[dict] = []
    warnings: list[str] = []
    if not tasks:
        return records, warnings

    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_FETCHES, len(tasks))) as pool:
        for record, warning in pool.map(run, tasks):
            if record:
                records.append(record)
            if warning:
                warnings.append(warning)
    return records, warnings


def _claim_text(record: dict, country: str) -> str:
    return (
        f"In {country}, {record['measure']} was {record['value']} "
        f"{record['unit']} in {record['year']} "
        f"({record['publisher']}, indicator {record['code']})."
    )


def official_data_node(state: CityResearchState) -> dict:
    """Emits fact-checked claims directly, bypassing the LLM adjudicator.

    See the module docstring for why that is sound here: nothing in this path
    is model-generated, so there is no fabrication to detect. The claims still
    pass through persistence, coverage evaluation and the report unchanged.
    """
    if not settings.ENABLE_OFFICIAL_DATA:
        return {}

    city = state["city"]
    country = state.get("country")
    focus = set(state.get("focus_dimensions") or state.get("dimensions") or [])
    dimensions = focus & DIMENSIONS_SERVED

    if not dimensions:
        return {}

    # Claim ids are deterministic, so a retry pass recognises its own earlier
    # work. Note the fetch still happens and the duplicate is dropped after,
    # which is deliberate: a dimension only comes back into focus if it is
    # still uncovered, and the usual reason for that is an indicator that
    # failed transiently on the first pass and deserves another attempt.
    already = {fc.claim_id for fc in state.get("fact_checked", [])}

    if settings.is_mock:
        warnings: list[str] = []
        records = [r for r in _mock_records() if r["dimension"] in dimensions]
    else:
        if not country:
            return {
                "warnings": [
                    "Skipped official statistics: these APIs are keyed by country and "
                    "no country was supplied with the request. Pass `country` to get "
                    "WHO and World Bank context alongside the city findings."
                ]
            }
        iso3 = _resolve_iso3(country)
        if not iso3:
            return {
                "warnings": [
                    f"Skipped official statistics: could not resolve '{country}' to an "
                    f"ISO3 country code via the World Bank country list."
                ]
            }
        records, warnings = _gather(country, iso3, dimensions)

    records = [r for r in records if r["claim_id"] not in already]
    if not records:
        return {"warnings": warnings}

    candidates: list[SourceCandidate] = []
    crawl_results: list[CrawlabilityResult] = []
    passages: list[ExtractedPassage] = []
    fact_checked: list[FactCheckedClaim] = []

    for record in records:
        url = record["url"]
        title = f"{record['publisher']} — {record['code']}"
        text = _claim_text(record, country or city)
        # The hostname, not the publisher's name, to match what every other
        # agent puts in this field. The fact-checker counts distinct domains
        # to judge independence, so a second spelling of one source would let
        # two WHO indicators corroborate each other. The readable publisher
        # name is carried in the title and in the claim text instead.
        domain = domain_of(url)

        candidates.append(
            SourceCandidate(
                url=url,
                title=title,
                snippet=text,
                query_used=f"official-api:{record['code']}",
                dimension=record["dimension"],
            )
        )
        # A verdict is recorded rather than skipped, so the source registry
        # accounts for every source behind the brief — an API read is still a
        # source, and non-negotiable #7 does not have an exemption for it.
        crawl_results.append(
            CrawlabilityResult(
                url=url,
                verdict=CrawlVerdict.ALLOWED,
                reason="Official public REST API whose terms permit programmatic "
                "access; no robots.txt gate applies to an API read.",
            )
        )
        # Indexed like any passage, which is what lets the fact-checking agent
        # cite an official statistic as corroboration for a web claim.
        passages.append(
            ExtractedPassage(
                url=url,
                domain=domain,
                title=title,
                text=text,
                dimension=record["dimension"],
            )
        )
        fact_checked.append(
            FactCheckedClaim(
                claim_id=record["claim_id"],
                text=text,
                tier=ConfidenceTier.SINGLE_SOURCE,
                source_url=url,
                source_domain=domain,
                dimension=record["dimension"],
                reasoning=_TIER_REASONING,
                # Country-level data asserted in a city brief. The report turns
                # this into a visible warning on the line itself.
                national_vs_city_flag=True,
            )
        )

    return {
        "candidates": candidates,
        "crawl_results": crawl_results,
        "passages": passages,
        "fact_checked": fact_checked,
        "warnings": warnings,
    }
