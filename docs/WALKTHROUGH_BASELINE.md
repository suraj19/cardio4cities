# CARDIO4Cities — Component Walkthrough (baseline version)

Presentation preparation document. Every component of the application as it
stood **before** the recent round of changes. This is the version in the git
repository and the one currently deployed on Railway.

Section numbering matches [`WALKTHROUGH_CURRENT.md`](WALKTHROUGH_CURRENT.md)
so the two can be read side by side. Where this version differs from the
current one, the difference is called out inline as **→ later changed to …**
with the reason, so each change can be defended as a decision rather than a
tidy-up.

Everything **not** marked as later changed is identical in both versions —
which is most of the application. The agents, the trust model, the three
datastores and the data contracts are the baseline's, and the changes were
concentrated in three places: how a run is invoked, how many tokens it costs,
and how much memory it needs.

---

## 1. What the application does

Identical in both versions, and it is the part to lead with.

A City Lead has a stakeholder meeting about a city nobody on the team has
researched. This application takes the city name and produces an
evidence-linked intelligence brief by doing live internet research,
independently verifying what it finds, and persisting the result as a reusable
asset rather than a one-off document.

**This is not a summarisation tool, it is a verification pipeline that happens
to write a brief.** A summariser compresses what it was given; this system
decides what it is willing to assert, and says plainly what it could not
establish.

- **Every fact carries its evidence.** No retrievable source URL, no
  appearance in the brief at any confidence level.
- **Absence is reported, not hidden.** A dimension with nothing verifiable
  becomes an explicit gap with a stated reason.
- **National data is never presented as city data.** Country-level statistics
  are usable, but flagged as national on every individual fact.

### Five research dimensions

| Dimension key | What it covers |
|---|---|
| `cv_burden` | Cardiovascular disease burden — prevalence, mortality, risk factors |
| `health_system` | Health system capacity — facilities, workforce, access |
| `healthcare_programmes` | Programmes and interventions running in the city |
| `policy_initiatives` | Policy and regulation relevant to CVD |
| `stakeholders` | Organisations, agencies and people who matter locally |

### Two run modes

- **`MOCK`** — no API keys, no internet, no Neo4j. Deterministic fixtures
  drive the full ten-node graph.
- **`LIVE`** — real search, real model calls, real datastores.

---

## 2. Request lifecycle

**A research run is one blocking HTTP request.**

```
POST /research {city}
      → connection held open for the entire run (minutes)
      → 200 with the complete payload: report, facts, gaps, sources, timings
```

`async def research(req: ResearchRequest)` awaits the whole compiled graph and
returns only when the report exists. There is no job id, no progress, and no
way to ask "how far along is it".

This is the simplest correct design and it is fine locally, where nothing
between the client and the app enforces a deadline.

**→ later changed to a background job with pollable progress**, because
hosting platforms disagree. Railway closes a request after 5 minutes with no
bytes transferred and caps even a chatty one at 15 minutes. Under a proxy,
this design produces the worst available failure mode: the browser receives a
timeout while the container quietly finishes the work and saves the brief, so
a successful run looks broken. The run duration itself was never the problem;
coupling it to a socket was.

Two further consequences of the blocking design:

- **Two clicks on *Run* start two passes for the same city**, writing the same
  facts twice and contending for the SQLite write lock. Nothing deduplicates
  concurrent runs.
- **Nothing bounds how many runs are in flight**, against a store that
  accepts one writer.

---

## 3. Orchestration — `app/graph/`

### 3.1 `workflow.py` — the LangGraph

Ten nodes, wired identically to the current version:

```
planner → query_gen → search → official_data → crawlability
        → extraction → fact_check → graph_writer → coverage_evaluator
        → report → END
                 ↘ (if coverage insufficient and retries remain) → planner
```

- **The crawlability gate is an edge, not a branch.** `crawlability` always
  proceeds to `extraction`; the gate is enforced inside the extraction node,
  which only ever requests URLs in `allowed_urls`. A conditional edge could be
  reordered by a later change and stop applying; as an edge plus a filter,
  there is no code path that fetches a page body before a verdict exists.
- **`recursion_limit` is computed**, as
  `NODES_PER_PASS * (MAX_PLANNER_RETRIES + 1) + 5`, because LangGraph counts
  every node execution against it and a constant aborted the final permitted
  retry mid-pass.

`_timed(name, node)` already wraps every node and records per-node duration,
and `_summarise` already collects those durations into the response.

**→ later changed** so `_timed` also reports each stage transition to a job
registry and `_summarise` also carries a token-usage snapshot. The timing
existed; nobody could watch it happen.

### 3.2 `state.py` — `CityResearchState`

