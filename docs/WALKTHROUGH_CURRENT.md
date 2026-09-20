# CARDIO4Cities — Component Walkthrough (current version)

Presentation preparation document. Every component of the application as it
stands **after** the recent round of changes, in the order a request travels
through it.

A companion document, [`WALKTHROUGH_BASELINE.md`](WALKTHROUGH_BASELINE.md),
describes the version **before** those changes — the one currently on GitHub
and deployed. Section numbering matches between the two so they can be read
side by side. Items that are new or changed here are marked **[NEW]** or
**[CHANGED]**, with the reason, so each one can be spoken to directly.

---

## 1. What the application does

A City Lead has a stakeholder meeting about a city nobody on the team has
researched. This application takes the city name and produces an
evidence-linked intelligence brief by doing live internet research,
independently verifying what it finds, and persisting the result as a reusable
asset rather than a one-off document.

The framing that matters: **this is not a summarisation tool, it is a
verification pipeline that happens to write a brief.** Every design decision
below follows from that. A summariser's job is to compress what it was given;
this system's job is to decide what it is willing to assert, and to say
plainly what it could not establish.

Three properties define the output:

- **Every fact carries its evidence.** A claim without a retrievable source
  URL does not appear in the brief at any confidence level.
- **Absence is reported, not hidden.** A dimension with nothing verifiable
  becomes an explicit gap with a stated reason, never an empty section or a
  confident-sounding generality.
- **National data is never presented as city data.** Country-level
  statistics are usable and useful, but they are flagged as national on every
  individual fact.

### Five research dimensions

A "city" is decomposed into five dimensions. Every downstream stage is scoped
by them, which is what makes coverage measurable rather than a matter of
opinion.

| Dimension key | What it covers |
|---|---|
| `cv_burden` | Cardiovascular disease burden — prevalence, mortality, risk factors |
| `health_system` | Health system capacity — facilities, workforce, access |
| `healthcare_programmes` | Programmes and interventions running in the city |
| `policy_initiatives` | Policy and regulation relevant to CVD |
| `stakeholders` | Organisations, agencies and people who matter locally |

### Two run modes

`RUN_MODE` selects between them and nothing else in the code branches on
deployment environment.

- **`MOCK`** — no API keys, no internet, no Neo4j. Deterministic fixtures
  drive the full ten-node graph, so orchestration and trust logic are
  verifiable offline. This is what the test suite runs under.
- **`LIVE`** — real search, real model calls, real datastores.

---

## 2. Request lifecycle

**[CHANGED]** A research run is now a **background job with pollable
progress**. Previously `POST /research` held the HTTP request open for the
whole run.

```
POST /research {city}
      → 202 Accepted, {job_id}, Location: /research/{job_id}
      → run starts on the event loop behind the response

GET /research/{job_id}          poll: state + per-node stage progress
GET /research/{job_id}?wait=N   long-poll up to N seconds
      → when state == "succeeded", the body carries `result`,
        which is exactly the payload the old blocking endpoint returned

DELETE /research/{job_id}       cancel
GET  /research                  list all known jobs
```

**Why it changed.** A city takes minutes — and on a local Ollama, hours. Every hosting platform closes an
idle HTTP request long before that — Railway cuts a request after 5 minutes
with no bytes transferred and caps even a chatty one at 15. The old design
therefore produced the worst possible failure: the browser received a timeout
while the container quietly finished the work and saved the brief, so the run
looked broken and was not. Returning an id immediately decouples run duration
from any proxy deadline.

A second submit for a city already running returns the existing job instead of
starting a duplicate pass. The check and the insert happen under one lock
inside `submit`, because two clicks on *Run* would otherwise write the same
facts twice and contend for the SQLite write lock.

---

## 3. Orchestration — `app/graph/`

### 3.1 `workflow.py` — the LangGraph

Ten nodes, wired as a mostly linear chain with one conditional loop back to
the planner:

```
planner → query_gen → search → official_data → crawlability
        → extraction → fact_check → graph_writer → coverage_evaluator
        → report → END
                 ↘ (if coverage insufficient and retries remain) → planner
```

Two structural decisions worth defending:

- **The crawlability gate sits before extraction as an edge, not as a
  branch.** `crawlability` always proceeds to `extraction`; the gate is
  enforced inside the extraction node, which only ever requests URLs present
  in `allowed_urls`. A conditional edge could be reordered by a later change
  and the gate would silently stop applying. As an edge plus a filter, there
  is no code path that fetches a page body before a verdict exists.
- **`recursion_limit` is computed, not constant.** LangGraph counts every
  node execution against it, so the limit is
  `NODES_PER_PASS * (MAX_PLANNER_RETRIES + 1) + 5`. Hard-coding it meant the
  final permitted retry aborted mid-pass.

**`_timed(name, node)`** wraps every node to record per-node timing.
**[CHANGED]** it now also reports stage transitions to the job registry, which
is what makes polling show real progress instead of one opaque "running". The
wrapper's parameter must literally be named `config` — LangGraph's
`RunnableCallable` injects the run config by parameter name, and that config
is how **[NEW] `_job_of`** recovers the job handle inside a node.

**[CHANGED] `_summarise`** now also carries the LLM usage snapshot alongside
the per-node durations.

### 3.2 `state.py` — `CityResearchState`

The shared state object. Two kinds of channel, and the distinction matters:

- **Accumulating channels** use the `append_list` reducer, so each pass adds
  to what earlier passes found. Sources, passages, claims and facts
  accumulate.
- **Replacing channels** are overwritten per pass — the current dimension
  focus, the latest coverage verdict.

