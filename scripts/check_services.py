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
        "fastembed": "fastembed",
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


def _check_one_model(label: str, model: str, effort: str, max_tokens: int) -> bool:
    """One minimal chat call — proves the key, the base URL and, importantly,
    that the model ID has not been retired out from under us."""
    try:
        from openai import OpenAI

        from app.llm.client import _answer_text

        client = OpenAI(
            api_key=settings.LLM_API_KEY or "local", base_url=settings.LLM_BASE_URL
        )
        kwargs = {}
        if effort:
            kwargs["reasoning_effort"] = effort
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            **kwargs,
        )
        # Unwrapped via the client's helper rather than read directly: a
        # reasoning model can return a list of chunks here, and `.strip()` on
        # a list would report the wrong failure for the right problem.
        text = _answer_text(response.choices[0].message).strip()
        if not text:
            return _report(
                label,
                False,
                f"{model} returned empty content — thinking tokens likely "
                f"consumed the whole budget. Raise the max-tokens setting for "
                f"this role, or lower its reasoning effort.",
            )
        return _report(label, True, f"{model} replied {text!r}")
    except Exception as exc:
        # The three ways a provider switch fails, in the order they bite. All
        # three present as an opaque 400 or 401 from the shim, so the hint is
        # worth more than the exception text.
        message = f"{type(exc).__name__}: {exc}".lower()
        hint = ""
        if "reasoning_effort" in message or "unknown" in message or "unsupported" in message:
            hint = (
                f" — this model rejects a parameter we sent rather than "
                f"ignoring it. It was called with reasoning_effort={effort!r}; "
                f"clear the setting for this role in .env. The two roles have "
                f"separate settings precisely because one model can implement "
                f"the parameter while the other does not."
            )
        elif "api key" in message or "unauthor" in message or "401" in message:
            hint = (
                f" — the key does not match LLM_BASE_URL ({settings.LLM_BASE_URL}). "
                f"Keys are per-provider; switching the base URL means a new key."
            )
        elif "model" in message or "not found" in message or "404" in message:
            hint = (
                f" — '{model}' is not a model this provider serves, or it has "
                f"been retired. Check the provider's model list."
            )
            if settings.llm_is_local:
                hint = (
                    f" — Ollama has no model named '{model}'. Pull it first: "
                    f"`ollama pull {model}`, and check `ollama list` for the "
                    f"exact tag — the name must match including the `:tag`."
                )
        elif settings.llm_is_local and (
            "connection" in message or "refused" in message
        ):
            hint = (
                f" — nothing is listening on {settings.LLM_BASE_URL}. Start the "
                f"server with `ollama serve` (see the context-window check below "
                f"for the environment variable it needs)."
            )
        return _report(label, False, f"{type(exc).__name__}: {exc}{hint}")


def check_llm() -> bool:
    """Both configured models, because the pipeline uses both and they fail
    independently.

    The bulk model is the one worth checking most and the one a single-model
    check missed entirely: it serves claim extraction and Graphiti's entity
    extraction, so a retired or misspelled id there produces a run with no
    claims and an empty graph while the judgement model — and therefore the
    old version of this check — reported green. Each role also carries its own
    reasoning effort and token ceiling, and on the Mistral defaults those
    differ deliberately: Small 4 implements reasoning_effort and Large 3
    rejects it, so the two calls are genuinely different requests and only one
    of them can fail on that parameter.
    """
    if not settings.LLM_API_KEY and not settings.llm_is_local:
        return _report(
            "LLM",
            False,
            "LLM_API_KEY is not set in .env. To run inference locally with no "
            "key, set LLM_BASE_URL=http://localhost:11434/v1 instead.",
        )

    judgement = _check_one_model(
        "LLM (judgement)",
        settings.LLM_MODEL,
        settings.LLM_REASONING_EFFORT,
        settings.LLM_MAX_TOKENS,
    )
    # Skipped when both roles resolve to one model, so the common
    # single-model configuration does not pay for a duplicate call.
    if settings.LLM_BULK_MODEL == settings.LLM_MODEL and (
        settings.LLM_BULK_REASONING_EFFORT == settings.LLM_REASONING_EFFORT
    ):
        return judgement

    bulk = _check_one_model(
        "LLM (bulk)",
        settings.LLM_BULK_MODEL,
        settings.LLM_BULK_REASONING_EFFORT,
        settings.LLM_BULK_MAX_TOKENS,
    )
    return judgement and bulk