Byte-identical in both versions. Accumulating channels use the `append_list`
reducer so each pass adds to what earlier passes found; scalar channels are
replaced per pass. Replacing an accumulator makes the second planner pass
discard the first pass's evidence; accumulating a scalar makes the retry logic
read stale verdicts.

---

## 4. The ten agents — `app/agents/`

Each is a plain function taking state and returning a state delta. No agent
calls another; the graph is the only thing that sequences them.

### 4.1 `planner.py` — `planner_node`

Identical. All five dimensions on the first pass; on a retry, only the
dimensions the coverage evaluator reported as uncovered.

### 4.2 `query_gen.py` — `query_gen_node`

Identical. `QUERIES_PER_DIMENSION` (2) queries per dimension from the model,
with `_fallback_queries` supplying deterministic templates when the call
fails, so a model outage degrades search quality rather than stopping the run.

### 4.3 `search_agent.py` — `search_node`

Two providers: keyless **`ddgs`** (multi-backend DuckDuckGo, so one engine
blocking does not mean zero results) and **Tavily** when `TAVILY_API_KEY` is
set. Up to `MAX_SOURCES_PER_DIMENSION` (4) candidates per dimension, which
keeps a five-dimension brief balanced rather than letting one well-covered
dimension consume the whole budget. A provider failure is reported, not
swallowed — a silent zero-result search is indistinguishable from "this city
has no published information", the most misleading thing the system could say.

**This version has a source-domain policy the current one does not:**

- **`SOURCE_DOMAIN_DENYLIST`** — default `youtube.com,youtu.be,facebook.com,…`
- **`SOURCE_DOMAIN_ALLOWLIST`** — empty by default, meaning open discovery
- **`_domain_permitted`** — applies both, **before** the per-dimension cap, so
  uncitable results do not consume one of the four slots a dimension gets
- **`domain_matches`** in `app/util.py` — matches on the dot boundary, so
  `notyoutube.com` is not treated as `youtube.com`
- **Denylist wins over allowlist**, and when an allowlist narrowed discovery
  the brief discloses that it did — a narrowed search that does not say so is
  a coverage claim the system cannot support

Five tests cover this. **It is absent from the current version and should be
ported across, because `deck/slides.md` describes it.**

`_run_query` runs the queries **serially**. → later changed to run them
concurrently under `SEARCH_MAX_CONCURRENCY`, since search is pure network
wait and the serial loop was one of the two largest contributors to wall-clock
time.

### 4.4 `official_data_agent.py` — `official_data_node`

Byte-identical in both versions. Structured indicators from public-health REST
APIs with **no model involved**:

- **WHO Global Health Observatory** (`ghoapi.azureedge.net`) — `BP_04` (raised
  blood pressure), `NCDMORT3070` (premature NCD mortality), `NCD_BMI_30A`
  (obesity).
- **World Bank** — development indicators, with `_resolve_iso3` mapping the
  country to an ISO3 code.

This agent is why the system still produces something useful when the model
provider is unavailable — these facts arrive as structured data and need no
extraction. They are always flagged `national_vs_city_flag = True`, because
WHO and the World Bank report at country level, and never promoted above
`SINGLE_SOURCE`. Disabled with `ENABLE_OFFICIAL_DATA=false`.

### 4.5 `crawlability_agent.py` — `crawlability_node`

Runs **before anything is crawled**, and its verdict gates extraction. Checks
in order, stopping at the first DENY:

1. **ToS-restrictive platforms** — hard denylist (LinkedIn, Facebook,
   Instagram, X/Twitter), which scraping violates regardless of `robots.txt`.
2. **`robots.txt`** disallow rules for *this specific path*.
3. **`X-Robots-Tag: noindex/nofollow`**, via one `HEAD` request.

Never fetches page content — only `robots.txt` and response headers.

`robots.txt` is fetched with `requests` under an explicit timeout and parsed
from text, because `RobotFileParser.read()` has no timeout and hangs
indefinitely on a black-holed host, which in a demo looks like the app
freezing. `_RobotsCache` caches per origin.

**Absence is not prohibition, but the run records which it was.** A missing
`robots.txt` defaults to allowed and the stored reason says so, so an auditor
can distinguish "explicitly permitted" from "nobody said no".

Every verdict — ALLOWED *and* DENIED, with its reason — is written to the
relational source registry and exposed at `GET /sources/{city}`.

### 4.6 `extraction_agent.py` — `extraction_node`

Fetches allowed pages and turns them into candidate claims. One function,
`_process_source`, does all of it: fetch, parse, prompt, parse the response.