Getting this wrong in either direction is a real bug: replace an accumulator
and the second planner pass throws away the first pass's evidence; accumulate
a scalar and the retry logic reads stale verdicts.

---

## 4. The ten agents — `app/agents/`

Each is a plain function taking state and returning a state delta. No agent
calls another; the graph is the only thing that sequences them.

### 4.1 `planner.py` — `planner_node`

Decides which dimensions this pass will pursue. On the first pass, all five.
On a retry, only the dimensions the coverage evaluator reported as
uncovered — so a second pass is targeted rather than a repeat.

### 4.2 `query_gen.py` — `query_gen_node`

Turns each dimension into `QUERIES_PER_DIMENSION` (default 2) search queries
via the judgement model. `_fallback_queries` provides deterministic templates
when the model call fails, so a model outage degrades search quality rather
than stopping the run.

### 4.3 `search_agent.py` — `search_node`

Discovers candidate sources. Two providers:

- **`ddgs`** — keyless DuckDuckGo search, multi-backend, so one engine
  blocking does not mean zero results. This is the default and needs no
  account.
- **Tavily** — used when `TAVILY_API_KEY` is set.

Each dimension keeps at most `MAX_SOURCES_PER_DIMENSION` (default 4)
candidates, which is what keeps a five-dimension brief balanced rather than
letting one well-covered dimension consume the whole budget.

**A search provider failure is reported, not swallowed.** A silent zero-result
search is indistinguishable from "this city has no published information",
which is the single most misleading thing this system could do.

**[CHANGED]** Queries now run concurrently, bounded by
`SEARCH_MAX_CONCURRENCY` (default 3). They were serial, and search is pure
network wait.

**[NEW here, restored from the baseline]** The source-domain policy —
`SOURCE_DOMAIN_ALLOWLIST` and `SOURCE_DOMAIN_DENYLIST`, `domain_matches` in
`app/util.py`, and `_domain_permitted` here — which had been dropped in this
lineage while `deck/slides.md` still described it. Two slides document it, so
the alternative to porting it was a deck that described code nobody could
open. Five tests came back with it.

The gate runs **before** the per-dimension cap, which is the whole point: a
source the policy was never going to allow must not first consume one of the
four slots its dimension gets. Denylist is checked before allowlist, because
that is the only reading of a contradictory configuration that cannot be used
to smuggle a blocked domain in by also allowlisting it. Matching is on a dot
boundary, so `gov.in` covers `nhm.maharashtra.gov.in` and not `evilgov.in` —
with a plain `endswith`, an allowlist is a suggestion rather than a policy.

Denylist rejections are **logged, not warned**: excluding a recipe blog is the
policy working rather than the run degrading, and it does not belong in a
brief a stakeholder reads. A set allowlist is the opposite — it is **warned**,
and therefore disclosed in the report, because every gap in the brief is
worded as "not established", and narrowing the search silently changes that
from "nobody has published this" to "we only looked in these places".

### 4.4 `official_data_agent.py` — `official_data_node`

Structured indicators pulled directly from public-health REST APIs, with **no
model involved**:

- **WHO Global Health Observatory** (`ghoapi.azureedge.net`) — indicators
  such as `BP_04` (raised blood pressure), `NCDMORT3070` (premature NCD
  mortality), `NCD_BMI_30A` (obesity).
- **World Bank** — development indicators.

`_resolve_iso3` maps the country to an ISO3 code for the query.

This agent is why the system still produces something useful when the model
provider is unavailable: these facts arrive as structured data and need no
extraction. **They are always flagged `national_vs_city_flag = True`**,
because WHO and World Bank report at country level and presenting them as
city-specific would be the exact failure the brief must not commit. They are
also never promoted above `SINGLE_SOURCE`.

Disabled with `ENABLE_OFFICIAL_DATA=false`.

### 4.5 `crawlability_agent.py` — `crawlability_node`

Runs **before anything is crawled**, and its verdict actually gates
extraction. Checks in order, stopping at the first DENY:

1. **ToS-restrictive platforms** — hard denylist (LinkedIn, Facebook,
   Instagram, X/Twitter). Scraping these violates their terms regardless of
   what `robots.txt` says.
2. **`robots.txt`** disallow rules for *this specific path*.
3. **`X-Robots-Tag: noindex/nofollow`** response header, via one `HEAD`
   request.

This agent never fetches page content — only `robots.txt` and response
headers, the minimum needed to decide.

Two operational details that matter more than they look:

- **`robots.txt` is fetched with an explicit timeout.**
  `RobotFileParser.read()` has none and will hang indefinitely on a
  black-holed host, which in a live demo looks like the app freezing. So it is
  fetched with `requests` and parsed from text.
- **`_RobotsCache` caches per origin**, one fetch per origin per run.

**Absence is not prohibition, but the run records which it was.** A missing
`robots.txt` defaults to allowed, and the stored reason says so explicitly, so
an auditor can tell "explicitly permitted" from "nobody said no".

Every verdict — ALLOWED *and* DENIED, with its reason — is written to the
relational source registry and exposed at `GET /sources/{city}`.

### 4.6 `extraction_agent.py` — `extraction_node`

Fetches allowed pages and turns them into candidate claims.
**[CHANGED]** split from a single `_process_source` into three functions with
separate concerns:

- **`_fetch_source`** — retrieves the page. Content-type gating decides the
  parser: XML is parsed as XML, HTML as HTML, and CSV is refused rather than
  read as a page. A page with no readable text is dropped *before* any model
  call, which saves a request that could only return nothing.
  `_missing_parser_dependency` names a missing parser once rather than once
  per source.
