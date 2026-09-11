"""
Shared data contracts. Every agent reads/writes these shapes so the
LangGraph state stays typed and stores don't have to guess field names.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

# Note on timestamps: these models carry none. Every record that needs a
# timestamp gets an authoritative one from the relational store's column
# defaults at write time. Stamping the in-memory model too would give each
# fact two timestamps that disagree by however long the pipeline took, and
# the one the API returns would be the one nobody set deliberately.

# ---------------------------------------------------------------------
# Research dimensions
#
# "Understanding a city" is decomposed into the five things a City Lead
# actually walks into a stakeholder meeting needing to know. Opportunities
# and risks are deliberately NOT a dimension: they are a judgement the
# City Lead makes, and the honest machine contribution to them is the gap
# log — what we could not establish — rather than invented commentary.
#
# The brief is prompt text, not documentation: it is what the query
# generator and the claim extractor are told the dimension means, so
# changing it changes system behaviour.
# ---------------------------------------------------------------------
DIMENSION_BRIEFS: dict[str, str] = {
    "cv_burden": (
        "Cardiovascular disease burden and risk factors in the city: prevalence "
        "of hypertension, type 2 diabetes and dyslipidaemia, mortality and "
        "screening coverage statistics."
    ),
    "healthcare_programmes": (
        "Existing programmes and interventions running in the city: screening "
        "drives, hypertension or diabetes control initiatives, primary-care "
        "expansion, who runs them and since when."
    ),
    "policy_initiatives": (
        "Policy, regulation and official plans affecting cardiovascular health "
        "in the city: municipal health strategies, NCD action plans, salt or "
        "tobacco regulation, budget commitments."
    ),
    "stakeholders": (
        "Organizations and institutions active in the city: municipal health "
        "department, ministries, hospitals, universities, NGOs and funders, "
        "and the roles they play."
    ),
    "health_system": (
        "Health system capacity in the city: primary health centres, workforce, "
        "medicines availability, financing and referral pathways."
    ),
}

DIMENSIONS: list[str] = list(DIMENSION_BRIEFS)

DIMENSION_LABELS: dict[str, str] = {
    "cv_burden": "Cardiovascular Burden & Risk Factors",
    "healthcare_programmes": "Healthcare Programmes",
    "policy_initiatives": "Policy & Regulation",
    "stakeholders": "Stakeholders & Organizations",
    "health_system": "Health System Capacity",
}


def dimension_label(dimension: str) -> str:
    return DIMENSION_LABELS.get(dimension, dimension.replace("_", " ").title())


class CrawlVerdict(str, Enum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


class ConfidenceTier(str, Enum):
    VERIFIED = "VERIFIED"            # corroborated by >= MIN_SOURCES_FOR_VERIFIED independent sources
    SINGLE_SOURCE = "SINGLE_SOURCE"  # one source only, flagged for human review
    CONFLICTING = "CONFLICTING"      # two+ sources disagree
    UNSUPPORTED = "UNSUPPORTED"      # claim could not be corroborated at all -> becomes a Gap, NOT a Fact


class PlannedQuery(BaseModel):
    """A search query together with the dimension it is meant to serve, so a
    result's dimension is known from discovery onward rather than guessed at
    report time."""
    text: str
    dimension: str


class SourceCandidate(BaseModel):
    """A URL discovered by the search agent, before crawlability has been checked."""
    url: str
    title: str = ""
    snippet: str = ""
    query_used: str = ""
    dimension: str = "healthcare_programmes"


class CrawlabilityResult(BaseModel):
    url: str
    verdict: CrawlVerdict
    reason: str


class ExtractedPassage(BaseModel):
    """Raw text pulled from an ALLOWED source."""
    url: str
    domain: str
    title: str
    text: str
    dimension: str = "healthcare_programmes"


class Claim(BaseModel):
    """A single atomic factual claim extracted from one passage, pre-fact-check."""
    claim_id: str
    text: str
    source_url: str
    source_domain: str
    is_city_level: bool  # False if this looks like national/regional data being asserted about the city
    dimension: str = "healthcare_programmes"


class FactCheckedClaim(BaseModel):
    """A claim after the independent fact-checking agent has adjudicated it.

    `source_url` is the origin passage and is always populated — non-negotiable
    #7 means a SINGLE_SOURCE or CONFLICTING fact has to be traceable too, not
    just a VERIFIED one. `corroborating_urls` is the *additional* independent
    support the fact-checker actually cited, so it is empty for a genuinely
    single-sourced fact rather than padded with every other URL in the run.
    """
    claim_id: str
    text: str
    tier: ConfidenceTier
    source_url: str = ""
    source_domain: str = ""
    dimension: str = "healthcare_programmes"
    corroborating_urls: list[str] = Field(default_factory=list)
    conflicting_text: Optional[str] = None
    reasoning: str = ""
    national_vs_city_flag: bool = False

    @property
    def evidence_urls(self) -> list[str]:
        """Every URL backing this fact, origin first. This is what the report
        and the API render, so no fact can appear without provenance."""
        urls = [self.source_url] if self.source_url else []
        return urls + [u for u in self.corroborating_urls if u and u != self.source_url]


class Gap(BaseModel):
    """An UNSUPPORTED claim, or a research dimension that yielded nothing usable."""
    description: str
    dimension: str
    reason: str


class ResearchRequest(BaseModel):
    city: str
    country: Optional[str] = None