Content-type gating decides the parser — XML as XML, HTML as HTML, CSV refused
rather than read as a page. A page with no readable text is dropped before any
model call. `_missing_parser_dependency` names a missing parser once rather
than per source. A URL that failed to fetch is not retried next pass. At most
`MAX_CLAIMS_PER_PASSAGE` (3) claims per source.

**The extraction prompt is given the first `PASSAGE_CHAR_LIMIT` (5000)
characters of the page.** → later changed in two ways: `_process_source` was
split into `_fetch_source`, `_focused_window` and `_extract_claims`, and the
prompt now receives the `EXTRACTION_CHAR_LIMIT` (2500) slice most relevant to
the dimension being researched. Taking the top of a long page mostly buys
navigation and boilerplate, so this both halves the prompt and improves what
is in it.

### 4.7 `fact_check_agent.py` — `fact_check_node`

The independent verification stage, and the heart of the trust story.
**Separate** from extraction, with its own model calls — the extractor is
never asked to grade its own work.

| Tier | Meaning | Requirement |
|---|---|---|
| `VERIFIED` | Independently corroborated | ≥ `MIN_SOURCES_FOR_VERIFIED` (2) **independent domains**, and the corroborating source must actually be cited |
| `SINGLE_SOURCE` | Stated by one source only | Reported with that caveat visible |
| `UNSUPPORTED` | Not supported by the evidence | **Discarded. Never reaches the brief.** |

The rules that make this real rather than decorative, all present here:

- **A hallucinated source label is discarded** — a citation to a source that
  was not in the evidence given is dropped and the tier falls back.
- **A malformed verdict fails safe** and cannot produce a VERIFIED fact.
- **Verdicts are matched by label, not position**, so out-of-order output
  cannot attach verdict A to claim B.
- **A claim the batch omitted is re-asked on its own.**
- **Independence is by domain** — two pages on one site are one source.

Same-domain claims are batched into one call (`FACT_CHECK_BATCH_SIZE`, 5);
different domains are never batched together, because the independence
judgement depends on knowing which domain each claim came from. Context is
capped at `FACT_CHECK_CONTEXT_PASSAGES` (4) passages and
`FACT_CHECK_CONTEXT_CHARS` (1500) characters, selected by the locally defined
`_tokens` and `_relevant_window`.

**There is no restatement deduplication.** One press release syndicated across
five pages of the same site is adjudicated five times — five model calls for
one fact. → later changed: `_restatement_key` collapses them, keyed on
**domain** so the identical sentence on two different domains stays separate,
and each collapsed copy keeps its own `national_vs_city_flag` rather than
inheriting the representative's. That flag detail was a genuine correctness
risk: inheriting it could publish country-level data as city-specific.

### 4.8 `graph_writer_agent.py` — `graph_writer_node`

`async`, and persists the pass into **all three** datastores: the relational
audit trail, the vector store, and the knowledge graph.
`_graph_failure_hint` turns a driver error into an actionable message rather
than a stack trace. Idempotent — re-running a city filters what it has already
processed.

Writes are **serial**, and the vector-store write — which runs the embedding
model — happens on the event loop. → later changed to bounded concurrency
plus `asyncio.to_thread` for the embedding call, since a synchronous encode on
the loop stalls everything else in the process.

### 4.9 `coverage_evaluator.py` — `coverage_evaluator_node`

Byte-identical. If fewer than `MIN_DIMENSIONS_COVERED` (3) dimensions have
surviving facts and retries remain (`MAX_PLANNER_RETRIES`, 2), routes back to
the planner with the uncovered dimensions; otherwise proceeds to the report.
The only conditional edge in the graph.

### 4.10 `report_agent.py` — `report_node`

Byte-identical. `_fact_lines` renders each fact with evidence links and tier;
`_narrative` produces the prose framing. Grouped by dimension, with a
`## Confidence Summary` and gaps as their own section with stated reasons.

---

## 5. The LLM layer — `app/llm/`

### 5.1 `client.py` — provider-agnostic chat

Talks to **any** OpenAI-compatible `/chat/completions` endpoint, selected by
`LLM_BASE_URL` and `LLM_MODEL` alone. Mistral, Gemini, DeepSeek, Groq,
OpenRouter, OpenAI and a local Ollama are interchangeable with no code change;
the `openai` package is used purely as a protocol client. "Compatible" is not
uniform, and a shim typically **rejects** a parameter it does not implement
rather than ignoring it, so optional fields are only sent when configured.
Retries use `_is_retryable`, matching rate-limit, quota, overload, timeout and
connection markers including `429`.