- **[NEW] `_focused_window`** — selects the `EXTRACTION_CHAR_LIMIT` (2500)
  slice of the page most relevant to the dimension being researched, using
  `relevant_window` in `app/util.py`. Previously the prompt took the top of
  the page, which on a long page is navigation and boilerplate.
- **`_extract_claims`** — one bulk-profile model call per source, returning at
  most `MAX_CLAIMS_PER_PASSAGE` (3) claims as JSON.

A URL that failed to fetch is not retried on the next pass.

### 4.7 `fact_check_agent.py` — `fact_check_node`

The independent verification stage, and the heart of the trust story. It is a
**separate** stage with its own model calls — it does not ask the extractor to
grade its own work.

Each claim is adjudicated into one of three tiers:

| Tier | Meaning | Requirement |
|---|---|---|
| `VERIFIED` | Independently corroborated | ≥ `MIN_SOURCES_FOR_VERIFIED` (2) **independent domains**, and the corroborating source must actually be cited |
| `SINGLE_SOURCE` | Stated by one source only | Reported with that caveat visible |
| `UNSUPPORTED` | Not supported by the evidence | **Discarded. Never reaches the brief.** |

The rules that make this real rather than decorative:

- **A hallucinated source label is discarded.** If the model cites a
  corroborating source that was not in the evidence it was given, the citation
  is dropped and the tier falls back.
- **A malformed verdict fails safe** — it cannot produce a VERIFIED fact.
- **Verdicts are matched by label, not by position**, so a model that returns
  them out of order cannot attach verdict A to claim B.
- **A claim the batch omitted is re-asked on its own**, rather than silently
  losing it.
- **Independence is by domain.** Two pages on one site are one source.

**Batching.** Claims from the same domain are adjudicated in one call
(`FACT_CHECK_BATCH_SIZE`, default 5); claims from different domains are never
batched together, because the independence judgement depends on knowing which
domain each came from. Context is limited to
`FACT_CHECK_CONTEXT_PASSAGES` (4) passages and `FACT_CHECK_CONTEXT_CHARS`
(1500) characters.

**[NEW] `_restatement_key`** deduplicates the same sentence repeated across
one site, so one press release syndicated to five of its own pages is
adjudicated once. The key includes the **domain**, so the identical sentence
appearing on two different domains stays separate — collapsing those would
destroy the independent-domain count that `VERIFIED` depends on.

**[CHANGED] A collapsed restatement keeps its own national flag.** Previously
a collapsed copy inherited the representative claim's
`national_vs_city_flag`, which could publish country-level data as
city-specific. The flag is now re-derived per copy and never lowered. This was
a live correctness defect, not a theoretical one.

### 4.8 `graph_writer_agent.py` — `graph_writer_node`

Persists the pass into **all three** datastores: the relational audit trail,
the vector store, and the knowledge graph.

`_graph_failure_hint` turns a driver error into an actionable message rather
than a stack trace.

**[CHANGED]** graph writes now run concurrently, bounded by
`GRAPH_WRITE_CONCURRENCY`, and the vector-store write is dispatched through
`asyncio.to_thread` so encoding a batch of passages does not block the event
loop while a poll is waiting on it.

Idempotency is enforced here and in every node: re-running a city filters what
it has already processed rather than duplicating it.

### 4.9 `coverage_evaluator.py` — `coverage_evaluator_node`

Decides whether the run has enough to report. If fewer than
`MIN_DIMENSIONS_COVERED` (3) dimensions have surviving facts and retries
remain (`MAX_PLANNER_RETRIES`, default 2), it routes back to the planner with
the uncovered dimensions. Otherwise it proceeds to the report.

This is the only conditional edge in the graph.

### 4.10 `report_agent.py` — `report_node`

Writes the Markdown brief. `_fact_lines` renders each fact with its evidence
links and tier; `_narrative` produces the prose framing via the judgement
model. Output is grouped by dimension with a `## Confidence Summary`, and gaps
appear as their own section with stated reasons.

---

## 5. The LLM layer — `app/llm/`

### 5.1 `client.py` — provider-agnostic chat

Talks to **any** OpenAI-compatible `/chat/completions` endpoint, selected by
`LLM_BASE_URL` plus the model settings alone. Mistral, Gemini, DeepSeek, Groq,
OpenRouter, OpenAI and a local Ollama are interchangeable with no code change.
The `openai` package is used purely as a protocol client.

"Compatible" is not uniform, and the code accounts for that: a shim typically
**rejects** a parameter it does not implement rather than ignoring it, so
optional fields are only sent when configured.

**[NEW] Two profiles rather than one model.** A run makes two kinds of
request and they do not want the same thing:

| Profile | Calls/run | Work | Model | Reasoning | Max tokens |
|---|---|---|---|---|---|
| **Judgement** | ~30 | Query planning, claim adjudication, narrative, `/ask` | `LLM_MODEL` | `high` | 4096 |
| **Bulk** | ~120 | Page → claim JSON, Graphiti entity extraction | `LLM_BULK_MODEL` | `none` | 2048 |

**[CHANGED] Both models now default to `mistral-small-latest`** — Mistral's
only **open-weight** model (Apache 2.0, published weights), so the whole
pipeline runs on something a reader can inspect, self-host or audit. Sharing
one model does not collapse the split: it moves to the thinking budget. Bulk
calls fill a fixed schema from text in front of them; there is no judgement in
them, and thinking tokens bill at the **output** rate, so `none` there is
where most of the token saving on a run comes from.

