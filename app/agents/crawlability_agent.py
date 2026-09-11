"""
Crawlability Detection Agent.

Non-negotiable #3: this MUST run before anything is crawled, and its
verdict must actually gate the extraction agent (extraction_node filters
to ALLOWED urls before issuing a single content request).

Checks, in order, stopping at the first DENY:
  1. Known ToS-restrictive platforms (hard denylist — LinkedIn, Facebook,
     Instagram, X/Twitter — scraping these violates their terms regardless
     of what robots.txt happens to say)
  2. robots.txt disallow rules for this specific path
  3. An explicit `X-Robots-Tag: noindex/nofollow` response header

This agent never fetches page *content* — only robots.txt and response
headers, which is the minimum needed to make the crawl/no-crawl decision.

Two operational details that matter more than they look:

  * robots.txt is fetched with an explicit timeout. urllib's
    RobotFileParser.read() has no timeout and will hang indefinitely on a
    black-holed host, which in a live demo looks like the app freezing.
  * Results are cached per domain and the checks run on a thread pool,
    because this is pure network wait and runs over every candidate URL.
"""
from __future__ import annotations

import threading
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from app.config import settings
from app.graph.state import CityResearchState
from app.models.schemas import CrawlabilityResult, CrawlVerdict
from app.util import domain_of

USER_AGENT = "CARDIO4Cities-Research-Bot/0.1"

# Platforms whose Terms of Service explicitly prohibit automated scraping,
# independent of what robots.txt says. Treated as a hard deny.
TOS_RESTRICTED_DOMAINS = {
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
}


def _check_tos_denylist(url: str) -> CrawlabilityResult | None:
    domain = domain_of(url)
    for restricted in TOS_RESTRICTED_DOMAINS:
        if domain == restricted or domain.endswith("." + restricted):
            return CrawlabilityResult(
                url=url,
                verdict=CrawlVerdict.DENIED,
                reason=f"Domain '{domain}' has ToS provisions prohibiting automated "
                f"scraping, independent of robots.txt.",
            )
    return None


class _RobotsCache:
    """One robots.txt fetch per origin per run, shared across threads."""

    def __init__(self):
        self._parsers: dict[str, tuple[urllib.robotparser.RobotFileParser | None, str]] = {}
        self._lock = threading.Lock()

    def get(self, url: str) -> tuple[urllib.robotparser.RobotFileParser | None, str]:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            if origin in self._parsers:
                return self._parsers[origin]

        entry = self._fetch(origin)
        with self._lock:
            self._parsers[origin] = entry
        return entry

    @staticmethod
    def _fetch(origin: str):
        import requests

        try:
            resp = requests.get(
                f"{origin}/robots.txt",
                timeout=settings.HTTP_TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            )
        except Exception as exc:
            return None, f"robots.txt unreachable ({type(exc).__name__})"

        if resp.status_code >= 400:
            return None, f"robots.txt returned HTTP {resp.status_code}"

        parser = urllib.robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser, "robots.txt fetched"


def _check_robots_txt(url: str, cache: _RobotsCache) -> CrawlabilityResult:
    parser, note = cache.get(url)

    if parser is None:
        # No robots.txt is not a prohibition. The convention is that absence
        # means unrestricted, but we record WHY we allowed it so an auditor
        # can tell "explicitly permitted" apart from "nobody said no".
        return CrawlabilityResult(
            url=url,
            verdict=CrawlVerdict.ALLOWED,
            reason=f"{note}; defaulting to allowed per convention (absence of a "
            f"robots.txt is not a prohibition).",
        )

    if parser.can_fetch(USER_AGENT, url):
        return CrawlabilityResult(
            url=url, verdict=CrawlVerdict.ALLOWED, reason="Permitted by robots.txt."
        )
    return CrawlabilityResult(
        url=url,
        verdict=CrawlVerdict.DENIED,
        reason="Disallowed by robots.txt for this path.",
    )


def _check_noindex_header(url: str) -> CrawlabilityResult | None:
    """A site can permit crawling in robots.txt and still forbid indexing or
    reuse via X-Robots-Tag. Honouring it costs one HEAD request."""
    import requests

    try:
        resp = requests.head(
            url,
            timeout=settings.HTTP_TIMEOUT_SECONDS,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    except Exception:
        return None  # inconclusive; robots.txt verdict stands

    tag = resp.headers.get("X-Robots-Tag", "").lower()
    if "noindex" in tag or "none" in tag:
        return CrawlabilityResult(
            url=url,
            verdict=CrawlVerdict.DENIED,
            reason=f"Server sent 'X-Robots-Tag: {tag}', an explicit instruction "
            f"not to index or reuse this content.",
        )
    return None


def _mock_check(url: str) -> CrawlabilityResult:
    """Deterministic mock so behavior is demoable offline: government and
    NGO domains pass, the local-news domain is treated as paywalled/denied,
    and the LinkedIn-style URL is denied via the ToS denylist — exactly
    mirroring what the real checks would very plausibly find."""
    tos_hit = _check_tos_denylist(url)
    if tos_hit:
        return tos_hit
    if "localnews.example.com" in url:
        return CrawlabilityResult(
            url=url,
            verdict=CrawlVerdict.DENIED,
            reason="Simulated paywall/robots disallow for demo local-news domain.",
        )
    return CrawlabilityResult(
        url=url, verdict=CrawlVerdict.ALLOWED, reason="Mock: government/NGO domain, permitted."
    )


def _check_live(url: str, cache: _RobotsCache) -> CrawlabilityResult:
    tos_hit = _check_tos_denylist(url)
    if tos_hit:
        return tos_hit

    robots_verdict = _check_robots_txt(url, cache)
    if robots_verdict.verdict == CrawlVerdict.DENIED:
        return robots_verdict

    return _check_noindex_header(url) or robots_verdict


def crawlability_node(state: CityResearchState) -> dict:
    candidates = state.get("candidates", [])

    # candidates accumulate across retries; re-checking a URL we already have
    # a verdict for would duplicate the source registry and waste requests.
    already_checked = {r.url for r in state.get("crawl_results", [])}
    pending = [c.url for c in candidates if c.url not in already_checked]
    if not pending:
        return {"crawl_results": []}

    if settings.is_mock:
        return {"crawl_results": [_mock_check(url) for url in pending]}

    cache = _RobotsCache()
    with ThreadPoolExecutor(max_workers=settings.LLM_MAX_CONCURRENCY) as pool:
        results = list(pool.map(lambda url: _check_live(url, cache), pending))

    return {"crawl_results": results}