**One model for every call.** `LLM_MODEL` defaults to `mistral-small-latest`;
`LLM_SMALL_MODEL` exists but defaults to `LLM_MODEL`, so unless it is set by
hand there is no role separation. One `LLM_MAX_TOKENS` (4096) ceiling applies
to all ~150 calls in a run, and `LLM_REASONING_EFFORT` defaults to empty,
meaning the parameter is not sent at all.

**This version also supports a keyless local model.** `Settings.llm_is_local`
detects a local endpoint so an Ollama server can be used with no API key. →
that property is absent from the current version and should be ported back.

**→ later changed to two profiles.** A run makes two kinds of request and they
do not want the same thing:

| Profile | Calls/run | Work | Reasoning | Max tokens |
|---|---|---|---|---|
| Judgement | ~30 | Query planning, adjudication, narrative, `/ask` | `high` | 4096 |
| Bulk | ~120 | Page → claim JSON, Graphiti entity extraction | `none` | 2048 |

Both still resolve to `mistral-small-latest` — Mistral's only **open-weight**
model (Apache 2.0, published weights) — so the split is in the thinking
budget, not the model. Bulk calls fill a fixed schema from text in front of
them; there is no judgement in them, and thinking tokens bill at the output
rate, which is where most of the token saving on a run comes from.

That change required a new helper, **`_answer_text`**, which does not exist
here and is the reason `LLM_REASONING_EFFORT` is safe to turn on: Mistral
Small at `high` returns `message.content` as a list of typed chunks rather
than a string, and every caller feeds the result to `json.loads` or into the
brief. The list does not raise — it stringifies into something that parses as
nothing, so every page reports no claims.

### 5.2 `embeddings.py` — local embeddings

**`sentence-transformers` with `all-MiniLM-L6-v2`**, loaded lazily and cached,
with `aembed_one`/`aembed_many` running encoding in a worker thread.

Embeddings are generated on-box rather than through an API for two reasons:

1. **It decouples the graph from the chat provider.** Graphiti embeds every
   node and edge it writes, and several good chat providers (DeepSeek, Groq)
   expose no embeddings endpoint — pointing Graphiti's `OpenAIEmbedder` at
   them returns 404 on every write. Embedding locally means the provider only
   has to serve chat completions.
2. **Milvus and Graphiti share one loaded model** instead of two.

**→ later changed to ONNX via `fastembed`**, same weights, different runtime.
`sentence-transformers` depends on `torch`, whose default linux/x86_64 PyPI
wheel is the CUDA build — roughly 2 GB of `nvidia-*` packages on hosts with no
GPU — and `torch` plus the model is 500–800 MB resident. `onnxruntime` holds
the same model in roughly 150–250 MB, which is the difference between fitting
a 0.5 GB container and being OOM-killed. Nothing torch offered was in use:
this pipeline encodes short passages a batch at a time and trains nothing.
Ranking is unaffected because Milvus queries with `COSINE` and the graph
reranker computes cosine directly — both scale-invariant, so normalised output
changes no ordering, and the width is identical, so an existing collection
stays valid.

### 5.3 `json_utils.py` — tolerant parsing

Byte-identical. `_strip_fences` and `_first_balanced_value` recover JSON from
prose or backticks; brackets inside strings do not truncate the span, escaped
quotes survive, a bare object is accepted where a list was requested, and
unparseable or truncated output returns the typed default rather than partial
data. Eight tests.

### 5.4 Token accounting

**Does not exist.** There is no record of how many tokens a run cost, which
means any claim about a token optimisation is an assertion rather than a
measurement. → later added as `app/llm/usage.py` (`UsageLedger`,
`ModelUsage`, `UsageSnapshot`), with the snapshot attached to each finished
job.

---

## 6. The three datastores — `app/stores/`

All four modules are effectively identical between versions. Three stores
because there are three different questions, and one store answers each badly.

### 6.1 `relational_store.py` — SQLite via SQLAlchemy

**Question: what happened, and can I audit it?**

| Table | Holds |
|---|---|
| `SourceRecord` | Every candidate URL with its crawl verdict and the reason |
| `FactAuditRecord` | Every fact with tier, evidence URLs, reasoning, national flag |
| `GapRecord` | Every gap with its stated reason |
| `ReportRecord` | The saved brief per city |

Schema created on first use. `DATABASE_URL` accepts Postgres.

### 6.2 `vector_store.py` — Milvus

**Question: what did we read that is semantically close to this?** Collection
`city_passages`, 384-dimensional, `COSINE` metric, created automatically.

`MILVUS_URI` alone selects the topology: a path ending `.db` runs **Milvus
Lite** embedded with no server; an `http://host:19530` URI or a Zilliz Cloud
endpoint runs a real cluster. No code path differs.

`_schema_problem` self-repairs a collection whose schema no longer matches — an
Int64 primary key where a string is needed, a changed embedding dimension, a
missing vector field — and fails open on an unreadable schema.