**[NEW] `_answer_text(message)`** unwraps content that is not a string.
Mistral Small at `reasoning_effort="high"` returns a list of typed chunks — a
`thinking` chunk followed by a `text` chunk — and every caller here feeds the
result straight to `json.loads` or into the brief. The list form does not
raise; it stringifies into something that parses as nothing, so the run
reported every page as having no claims. This helper drops the reasoning and
joins the text. **`LLM_REASONING_EFFORT=high` is only safe because of it.**

Retries use `_is_retryable`, which matches on rate-limit, quota, overload,
timeout and connection markers, including `429`.

**[NEW] A local endpoint needs no API key.** `Settings.llm_is_local` in
`app/config.py` recognises `localhost`, `127.0.0.1`, `::1`, `0.0.0.0`,
`host.docker.internal` and `.local` names, and both this module and
`graph_store.py` skip the key requirement for them. Ollama, LM Studio and
llama.cpp ignore the `Authorization` header entirely — Ollama's own docs call
the `api_key` "required but ignored" — so demanding a key for them turned a
correct configuration into a startup error telling the operator to obtain a
key that does not exist. The OpenAI SDK still refuses to construct without
one, so a placeholder is passed when, and only when, `LLM_API_KEY` is empty.

A LAN address such as `192.168.1.20` is deliberately **not** local: the
relaxation is a security-relevant exception, so it is scoped to loopback and
names that cannot be a third party.

Both reasoning-effort settings also **default to empty against a local base
URL**, the same way `MILVUS_URI` alone decides Lite-or-cluster. Ollama does
implement the parameter, but LM Studio and llama.cpp do not all, and on a
local server thinking is spent in wall-clock rather than money — which is the
exact trade the judgement/bulk split was making. The model default is *not*
switched to match, because blank is a universally valid reasoning effort
whereas there is no universally valid model name; `LLM_MODEL` has to be set to
something that has actually been pulled.

See §12 of this document and `docs/DEPLOYMENT.md` §3.5.1 for the setup, and
note the one non-obvious requirement: `OLLAMA_CONTEXT_LENGTH=32768` on the
server, or Graphiti's prompts are silently truncated and the graph ends up
with no fact edges.

### 5.2 `embeddings.py` — local embeddings

**[CHANGED] ONNX via `fastembed`, previously `sentence-transformers`.** Same
`all-MiniLM-L6-v2` weights; different runtime.

Embeddings are generated on-box rather than through an API, for two reasons:

1. **It decouples the graph from the chat provider.** Graphiti embeds every
   node and edge it writes, and several good chat providers (DeepSeek, Groq)
   expose no embeddings endpoint at all — pointing Graphiti's `OpenAIEmbedder`
   at them returns 404 on every write. Embedding locally means the provider
   only has to serve chat completions.
2. **Milvus and Graphiti share one loaded model** instead of two.

**Why ONNX.** `sentence-transformers` depends on `torch`, whose default
linux/x86_64 wheel on PyPI is the CUDA build — roughly 2 GB of `nvidia-*`
packages on hosts with no GPU — and `torch` plus the model is 500–800 MB
resident. `onnxruntime` holds the same model in roughly 150–250 MB. That moves
the app from "needs ~1 GB" to "fits in 0.5 GB", which is the difference
between deploying on Railway's Free plan and being OOM-killed. Nothing torch
offered was being used: this pipeline encodes short passages a batch at a
time and trains nothing.

**Ranking is unaffected.** Milvus queries with the `COSINE` metric and the
graph reranker computes cosine directly. Both are scale-invariant, so
`fastembed` returning L2-normalised vectors where `sentence-transformers` did
not changes no ordering, and the width is identical, so an existing collection
stays valid.

`_model_name` accepts the bare `all-MiniLM-L6-v2` and expands it to the full
Hugging Face repo id that `fastembed` resolves against, so no existing `.env`
or deployed variable needed editing. `embedding_dim` probes the width with one
short encode rather than trusting a constant or a library method that has
moved between releases.

The model loads lazily on first use; `aembed_one`/`aembed_many` run encoding
in a worker thread so a graph write never blocks the event loop.

### 5.3 `json_utils.py` — tolerant parsing

Models return JSON wrapped in prose, fenced in backticks, or truncated.
`_strip_fences` and `_first_balanced_value` recover the payload; brackets
inside strings do not truncate the span, escaped quotes survive, a bare object
is accepted where a list was requested, and unparseable or truncated output
returns the typed default rather than partial data.

### 5.4 `usage.py` — **[NEW]** token accounting

`UsageLedger` records prompt and completion tokens per model on every call.
The snapshot is attached to each finished job, so the effect of a token
decision is measurable rather than asserted.

---

## 6. The three datastores — `app/stores/`

Three stores because there are three different questions, and one store
answers each badly.

### 6.1 `relational_store.py` — SQLite via SQLAlchemy

**Question: what happened, and can I audit it?** Four tables:

| Table | Holds |
|---|---|
| `SourceRecord` | Every candidate URL with its crawl verdict and the reason |
| `FactAuditRecord` | Every fact with tier, evidence URLs, reasoning, national flag |
| `GapRecord` | Every gap with its stated reason |
| `ReportRecord` | The saved brief per city |

Schema is created on first use. SQLite is a single-writer store, which is why
`MAX_CONCURRENT_RESEARCH` defaults to 2.

### 6.2 `vector_store.py` — Milvus

**Question: what did we read that is semantically close to this?** Collection
`city_passages`, 384-dimensional, `COSINE` metric, created automatically.

`MILVUS_URI` alone selects the topology — a path ending `.db`
(`./data/milvus.db`) runs **Milvus Lite** embedded with no server, which is
what the deployment uses; an `http://host:19530` URI or a Zilliz Cloud
endpoint runs a real cluster. There is no code path that differs.

