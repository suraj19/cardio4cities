"""
Preflight check for the three external dependencies. Run this before a demo:

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
        return _report("LLM", False, f"{type(exc).__name__}: {exc}")


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
                count = session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
            return _report("Neo4j", True, f"{settings.NEO4J_URI} reachable, {count} node(s)")
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
    if settings.TAVILY_API_KEY:
        return _report("Search", True, "Tavily key present")
    return _report(
        "Search", True, "no Tavily key — falling back to duckduckgo-search (noisier results)"
    )


def main() -> int:
    print(f"RUN_MODE={settings.RUN_MODE}\n")
    if settings.is_mock:
        print("MOCK mode: no external services are used. Set RUN_MODE=LIVE to check them.")
        return 0

    results = [check_llm(), check_neo4j(), check_milvus(), check_search()]
    print()
    if all(results):
        print("All checks passed — ready to research a city.")
        return 0
    print("Fix the failures above before demoing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