### 6.3 `graph_store.py` — Neo4j via Graphiti

**Question: what do we believe about this city, and what did we believe
before?** The only store that answers the second half.

Graphiti models **facts as edges**, and the edge carries a temporal validity
window. A superseded fact is not deleted; it gains an `invalid_at`. That is
what makes the graph a record over time rather than a snapshot.

`_build_local_clients` supplies a `LocalEmbedder` and a `LocalReranker`
(cosine over the same bi-encoder, avoiding a second model download), so **only
chat is remote** — Graphiti normally wants a key for chat, embeddings *and*
reranking.

Configuration details that were each a bug first:

- `temperature=0` set explicitly, because Graphiti defaults to 1, which is
  wrong for entity extraction — re-reading a passage should yield the same
  entities.
- `structured_output_mode="json_object"` rather than the default
  `json_schema`, because the OpenAI-compatible shims implement constrained
  decoding inconsistently. Slightly weaker adherence, but the only mode that
  works across every documented provider.
- `GRAPHITI_MAX_TOKENS` (16384) deliberately generous — Graphiti's prompts are
  long and it asks for bigger JSON than we do, and too low a ceiling shows up
  as **a graph with no edges rather than as an error**.
- `GRAPHITI_SEMAPHORE_LIMIT` exported into the environment *before*
  `graphiti_core` is imported, because the library reads it once at module
  scope.

`query_facts_for_city` does hybrid retrieval — semantic search over edge
embeddings, BM25, then a rerank of the union — and **is read at query time**
by `GET /graph/{city}` and `/ask`. That is the clause that makes the graph part
of the product rather than a write-only artifact.

Graphiti's own model is not separately configurable here; it uses the single
`LLM_MODEL`. → later given its own `GRAPHITI_MODEL` setting defaulting to the
bulk profile, since entity extraction is bulk work.

### 6.4 `lazy.py` — `LazyStore`

Byte-identical. Stores are constructed on first attribute access, not at
import, which is why importing the app loads no model and opens no connection,
and why `asyncio.to_thread(lambda: vector_store.add_passages(...))` uses a
lambda — passing the bound method would trigger construction, and the model
load, on the event loop.

---

## 7. Contracts and utilities

### 7.1 `models/schemas.py`

Byte-identical. `PlannedQuery`, `SourceCandidate`, `CrawlabilityResult`,
`ExtractedPassage`, `Claim`, `FactCheckedClaim`, `Gap`, `ResearchRequest`, the
`CrawlVerdict` and `ConfidenceTier` enums, and `dimension_label`.
`FactCheckedClaim.evidence_urls` is the single place "what is this fact's
evidence" is answered.

### 7.2 `util.py`

Two functions: **`domain_of`** (the domain, used for the independence
judgement) and **`domain_matches`** (dot-boundary matching for the source
policy in §4.3).

→ later changed: `domain_matches` disappeared with the domain policy, and
`tokens` and `relevant_window` moved here from `fact_check_agent.py` because
extraction started needing them too. `relevant_window` was found to have a
real bug when the suite was run against it — the stride never landed on the
final window, so on an 8107-character page with a 2500-character budget the
scan stopped at offset 5000 and nothing past 7500 was ever scored. Present in
this version too, inside `fact_check_agent._relevant_window`.

### 7.3 Job registry

**Does not exist.** Run state lives only in the open HTTP request. →
later added as `app/jobs.py` (`Stage`, `Job`, `JobRegistry`, `summarise_run`).

---

## 8. HTTP API — `app/main.py`

Nine routes.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness. Status and `run_mode`. Touches no store, deliberately — a healthcheck that depends on a datastore turns a degraded dependency into a failed deploy |
| POST | `/research` | Run the whole pipeline **and block** until the brief exists |
| GET | `/cities` | Cities with a saved brief |
| GET | `/report/{city}` | The brief |
| GET | `/report/{city}/download` | The brief as a file attachment |
| GET | `/sources/{city}` | Every candidate URL with crawl verdict and reason |
| GET | `/facts/{city}` | Every fact with tier, evidence and national flag, plus gaps |
| GET | `/graph/{city}` | Knowledge-graph facts; `?question=` scopes the traversal |
| POST | `/ask` | Conversational retrieval over the vector store **and** the graph |

`/ask` constrains the model to the numbered evidence it is given, requires
inline `[1]`, `[2]` citations, and instructs it to state what is missing
rather than fill the gap, and never to present national information as
city-specific. It reads two of the three stores, which satisfies the "used at
query time" requirement.

**There is no `lifespan` handler and no startup work of any kind.** The
embedding model loads on the first request that needs it.