`_schema_problem` self-repairs a collection whose schema no longer matches —
an Int64 primary key where a string is needed, a changed embedding dimension,
a missing vector field — and fails open on an unreadable schema.

### 6.3 `graph_store.py` — Neo4j via Graphiti

**Question: what do we believe about this city, and what did we believe
before?** This is the only store that answers the second half.

Graphiti models **facts as edges**, and the edge carries a temporal validity
window. A superseded fact is not deleted; it gains an `invalid_at`. That is
what makes the graph a record over time rather than a snapshot.

`_build_local_clients` supplies Graphiti with a `LocalEmbedder` and a
`LocalReranker` (cosine over the same bi-encoder, avoiding a second model
download), so **only chat is remote**. Graphiti normally wants an API key for
chat, embeddings *and* reranking.

Configuration details that were each a bug first:

- `temperature=0` is set explicitly because Graphiti defaults to 1, which is
  wrong for entity extraction — re-reading the same passage should yield the
  same entities.
- `structured_output_mode="json_object"` rather than Graphiti's default
  `json_schema`, because the OpenAI-compatible shims implement constrained
  decoding inconsistently. Slightly weaker adherence, but the only mode that
  works across every documented provider.
- `GRAPHITI_MAX_TOKENS` (16384) is deliberately generous: Graphiti's prompts
  are long, it asks for bigger JSON than we do, and too low a ceiling shows up
  as **a graph with no edges rather than as an error**.
- `GRAPHITI_SEMAPHORE_LIMIT` is exported into the environment *before*
  `graphiti_core` is imported, because the library reads it once at module
  scope.

`query_facts_for_city` does hybrid retrieval — semantic search over edge
embeddings, a BM25 pass, and a rerank of the union. **It is read at query
time** by `GET /graph/{city}` and by `/ask`, which is the clause that makes
the graph part of the product rather than a write-only artifact.

### 6.4 `lazy.py` — `LazyStore`

Stores are constructed on first attribute access, not at import. This is why
importing the app does not load a model or open a connection, and why
`asyncio.to_thread(lambda: vector_store.add_passages(...))` uses a lambda —
passing the bound method directly would trigger construction, and the model
load, on the event loop.

---

## 7. Contracts and utilities

### 7.1 `models/schemas.py`

Pydantic models every agent reads and writes, so a stage can be replaced
without renegotiating shapes: `PlannedQuery`, `SourceCandidate`,
`CrawlabilityResult`, `ExtractedPassage`, `Claim`, `FactCheckedClaim`, `Gap`,
`ResearchRequest`, plus the `CrawlVerdict` and `ConfidenceTier` enums and
`dimension_label` for display names. `FactCheckedClaim.evidence_urls` is the
single place "what is this fact's evidence" is answered.

### 7.2 `util.py`

- `domain_of` — the domain, used for the independence judgement.
- **[NEW] `tokens`** and **[NEW] `relevant_window`** — moved here from
  `fact_check_agent.py` because extraction now needs them too.

**[CHANGED] `relevant_window` had a real bug: it could never reach the tail of
a page.** The stride never landed on the final window, so on an
8107-character page with a 2500-character budget the scan stopped at offset
5000 and nothing past 7500 was ever scored. The last start is now evaluated
explicitly. Found by running the suite for the first time.

### 7.3 `jobs.py` — **[NEW]** the job registry

`Stage`, `Job` and `JobRegistry`. Holds queued/running/succeeded/failed/
cancelled state, per-node stage records, timings, the LLM usage snapshot and
the final result, with `JOB_RETENTION_SECONDS` (3600) retention. The brief
itself lives in the relational store permanently, so expiry costs the progress
detail and not the deliverable.

Three correctness details:

- **`submit` is atomic** — the duplicate-city check and the insert happen
  under one lock, and it returns `(job, created)` so the endpoint can say
  whether it started a run or joined one.
- **`abandon_running_stages`** marks any still-`running` stage as failed on
  both terminal paths, so a crashed run does not leave a stage spinning
  forever in the UI.
- **`summarise_run` is inside the inner `try`.** It previously sat after the
  `finally`, so if summarising raised, the job was stranded at `running` with
  no `finished_at` — never purged, and the city blocked from being researched
  again for the life of the process.

---

## 8. HTTP API — `app/main.py`

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness. Returns status and `run_mode`. Touches no store, deliberately — a healthcheck that depends on a datastore turns a degraded dependency into a failed deploy |
| POST | `/research` | **[CHANGED]** Start a job. `202` + `job_id` + `Location` header |
| GET | `/research` | **[NEW]** List known jobs |
| GET | `/research/{job_id}` | **[NEW]** Poll state, stages, result. `?wait=N` long-polls |
| DELETE | `/research/{job_id}` | **[NEW]** Cancel |
| GET | `/cities` | Cities with a saved brief |
| GET | `/report/{city}` | The brief |
| GET | `/report/{city}/download` | The brief as a file attachment |
| GET | `/sources/{city}` | Every candidate URL with crawl verdict and reason |
| GET | `/facts/{city}` | Every fact with tier, evidence and national flag, plus gaps |
| GET | `/graph/{city}` | Knowledge-graph facts; `?question=` scopes the traversal |
| POST | `/ask` | Conversational retrieval over the vector store **and** the graph |

`/ask` constrains the model to the numbered evidence it is given, requires
inline `[1]`, `[2]` citations, and instructs it to say plainly what is missing
rather than fill the gap — and never to present national information as
city-specific. It reads two of the three stores, which is what satisfies the
"used at query time" requirement.

