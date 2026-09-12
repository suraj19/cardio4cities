"""
Preflight check for the external dependencies. Run this before a demo:

    python -m scripts.check_services

Each check is independent, so a failure tells you exactly which service to
fix rather than surfacing as a confusing mid-pipeline error.
"""
import sys

from app.config import settings

OK = "  ok   "
FAIL = " FAIL  "


def _report(name: str, ok: bool, detail: str) -> bool:
    print(f"[{OK if ok else FAIL}] {name}: {detail}")
    return ok


def check_dependencies() -> bool:
    """Import every third-party package the LIVE path needs.

    First check deliberately, because a missing package does not present as a
    missing package. `bs4` absent means every page fetch raises inside the
    extraction agent's per-source handler, which turns it into one "Could not
    read <url>" warning per candidate — thirty lines that look like a web
    outage. The run completes, reports every dimension as a gap, and never
    names the cause.
    """
    required = {
        "bs4": "beautifulsoup4",
        "lxml": "lxml",
        "requests": "requests",
        "openai": "openai",
        "pymilvus": "pymilvus",
        "sentence_transformers": "sentence-transformers",
        "neo4j": "neo4j",
        "graphiti_core": "graphiti-core",
    }
    missing = []
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        return _report(
            "Dependencies",
            False,
            f"not installed: {', '.join(missing)} — run "
            f"`pip install -r requirements.txt`",
        )
    return _report("Dependencies", True, f"all {len(required)} imports resolved")