→ later added: three job endpoints (`GET /research`, `GET /research/{job_id}`,
`DELETE /research/{job_id}`), `POST /research` changed to return `202` with a
`job_id`, and a `lifespan` with an **optional, default-off** embedding
prewarm. Worth noting for the deployment story: enabling that prewarm by
default was a mistake — it allocated the model in the first seconds of every
container start, and on a memory-capped host that is an OOM kill rather than a
catchable exception. **This version deploys cleanly on Railway's free tier
precisely because it does no startup work.** The fix was to default the
prewarm off, not to remove it.

---

## 9. Frontend — `frontend/index.html`

Single-page UI served from the same origin as the API, so no CORS in the
normal path. Submits `POST /research` and **waits for the single response**,
with no intermediate progress — the browser is subject to the same timeout as
the request itself.

→ later changed to submit a job and poll, rendering per-node stage progress,
with stage states mapped to CSS classes and a distinct `failed` state so a
failed stage shows red rather than stuck mid-animation.

---

## 10. Scripts

- **`scripts/check_services.py`** — preflight for every external dependency:
  importable packages, the LLM, Neo4j, Milvus, search, and the official-data
  APIs. **Includes `check_local_context_window`**, which probes a local
  model's context window so a too-small one is reported at preflight rather
  than showing up as truncated output mid-run. → that check is absent from the
  current version and should be ported back; the current version instead
  reports the reasoning effort it sent for each of the two profiles.
- **`scripts/generate_sample_report.py`** — byte-identical; generates the
  example brief that ships with the repo.

---

## 11. Tests — 56, offline

| File | Tests | Covers |
|---|---|---|
| `test_pipeline.py` | 46 | The non-negotiables as executable contracts, plus a regression test per fixed bug |
| `test_json_utils.py` | 8 | The tolerant parser |
| `test_thin_slice.py` | 2 | Full graph end to end in MOCK |

Named for the behaviour, not the function —
`test_crawlability_gate_blocks_extraction`,
`test_every_reportable_fact_has_provenance`,
`test_unsupported_claims_never_become_facts`,
`test_official_statistics_are_flagged_as_national_not_city`,
`test_graph_is_queryable_after_a_run`,
`test_total_outage_still_produces_an_honest_report`.

**Six tests exist only here**, covering the two features only this version
has: `test_allowlist_restricts_discovery_and_says_so`,
`test_denylist_wins_over_allowlist`,
`test_domain_matching_respects_the_dot_boundary`,
`test_no_allowlist_means_open_discovery_and_no_disclosure`,
`test_uncitable_domains_are_dropped_before_the_per_dimension_cap`, and
`test_a_local_model_server_does_not_require_an_api_key`.

**Each module sets `RUN_MODE` with `os.environ.setdefault`.** → later changed
to plain assignment, because `setdefault` silently yields to an inherited
`RUN_MODE=LIVE`, which sends the whole suite at the live internet and the real
Neo4j, where it hangs on connect for nine minutes rather than failing. A suite
that promises "no keys, no internet, no Neo4j" has to own the mode.

---

## 12. Deployment

- **`Dockerfile`** — `python:3.12-slim`, `build-essential` for wheels that
  need compiling on ARM, `HF_HOME=/opt/hf-cache` outside `/app/data` because
  that path is a runtime volume mount and would shadow the baked model, and
  the `all-MiniLM-L6-v2` weights baked in with `SentenceTransformer(...)` so
  the first request does not stall on a ~90 MB download and the container
  works without outbound Hugging Face access.

  **Port 8000 is hard-coded** in `EXPOSE`, the `HEALTHCHECK` URL and the
  `CMD`, and `CMD` is in exec-array form.

  → later changed to obey an injected `PORT` for both the server and the
  healthcheck, which requires shell form for the variable expansion, with
  `exec` so uvicorn is still PID 1 and receives `SIGTERM` directly — without
  it the platform falls back to `SIGKILL` and a volume-backed SQLite file can
  be cut off mid-write. Also changed to bake the ONNX model with an explicit
  `FASTEMBED_CACHE_PATH`.

- **No `railway.json`.** Platform settings are whatever the dashboard says. →
  later added, pinning the Dockerfile builder, the `/health` healthcheck with
  a 300 s timeout, the restart policy and one replica. Note that Railway
  ignores a Docker `HEALTHCHECK` and uses its own, so without this file the
  baked healthcheck does nothing on that platform.

- **`docker-compose.yml`** — app plus a volume at `/app/data`.
  `docker-compose.milvus.yml` adds full Milvus standalone for demonstrating
  the cluster topology. Both identical.