**[NEW] `lifespan` + `_prewarm_embeddings`.** Optionally loads the embedding
model at startup in a daemon thread rather than on the first request.
**Default `false`**: turning it on allocates the model in the first seconds of
every container start, and on a memory-capped host that is an OOM kill rather
than an exception — the helper catches everything it can, but a process killed
by the kernel cannot log. Deferring costs one slow request; doing it at boot
can cost the deployment.

---

## 9. Frontend — `frontend/index.html`

Single-page UI served from the same origin as the API, so no CORS in the
normal path. **[CHANGED]** submits a job and polls, rendering per-node stage
progress, with stage states mapped to CSS classes
(`queued→idle`, `running→active`, `succeeded→done`, **[NEW]** `failed→failed`)
so a failed stage is visibly red rather than stuck mid-animation.

---

## 10. Scripts

- **`scripts/check_services.py`** — preflight for every external dependency:
  importable packages, both LLM profiles (naming the reasoning effort it
  sent, so a rejected value shows up here rather than as an empty graph three
  minutes into a run), Neo4j, Milvus, search, and the official-data APIs.

  **[NEW] `check_local_context_window`** runs when the endpoint is local. It
  reads the loaded model's real context window from Ollama's `/api/ps` and
  fails when it is under 32768. This catches the one local failure that is
  otherwise completely silent: Ollama defaults to a small window and
  *truncates* over-long prompts rather than refusing them, so Graphiti's
  entity-extraction prompts get cut off mid-instruction and the run finishes
  with a plausible brief and a knowledge graph containing no fact edges. It
  cannot be fixed from the client, because the OpenAI-compatible API has no
  way to set context size per request — hence a check rather than a setting.
  It is ordered after `check_llm` on purpose: that check makes a real
  completion, which loads the model, so `/api/ps` has something to report.
- **`scripts/generate_sample_report.py`** — generates the example brief that
  ships with the repo.

---

## 10a. Observability — `app/telemetry.py` **[NEW]**

OpenTelemetry traces and metrics, **off by default**. When off there is no
provider, no exporter and no background export thread, and `span()` yields one
shared no-op singleton rather than allocating an object at each of the few
hundred call sites a city hits — which is what keeps this compatible with a
0.5 GB container. (This module also imports nothing from `opentelemetry` on
that path, but that saves less than it sounds: `langsmith`, which arrives with
LangGraph, has already imported the API package regardless.) When on with the
packages missing, it logs once and continues disabled: telemetry is never a
reason a research run fails.

One run is one trace, rooted at `research <city>`:

```
research Springfield                     151.3 ms
  node.planner  … node.report                       (the ten nodes)
    chat <model>                                    (each call, under its node)
  node.graph_writer                      115.4 ms   (76% of the run)
```

The root span is created in `app/jobs.py`, not in the request handler. That is
forced by the job architecture: the handler returns a job id in milliseconds
while the work runs for minutes, so a trace started in the handler would end
before the run began.

| Layer | Span | Notable attributes |
|---|---|---|
| Run | `research {city}` | job id, final state, the whole `llm_usage` snapshot |
| Node | `node.{name}` | the same summary string the log line prints |
| LLM | `chat {model}` | GenAI conventions, profile (bulk/judgement), attempts |
| Embeddings | `embeddings.encode` | model id, batch size |
| Vector | `milvus.search`, `milvus.upsert` | collection, city, match count |
| Graph | `graphiti.search`, `graphiti.add_episode` | city, tier, edge count |
| Auto | FastAPI, `requests`, SQLAlchemy | standard conventions |

Four decisions worth defending:

- **Prompts and completions are not recorded by default.** They carry scraped
  page content, and a trace backend is a copy of it that nobody audits. Spans
  carry lengths and token counts instead, and a test asserts no attribute is
  long enough to be content. `OTEL_CAPTURE_CONTENT=true` opts in, under the
  `gen_ai.prompt.*` / `gen_ai.completion.*` keys that LLM-native dashboards
  read — needed for Opik, Langfuse or LangSmith to show anything in their
  span panels, and a genuine privacy decision rather than a formality.
- **One span per LLM call, not per attempt**, with each attempt as an event. A
  span per attempt is more literal and much less useful: the thing worth
  seeing is that a call took 30 seconds because it was rate-limited three
  times.
- **Thread context propagation is load-bearing, not a nicety.** Trace context
  is a contextvar; asyncio tasks inherit it and threads do not. Extraction,
  fact-checking, crawlability and search all fan out across
  `ThreadPoolExecutor`s, so without `ThreadingInstrumentor` every model call
  made inside a pool becomes its own root trace — roughly 150 orphans per
  city. Found by the test that asserts a run is exactly one trace.
- **MOCK runs are traced too**, tagged `gen_ai.system=mock`, so the trace
  shape is identical offline. That is what makes the instrumentation testable
  with no keys and no collector.

Because the exporter is plain OTLP/HTTP, an LLM-native dashboard is a
configuration change rather than a code change. **Opik** is documented in the
README: three environment variables for Comet-hosted, one for a self-hosted
instance. Two traps are handled in code rather than left to the reader — Opik
ingests traces and *not* metrics, so a traces-only endpoint disables the
metrics exporter instead of erroring against it every 60 seconds; and Opik
requires HTTP transport rather than gRPC, which is already the exporter in
`requirements.txt`. A test pins the exact trace URL against the one Opik
documents.

Metrics are a deliberately short list — token counters, a node-duration
histogram, facts by confidence tier — because everything there is derivable
from spans. They exist because those two questions get asked as aggregates
over time, which is the one thing traces are bad at.