def check_llm() -> bool:
    """One minimal chat call — proves the key, the base URL and, importantly,
    that the model ID has not been retired out from under us."""
    if not settings.LLM_API_KEY:
        return _report("LLM", False, "LLM_API_KEY is not set in .env")
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)
        kwargs = {}
        if settings.LLM_REASONING_EFFORT:
            kwargs["reasoning_effort"] = settings.LLM_REASONING_EFFORT
        response = client.chat.completions.create(
            model=settings.LLM_MODEL,
            max_tokens=settings.LLM_MAX_TOKENS,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            **kwargs,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            return _report(
                "LLM",
                False,
                f"{settings.LLM_MODEL} returned empty content — thinking tokens "
                f"likely consumed the whole budget. Raise LLM_MAX_TOKENS.",
            )
        return _report("LLM", True, f"{settings.LLM_MODEL} replied {text!r}")
    except Exception as exc:
        # The three ways a provider switch fails, in the order they bite. All
        # three present as an opaque 400 or 401 from the shim, so the hint is
        # worth more than the exception text.
        message = f"{type(exc).__name__}: {exc}".lower()
        hint = ""
        if "reasoning_effort" in message or "unknown" in message or "unsupported" in message:
            hint = (
                " — this provider rejects a parameter we sent rather than "
                "ignoring it. Clear LLM_REASONING_EFFORT in .env."
            )
        elif "api key" in message or "unauthor" in message or "401" in message:
            hint = (
                f" — the key does not match LLM_BASE_URL ({settings.LLM_BASE_URL}). "
                f"Keys are per-provider; switching the base URL means a new key."
            )
        elif "model" in message or "not found" in message or "404" in message:
            hint = (
                f" — '{settings.LLM_MODEL}' is not a model this provider serves, "
                f"or it has been retired. Check the provider's model list."
            )
        return _report("LLM", False, f"{type(exc).__name__}: {exc}{hint}")


def check_neo4j() -> bool:
    if not settings.NEO4J_URI:
        return _report("Neo4j", False, "NEO4J_URI is not set in .env")
    try:
        from neo4j import GraphDatabase, basic_auth

        driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=basic_auth(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
        try:
            driver.verify_connectivity()
            with driver.session(database="neo4j") as session:
                nodes = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
                # Edge count, not node count, is the number that matters. A
                # graph with entities but zero RELATES_TO edges holds no facts,
                # and that is the shape a silently failing write leaves behind.
                edges = session.run(
                    "MATCH ()-[e:RELATES_TO]->() RETURN count(e) AS c"
                ).single()["c"]
            detail = f"{settings.NEO4J_URI} reachable, {nodes} node(s), {edges} fact edge(s)"
            if nodes and not edges:
                detail += " — entities exist but no facts; check the run warnings"
            return _report("Neo4j", True, detail)
        finally:
            driver.close()
    except Exception as exc:
        return _report(
            "Neo4j",
            False,
            f"{type(exc).__name__}: {exc} — a Sandbox that has expired gets a new "
            f"IP and password, so re-copy both from sandbox.neo4j.com",
        )


def check_milvus() -> bool:
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=settings.MILVUS_URI, token=settings.MILVUS_TOKEN or None)
        collections = client.list_collections()
        mode = "Milvus Lite (embedded)" if not settings.MILVUS_URI.startswith("http") else "server"
        return _report("Milvus", True, f"{mode} at {settings.MILVUS_URI}, collections={collections}")
    except Exception as exc:
        return _report("Milvus", False, f"{type(exc).__name__}: {exc}")


def check_search() -> bool:
    """Actually resolves and calls the fallback client. A key check alone would
    have missed the failure this exists to catch: `duckduckgo-search` was
    renamed to `ddgs`, and an old virtualenv importing the pre-rename name
    returns zero results for every query."""
    if settings.TAVILY_API_KEY:
        return _report("Search", True, "Tavily key present")

    from app.agents.search_agent import _duckduckgo_search
    from app.models.schemas import PlannedQuery

    try:
        hits = _duckduckgo_search(
            PlannedQuery(text="Pune municipal health department", dimension="cv_burden")
        )
    except Exception as exc:
        return _report(
            "Search",
            False,
            f"no Tavily key and the fallback client failed — {type(exc).__name__}: {exc}",
        )
    if not hits:
        return _report(
            "Search", False, "no Tavily key; fallback client returned 0 results (rate limited?)"
        )
    return _report(
        "Search", True, f"no Tavily key — ddgs returned {len(hits)} result(s) (noisier)"
    )


def check_official_apis() -> bool:
    """WHO GHO and the World Bank, checked by actually pulling a value rather
    than just pinging the host. A reachable API that has archived the
    indicator we ask for is not a working dependency, and that failure is
    invisible from a status code — the World Bank returns HTTP 200 with a
    message object in place of the data array."""
    if not settings.ENABLE_OFFICIAL_DATA:
        return _report("Official APIs", True, "disabled via ENABLE_OFFICIAL_DATA")

    from app.agents.official_data_agent import _fetch_gho, _fetch_worldbank

    probes = [
        ("WHO GHO", lambda: _fetch_gho("BP_04", "IND")),
        ("World Bank", lambda: _fetch_worldbank("SH.MED.BEDS.ZS", "IND")),
    ]
    details, ok = [], True
    for name, probe in probes:
        try:
            result = probe()
        except Exception as exc:
            details.append(f"{name} {type(exc).__name__}")
            ok = False
            continue
        if result is None:
            details.append(f"{name} reachable but published no usable value")
            ok = False
        else:
            value, year, _ = result
            details.append(f"{name} {value} ({year})")

    return _report("Official APIs", ok, ", ".join(details))


def main() -> int:
    print(f"RUN_MODE={settings.RUN_MODE}\n")
    if settings.is_mock:
        print("MOCK mode: no external services are used. Set RUN_MODE=LIVE to check them.")
        return 0

    results = [
        # Dependencies first: a missing package makes several of the checks
        # below fail with errors that describe the symptom rather than the
        # cause, and there is no point diagnosing a network path that cannot
        # be reached because a parser was never installed.
        check_dependencies(),
        check_llm(),
        check_neo4j(),
        check_milvus(),
        check_search(),
        check_official_apis(),
    ]
    print()
    if all(results):
        print("All checks passed — ready to research a city.")
        return 0
    print("Fix the failures above before demoing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