- **`.dockerignore`** — identical, and excludes `.env`. **This is why secrets
  must be set as platform environment variables**: the image contains no
  `.env` regardless of what `.gitignore` says, and the Dockerfile only copies
  `requirements.txt`, `app`, `frontend` and `scripts`.

- **`.gitignore`** — covers `.env`, `*Credentials*.txt` and
  `connection_logs.txt`. → later broadened to `.env.*` with an explicit
  `!.env.example` re-inclusion, so a `.env.local` cannot be committed by
  accident while the example file stays tracked.

---

## 13. Configuration reference

Every setting in this version, with its default. **34 settings**, against 42
in the current version.

| Setting | Default | Purpose |
|---|---|---|
| `RUN_MODE` | `LIVE` | `MOCK` \| `LIVE` |
| `LOG_LEVEL` | `INFO` | INFO logs every node with duration and output |
| `LLM_API_KEY` | — | Also accepts `MISTRAL_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY` |
| `LLM_MODEL` | `mistral-small-latest` | Every call in the run |
| `LLM_BASE_URL` | `https://api.mistral.ai/v1` | Any OpenAI-compatible endpoint |
| `LLM_SMALL_MODEL` | `LLM_MODEL` | Exists, but no role separation unless set by hand |
| `LLM_REASONING_EFFORT` | *(empty)* | Empty means the parameter is not sent |
| `LLM_MAX_TOKENS` | 4096 | One ceiling for all ~150 calls |
| `GRAPHITI_MAX_TOKENS` | 16384 | Too low = graph with no edges |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers id |
| `TAVILY_API_KEY` | — | Optional; `ddgs` is keyless |
| `ENABLE_OFFICIAL_DATA` | `true` | WHO + World Bank |
| `DATABASE_URL` | `sqlite:///./data/cardio4cities.db` | Postgres supported |
| `MILVUS_URI` | `./data/milvus.db` | `.db` path = Lite; URL = cluster |
| `MILVUS_TOKEN` / `MILVUS_COLLECTION` | — / `city_passages` | |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | — / `neo4j` / — | |
| `MAX_PLANNER_RETRIES` | 2 | Retry passes |
| `MIN_SOURCES_FOR_VERIFIED` | 2 | Independent domains for VERIFIED |
| `MIN_DIMENSIONS_COVERED` | 3 | Coverage bar |
| **`SOURCE_DOMAIN_DENYLIST`** | `youtube.com,youtu.be,facebook.com,…` | **Only in this version** |
| **`SOURCE_DOMAIN_ALLOWLIST`** | *(empty = open)* | **Only in this version** |
| `QUERIES_PER_DIMENSION` | 2 | |
| `MAX_SOURCES_PER_DIMENSION` | 4 | Keeps the brief balanced |
| `MAX_CLAIMS_PER_PASSAGE` | 3 | |
| `FACT_CHECK_CONTEXT_PASSAGES` | 4 | |
| `FACT_CHECK_CONTEXT_CHARS` | 1500 | |
| `FACT_CHECK_BATCH_SIZE` | 5 | Same-domain batching |
| `PASSAGE_CHAR_LIMIT` | 5000 | Stored passage size **and** extraction prompt input |
| `LLM_MAX_CONCURRENCY` | 1 | Respects a 1 RPS free tier — **also caps the HTTP pool** |
| `GRAPHITI_SEMAPHORE_LIMIT` | 1 | Read once at module scope |
| `HTTP_TIMEOUT_SECONDS` | 12 | |

Eleven settings were added later — `LLM_BULK_MODEL`,
`LLM_BULK_REASONING_EFFORT`, `LLM_BULK_MAX_TOKENS`, `GRAPHITI_MODEL`,
`EXTRACTION_CHAR_LIMIT`, `HTTP_MAX_CONCURRENCY`, `SEARCH_MAX_CONCURRENCY`,
`GRAPH_WRITE_CONCURRENCY`, `MAX_CONCURRENT_RESEARCH`, `JOB_RETENTION_SECONDS`
and `PREWARM_EMBEDDINGS`. Two were dropped with the domain policy, and
`LLM_SMALL_MODEL` stopped being a setting of its own, surviving only as an
accepted alias for `LLM_BULK_MODEL` so an existing `.env` keeps working.

`HTTP_MAX_CONCURRENCY` deserves a note, because its absence here is a real
performance bug rather than a missing feature. **`LLM_MAX_CONCURRENCY` sizes
the worker pool for three stages that are mostly not model work** —
`crawlability_agent.py:200`, `extraction_agent.py:281` and
`fact_check_agent.py:373` each open a `ThreadPoolExecutor` with
`max_workers=settings.LLM_MAX_CONCURRENCY`. Setting it to 1 to respect a free
tier's 1 request/second limit therefore also serialises every `robots.txt`
read and every page download. Two unrelated concerns — *how fast may we call
the provider* and *how many sockets may we open* — shared one number, and the
correct value for the first is the worst value for the second.