## 11. Tests — 81, offline

`RUN_MODE` is **[CHANGED]** assigned rather than defaulted in all three test
modules. `os.environ.setdefault` silently yielded to an inherited
`RUN_MODE=LIVE`, which sent the whole suite at the live internet and the real
Neo4j, where it hung on connect for nine minutes instead of failing. A suite
that promises "no keys, no internet, no Neo4j" has to own the mode.

| File | Tests | Covers |
|---|---|---|
| `test_pipeline.py` | 47 | The non-negotiables as executable contracts, plus a regression test per fixed bug |
| `test_jobs.py` | **[NEW]** 8 | Submit returns before the run finishes; progress is per-node; duplicate city refused; cancel; failure reported; a raising summary still finishes the job |
| `test_json_utils.py` | 8 | The tolerant parser |
| `test_telemetry.py` | **[NEW]** 11 | One trace per run with every node under it; GenAI conventions; content capture off by default, populated and truncated when on; a failing node records its exception; the disabled path is inert and still re-raises; the Opik trace URL and its traces-only metrics rule |
| `test_thin_slice.py` | 2 | Full graph end to end in MOCK |

Net movement in `test_pipeline.py` against the baseline: **seven added** —
four for the deduplication and windowing work
(`test_one_sentence_repeated_by_one_site_is_adjudicated_once`,
`test_the_same_sentence_from_two_domains_is_not_collapsed`,
`test_a_collapsed_restatement_keeps_its_own_national_flag`,
`test_extraction_prompt_is_windowed_to_the_dimension_not_the_page_top`) and
three for local inference
(`test_a_local_model_server_does_not_require_an_api_key`,
`test_a_keyless_local_endpoint_builds_a_client_and_a_remote_one_does_not`,
`test_pointing_at_a_local_server_stops_sending_reasoning_effort`). The five
domain-policy tests, listed as lost in an earlier draft of this document, are
back — the policy was ported rather than dropped from the deck, for the
reasons in §16.

The second local test asserts both halves deliberately: relaxing the key
requirement is only safe if it stays relaxed for local URLs alone, so the same
test checks that a remote endpoint with no key still fails loudly at client
construction rather than as a 401 inside a node three minutes later.

Named for the behaviour, not the function — `test_crawlability_gate_blocks_extraction`,
`test_every_reportable_fact_has_provenance`,
`test_unsupported_claims_never_become_facts`,
`test_official_statistics_are_flagged_as_national_not_city`,
`test_graph_is_queryable_after_a_run`,
`test_total_outage_still_produces_an_honest_report`. Each regression test
exists because that behaviour was once wrong.

---

## 12. Deployment

- **`Dockerfile`** — `python:3.12-slim`. **[CHANGED]** obeys an injected
  `PORT` for both the server and the healthcheck (shell form is required for
  the expansion; `exec` so uvicorn is PID 1 and receives `SIGTERM` directly,
  without which the platform resorts to `SIGKILL` and a volume-backed SQLite
  file gets cut off mid-write). **[CHANGED]** bakes the ONNX embedding model
  into the image with explicit `HF_HOME` and `FASTEMBED_CACHE_PATH` outside
  `/app/data`, because that path is a runtime volume mount and would shadow
  the baked model.
- **`railway.json`** — **[NEW]** Dockerfile builder, `/health` healthcheck
  with a 300 s timeout, one replica. **[CHANGED]** no `startCommand`: it
  overrode the Dockerfile `CMD` and duplicated the `PORT` handling less
  safely, and an unexpanded `$PORT` there is an immediate crash loop.
- **`docker-compose.yml`** — app plus a volume at `/app/data`.
  `docker-compose.milvus.yml` adds full Milvus standalone for demonstrating
  the cluster topology.
- **`.dockerignore`** — excludes `.env`, `data`, tests and docs. **Note:
  this is why secrets must be set as platform environment variables** — the
  image contains no `.env` regardless of what `.gitignore` says.
- **`.gitignore`** — covers `.env`, the Neo4j Sandbox credentials file and
  `connection_logs.txt`. **[CHANGED]** broadened to `.env.*` with an explicit
  `!.env.example` re-inclusion, so a `.env.local` or `.env.production` cannot
  be committed by accident while the example file — which is itself a
  deliverable — stays tracked.

---

## 13. Configuration reference

Every setting, with its default.