def check_embeddings() -> bool:
    """Actually encode something, rather than just importing fastembed.

    Worth its own check because this one component sits under both evidence
    stores — Milvus embeds the passage and the question, and Graphiti's
    retrieval embeds the question through the same LocalEmbedder. When it
    fails, `/ask` reports the vector store AND the knowledge graph as
    unavailable, which reads as two unrelated outages and sends you off
    checking two databases that are both fine.

    Importing the package is not enough: the weights are downloaded from
    Hugging Face on first use, so the failure is at encode time, not import
    time. On a network with TLS interception that surfaces as a certificate
    error from deep inside httpx.
    """
    try:
        from app.llm.embeddings import embed_one

        width = len(embed_one("preflight probe"))
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        hint = ""
        if "certificate" in message.lower() or "ssl" in message.lower():
            hint = (
                " — this is a TLS failure reaching huggingface.co, not a "
                "problem with the model. On a corporate network, point "
                "REQUESTS_CA_BUNDLE and SSL_CERT_FILE at your proxy's CA "
                "bundle, or pre-populate the cache from a machine that can "
                "reach it and copy it to the path in FASTEMBED_CACHE_PATH."
            )
        elif isinstance(exc, ImportError):
            hint = (
                " — `pip install -r requirements.txt` in the interpreter "
                "running uvicorn. This moved from sentence-transformers to "
                "fastembed, so an older environment satisfies the import list "
                "but not this one."
            )
        return _report("Embeddings", False, f"{message}{hint}")
    return _report(
        "Embeddings", True, f"{settings.EMBEDDING_MODEL} encoded to {width} dimensions"
    )


def check_local_context_window() -> bool:
    """For a local model server, report the context window it will actually use.

    This exists because the failure it catches is silent, and it is the one
    that bites every first attempt at running this pipeline on Ollama. Ollama
    defaults to a small context — 4k unless the machine has a lot of VRAM —
    and it *truncates* longer prompts rather than refusing them. Graphiti's
    entity-extraction prompts are well past 4k. The result is a run that
    completes, writes a knowledge graph with no fact edges, and reports no
    error anywhere: the prompt was cut off mid-instruction and the model
    answered a question nobody asked.

    It cannot be fixed from our side. Ollama's own OpenAI-compatibility
    documentation states that the OpenAI API has no way to set the context
    size, so `num_ctx` has to be set on the server — hence a check rather than
    a setting.

    Runs after check_llm deliberately. That check makes a real completion,
    which loads the model, so /api/ps has something to report by the time we
    get here.
    """
    if not settings.llm_is_local:
        return True

    import requests

    root = settings.LLM_BASE_URL.rsplit("/v1", 1)[0].rstrip("/")
    try:
        running = requests.get(f"{root}/api/ps", timeout=5).json().get("models", [])
    except Exception as exc:
        return _report(
            "Local context window",
            False,
            f"could not reach the Ollama admin API at {root} "
            f"({type(exc).__name__}) — is `ollama serve` running?",
        )

    if not running:
        return _report(
            "Local context window",
            True,
            "no model loaded yet, so the window cannot be read. Start a run, "
            "then check `ollama ps` shows CONTEXT >= 32768",
        )

    smallest = min(model.get("context_length") or 0 for model in running)
    loaded = ", ".join(model.get("name", "?") for model in running)
    if smallest and smallest < 32768:
        return _report(
            "Local context window",
            False,
            f"{loaded} has only {smallest} tokens of context. Graphiti's "
            f"prompts exceed this and Ollama truncates silently, which shows "
            f"up as a graph with no fact edges. Restart the server with "
            f"OLLAMA_CONTEXT_LENGTH=32768 set in its environment.",
        )
    return _report("Local context window", True, f"{loaded}: {smallest} tokens")


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
        # After check_llm, which loads the model so its window can be read.
        check_local_context_window(),
        # Before the two stores that depend on it, so a failure here explains
        # the failures below rather than being buried under them.
        check_embeddings(),
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