→ later split: page fetching and crawlability move to
`HTTP_MAX_CONCURRENCY` (8), search to `SEARCH_MAX_CONCURRENCY` (3), graph
writes to `GRAPH_WRITE_CONCURRENCY` (1), and `LLM_MAX_CONCURRENCY` (1) is left
governing only the actual model calls. `extraction_agent.py` shows the split
most clearly in the current version: one pool at `HTTP_MAX_CONCURRENCY` to
fetch the pages, a second at `LLM_MAX_CONCURRENCY` to extract from them.

---

## 14. The nine non-negotiables, and where each lives

All nine are satisfied in this version too. This table is the same in both
documents except for row 9.

| # | Requirement | Where |
|---|---|---|
| 1 | Live internet research | `search_agent.py` (ddgs/Tavily) + `official_data_agent.py` |
| 2 | Agentic workflow you can walk | `graph/workflow.py`, ten named nodes |
| 3 | Crawlability checked **before** crawling, verdict enforced | `crawlability_agent.py` runs before `extraction`; gate applied structurally |
| 4 | Independent fact-checking with consequences | `fact_check_agent.py`; UNSUPPORTED is discarded |
| 5 | Knowledge graph **used at query time** | `graph_store.py`, read by `/graph/{city}` and `/ask` |
| 6 | Three datastores | SQLite, Milvus, Neo4j |
| 7 | Evidence on every fact | `FactCheckedClaim.evidence_urls`; enforced by test for every tier |
| 8 | No fabrication; national data flagged | Hallucinated citations discarded; `national_vs_city_flag` on every fact |
| 9 | Deployed and reachable | `Dockerfile` + `docker-compose.yml`; **no platform config file, and the deployed brief is only reachable if the client survives a multi-minute blocking request** |

---

## 15. Known limits of this version

The honest list, and the reason the changes were made.

- **A run is a blocking HTTP request**, so behind any proxy with a request
  deadline the client times out on runs that actually succeeded. This is the
  one limit that affects a live demo directly.
- **No progress reporting.** Per-node timings are collected but only visible
  after the run finishes.
- **No duplicate-run protection and no concurrency bound**, against a
  single-writer SQLite store.
- **No token accounting**, so cost and any optimisation are unmeasurable.
- **One model and one token ceiling for every call**, so the ~120 mechanical
  extraction calls are billed the same headroom as the ~30 judgement calls.
- **No restatement deduplication**, so a syndicated press release costs one
  model call per copy.
- **The extraction prompt takes the top of the page**, which on a long page is
  largely navigation.
- **Search queries and graph writes are serial**, and the embedding call runs
  on the event loop.
- **One concurrency number governs both model calls and network fetches**, so
  the value a free-tier rate limit demands also serialises every page download
  and `robots.txt` read.
- **`sentence-transformers` pulls `torch`**, adding roughly 2 GB to the image
  and 500–800 MB resident — which fits a 1 GB host but not a 0.5 GB one.
- **Port 8000 is hard-coded** in the Dockerfile, so a platform that injects
  `PORT` needs a dashboard override.
- **Free-tier model quotas govern run time.** One city costs 150–250 model
  calls and 400–600k tokens. Mistral's free tier meters per *month* (1B
  tokens) and throttles per minute, so a run is slow but completes. Providers
  that meter per *day* — Groq at 200k tokens/day, Gemini at 20 requests/day —
  stop mid-run and wait for reset.
- **Secrets must be platform environment variables**, since `.dockerignore`
  excludes `.env` from the image by design.
- **Redeploys are not zero-downtime** with a volume attached, and replicas are
  unavailable — both embedded stores assume exactly one writer.
- **Milvus Lite has no native Windows wheel.** Use Docker, WSL2, or point
  `MILVUS_URI` at a server.
- **The Neo4j Sandbox expires**, returning a new IP and password.

---

## 16. What this version has that the current one does not

Three features, worth porting forward rather than leaving behind. The first is
described in `deck/slides.md`, so it matters for the presentation.

1. **Source-domain allowlist / denylist** — §4.3, plus `domain_matches` in
   `app/util.py` and five tests.
2. **Local-model support without an API key** — `Settings.llm_is_local` and
   `check_local_context_window` in `scripts/check_services.py`, plus one test.
3. **A `LOG_LEVEL` note** in `.env.example`.

Everything else in this document is either identical to the current version or
was changed for a reason stated inline above.