| Setting | Default | Purpose |
|---|---|---|
| `RUN_MODE` | `LIVE` | `MOCK` \| `LIVE` |
| `LOG_LEVEL` | `INFO` | INFO logs every node with duration and output |
| `LLM_API_KEY` | — | Also accepts `MISTRAL_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY` |
| `LLM_BASE_URL` | `https://api.mistral.ai/v1` | Any OpenAI-compatible endpoint |
| `LLM_MODEL` | `mistral-small-latest` | Judgement profile **[CHANGED]** |
| `LLM_BULK_MODEL` | `mistral-small-latest` | Bulk profile **[NEW]** (`LLM_SMALL_MODEL` honoured) |
| `LLM_REASONING_EFFORT` | `high`, or **empty if local** | **[CHANGED]** Safe only via `_answer_text` |
| `LLM_BULK_REASONING_EFFORT` | `none`, or **empty if local** | **[NEW]** Main token saving |
| `LLM_MAX_TOKENS` | 4096 | Bounds thinking too |
| `LLM_BULK_MAX_TOKENS` | 2048 | **[NEW]** |
| `GRAPHITI_MAX_TOKENS` | 16384 | Too low = graph with no edges |
| `GRAPHITI_MODEL` | `LLM_BULK_MODEL` | **[NEW]** |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Short or full repo id |
| `TAVILY_API_KEY` | — | Optional; `ddgs` is keyless |
| `ENABLE_OFFICIAL_DATA` | `true` | WHO + World Bank |
| `DATABASE_URL` | `sqlite:///./data/cardio4cities.db` | Postgres supported |
| `MILVUS_URI` | `./data/milvus.db` | `.db` path = Lite; URL = cluster |
| `MILVUS_TOKEN` / `MILVUS_COLLECTION` | — / `city_passages` | |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | — / `neo4j` / — | |
| `MAX_PLANNER_RETRIES` | 2 | Retry passes |
| `MIN_SOURCES_FOR_VERIFIED` | 2 | Independent domains for VERIFIED |
| `MIN_DIMENSIONS_COVERED` | 3 | Coverage bar |
| `QUERIES_PER_DIMENSION` | 2 | |
| `MAX_SOURCES_PER_DIMENSION` | 4 | Keeps the brief balanced |
| `MAX_CLAIMS_PER_PASSAGE` | 3 | |
| `FACT_CHECK_CONTEXT_PASSAGES` | 4 | |
| `FACT_CHECK_CONTEXT_CHARS` | 1500 | |
| `FACT_CHECK_BATCH_SIZE` | 5 | Same-domain batching |
| `PASSAGE_CHAR_LIMIT` | 5000 | Stored passage size |
| `EXTRACTION_CHAR_LIMIT` | 2500 | **[NEW]** Windowed prompt input |
| `LLM_MAX_CONCURRENCY` | 1 | Respects a 1 RPS free tier |
| `HTTP_MAX_CONCURRENCY` | 8 | **[NEW]** Decoupled from the LLM limit |
| `SEARCH_MAX_CONCURRENCY` | 3 | **[NEW]** |
| `GRAPH_WRITE_CONCURRENCY` | 1 | **[NEW]** |
| `GRAPHITI_SEMAPHORE_LIMIT` | 1 | Read once at module scope |
| `HTTP_TIMEOUT_SECONDS` | 12 | |
| `MAX_CONCURRENT_RESEARCH` | 2 | **[NEW]** SQLite single-writer |
| `JOB_RETENTION_SECONDS` | 3600 | **[NEW]** |
| `PREWARM_EMBEDDINGS` | `false` | **[NEW]** See §8 |

---

## 14. The nine non-negotiables, and where each lives

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
| 9 | Deployed and reachable | `Dockerfile` + `railway.json`, one container and one volume |

---

## 15. Known limits

Worth stating before being asked.

- **Free-tier model quotas govern run time.** One city costs 150–250 model
  calls and 400–600k tokens. Mistral's free tier meters per *month* (1B
  tokens) and throttles per minute, so a run is slow but completes. Providers
  that meter per *day* — Groq at 200k tokens/day, Gemini at 20 requests/day —
  stop mid-run and wait for reset. That distinction, not model quality, drove
  the provider choice. **A local Ollama removes the limit entirely**, trading
  quota for wall-clock: hours on CPU, tens of minutes on a GPU, and a weaker
  brief from a laptop-sized model.
- **The Ollama path has not been executed end to end here.** The code path is
  tested offline and the configuration resolves correctly with no key, but
  Ollama is not installed on this machine, so no city has actually been
  researched through it.
- **Secrets must be platform environment variables.** `.dockerignore`
  excludes `.env` from the image by design.
- **Memory.** The embedding model is the largest resident cost; ONNX brings it
  to roughly 150–250 MB, which fits a 0.5 GB container. This is an estimate
  and has not been measured in the container.
- **Redeploys are not zero-downtime** with a volume attached, and replicas are
  unavailable — both embedded stores assume exactly one writer.
- **Milvus Lite has no native Windows wheel.** Use Docker, WSL2, or point
  `MILVUS_URI` at a server.
- **The Neo4j Sandbox expires**, returning a new IP and password.
- **The test suite has not been run since the ONNX change.** The Dockerfile
  build is currently the first real exercise of `fastembed`.

---

## 16. What the baseline had that this version was missing — now one item

All three gaps were features that existed only in the other lineage rather
than improvements that were reverted. Two mattered because `deck/slides.md`
described them as present, which would have meant presenting slides against
code nobody could open. Both are now ported.

1. ~~**Source-domain allowlist / denylist.**~~ **Ported.**
   `SOURCE_DOMAIN_ALLOWLIST` and `SOURCE_DOMAIN_DENYLIST`, `domain_matches` in
   `app/util.py`, and `_domain_permitted` in `search_agent.py` — dropping
   uncitable domains before the per-dimension cap, denylist winning over
   allowlist, dot-boundary matching, rejections logged rather than warned, and
   a disclosure line in the brief when an allowlist narrowed discovery. Five
   tests. `search_agent.py` also gained the module `logger` it had been
   missing, which is what the rejection count is reported through. See §4.3.
2. ~~**Local-model support without an API key.**~~ **Ported, and extended.**
   `Settings.llm_is_local` and `check_local_context_window` are present again;
   on top of the baseline's version, both reasoning-effort settings now
   default to empty against a local endpoint, `graph_store.py` and
   `check_services.py` no longer demand a key either, the OpenAI client is
   given a placeholder so it will construct, and `check_services` names the
   `ollama pull` command for a model that has not been pulled. Three tests.
3. **A `LOG_LEVEL` note** in `.env.example` — still only in the baseline, and
   the one remaining difference. It is a comment, not behaviour.

With those two ported, this version is a superset: everything in §2 through
§12 above exists only here, and the deck now describes code that is actually
in this repository.
