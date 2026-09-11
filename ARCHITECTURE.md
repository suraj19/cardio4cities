# Architecture — orchestration, agents, data, and trust

The README is the operator's guide. This document is the design record: who
invokes what, what each node reads and writes, where information lives and why,
and what was deliberately left out. [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
covers the runtime view — every dependent service and how to deploy it.

## The problem, as understood

A City Lead is about to meet health officials in a city nobody on the team has
researched. They need a briefing that is *honest* more than it is *complete* —
a confident paragraph of invented context is worse than a blank section, because
it will be repeated in a meeting. So the system is built around a single
governing rule: **nothing reaches the brief without a source, and anything we
could not establish is stated as unknown.**

Everything below follows from that.

## Request entry points

`app/main.py` is the only caller of the graph. Everything else is reached
through it, and the read endpoints exist so provenance survives the run that
produced it.

| Endpoint | Invokes | Notes |
|---|---|---|
| `POST /research` | `workflow.ainvoke(...)` | Runs all nine nodes. Single blocking call. Returns the brief plus per-tier counts, coverage and any degradation warnings. |
| `POST /ask` | `vector_store.query` + `graph_store.query_facts_for_city` | Conversational retrieval over two stores, answered with inline citations, refused when the evidence does not support an answer. |
| `GET /graph/{city}` | `graph_store.query_facts_for_city` | Reads Neo4j through Graphiti's hybrid search, with temporal validity per edge. |
| `GET /report/{city}` · `/download` | `relational_store.get_report` | The stored brief as JSON, or as a downloadable `.md`. |
| `GET /sources/{city}` | `relational_store.get_sources` | Every URL considered and the gate's verdict — including refusals. |
| `GET /facts/{city}` | `relational_store.get_facts` | The audit trail: tier, reasoning, origin URL, corroborators. |
| `GET /cities` | `relational_store.list_cities` | What is already in the asset. |
| `GET /health` | `settings.RUN_MODE` | Liveness + which mode the process booted in. |

## Orchestration

`app/graph/workflow.py` compiles the graph once at import; `workflow` is reused
across requests. Nodes never call each other — every node receives the shared
state, returns a partial update, and LangGraph merges it.

```mermaid
flowchart LR
  planner --> query_gen --> search --> crawlability --> extraction
  extraction --> fact_check --> graph_writer --> coverage{coverage_evaluator}
  coverage -- "coverage sufficient" --> report --> done([END])
  coverage -- "insufficient — re-plan uncovered dimensions,<br/>bounded by MAX_PLANNER_RETRIES" --> planner
```

Only one edge is conditional, and that is a deliberate choice rather than an
unfinished one. Branching is a claim that two paths are genuinely different
work. Everywhere else in this pipeline the variation is in what a node *finds*,
not in what happens next — the crawlability gate does not need a branch, because
`extraction_node` enforces it by filtering to ALLOWED URLs before issuing a
single request, which is a stronger guarantee than a routing rule that a later
edit could bypass. The one place the path really does differ is coverage: either
the brief is good enough to write, or the planner gets another attempt.

## Decomposition: what "understanding a city" means here

Research is split into five fixed dimensions (`app/models/schemas.py`):

| Dimension | What it covers |
|---|---|
| `cv_burden` | Prevalence of hypertension, diabetes, dyslipidaemia; mortality; screening coverage |
| `healthcare_programmes` | Programmes and interventions running in the city, who runs them, since when |
| `policy_initiatives` | Municipal strategies, NCD action plans, salt/tobacco regulation, budget commitments |
| `stakeholders` | Health department, ministries, hospitals, universities, NGOs, funders and their roles |
| `health_system` | Primary care capacity, workforce, medicines availability, financing, referral pathways |

Two decisions worth defending:

- **The set is fixed, not LLM-generated per city.** A generated set would fit
  each city better and make no two cities comparable. Fixing it is what lets the
  coverage evaluator say *"we know nothing about policy in this city"* — a
  sentence that requires knowing policy was in scope.
- **"Opportunities and risks" is not a dimension.** It is the judgement the City
  Lead is paid to make. The honest machine contribution to it is the gap log —
  what we could not establish — not synthesised commentary.

The dimension *brief* is prompt text, not documentation: it is what the query
generator and claim extractor are told the dimension means, so editing it
changes system behaviour.

## The nine nodes

| Node | Task | Calls LLM | Writes to state |
|---|---|:---:|---|
| `planner` | Puts all five dimensions in scope on the first pass. On a retry, narrows `focus_dimensions` to the dimensions that came back empty, so pass two spends its whole budget on the shortfall. | — | `dimensions`, `focus_dimensions`, `retry_count` |
| `query_gen` | One call covers every in-scope dimension, returning queries per dimension biased toward government / WHO / NGO primary sources. Queries already tried are excluded; keyword templates fill in for any dimension the model leaves empty, because a dimension with no queries is a guaranteed gap. | yes | `planned_queries` |
| `search` | Discovery only — no page bodies fetched. Tavily → duckduckgo-search → canned mock. Drops URLs seen in an earlier pass and caps candidates **per dimension**, so an abundant dimension cannot crowd out a sparse one. | — | `candidates` |
| `crawlability` | The compliance gate, before anything is crawled. Hard ToS denylist (LinkedIn, Facebook, Instagram, X) → `robots.txt` for the specific path → `X-Robots-Tag` header. robots.txt is fetched with an explicit timeout and cached per origin; checks run on a thread pool. | — | `crawl_results` |
| `extraction` | Filters to ALLOWED URLs *before* any request, fetches, rejects non-HTML and `meta robots noindex` pages, then asks for atomic claims tagged `is_city_level`. Skips URLs already extracted. Fetch + extract run on a thread pool. A failed fetch becomes a warning, never a fact. | yes | `passages`, `claims`, `warnings` |
| `fact_check` | Adjudicates each claim against passages from *other* domains only, never told which passage produced it. Must **name** the sources it relies on; VERIFIED is then re-derived from that evidence. Malformed output fails safe to UNSUPPORTED. | yes | `fact_checked` |
| `graph_writer` | The only node that touches persistence. UNSUPPORTED claims become `Gap`s and never reach any store as facts. Relational write is required; vector and graph writes degrade to warnings. | — | `gaps`, `warnings`, `persisted_claim_ids`, `indexed_urls` |
| `coverage_evaluator` | Counts **dimensions** with at least one usable fact, not facts in total. Below `MIN_DIMENSIONS_COVERED` it routes back to the planner naming the shortfall; at the retry ceiling it terminates and logs one gap per uncovered dimension. | — | `coverage_sufficient`, `covered_dimensions`, `uncovered_dimensions`, `gaps` |
| `report` | Groups facts by dimension with the tier travelling on each fact, sources on every fact at every tier, gaps and refused sources stated explicitly. Persists the brief and the final gap list. | yes | `report_markdown` |

### How trust is actually enforced

Three mechanisms, in the order a claim meets them:

1. **Structural independence.** The fact-checker receives the claim text and
   passages from other domains, labelled `[S1]`, `[S2]`. It is not told which
   passage produced the claim or what the extractor concluded, so it cannot
   rubber-stamp — it has to find support in material the extractor did not use.
   Same-domain passages are excluded: a site agreeing with itself is not
   corroboration.
2. **Cited provenance, not asserted provenance.** The checker must name the
   labels it relied on. Those labels are mapped back to real URLs, and a label
   that was never offered is discarded. `corroborating_urls` therefore contains
   only sources the checker actually cited. *(This was the single worst bug in
   the earlier version: the field was filled with every other-domain URL in the
   run, which made the report's "Sources:" line present and meaningless.)*
3. **Derived tiers.** VERIFIED is recomputed from the count of distinct
   supporting domains. A claim cannot reach VERIFIED without
   `MIN_SOURCES_FOR_VERIFIED` independent sources no matter what tier the model
   asked for, and the automatic downgrade is written into the fact's reasoning
   so the reader can see it happened.

`UNSUPPORTED` then has a consequence rather than a label: `graph_writer_agent.py`
filters those claims out of `persistable` and rewrites them as gaps, so a claim
the fact-checker could not corroborate cannot physically reach a store as a fact.

## Memory

Three tiers with very different lifetimes.

### Tier 1 — working memory: one HTTP request

`CityResearchState` (`app/graph/state.py`) is a `TypedDict` LangGraph merges
partial updates into. Fields marked `Annotated[..., append_list]` **accumulate
across retry passes** — the evidence base only grows. Everything else is
last-write-wins and describes the current pass.

| State field | Merge | Written by |
|---|---|---|
| `city`, `country` | set once | `main.py` |
| `dimensions`, `focus_dimensions` | overwrite | `planner` |
| `planned_queries` | overwrite | `query_gen` |
| `candidates` | **append** | `search` |
| `crawl_results` | **append** | `crawlability` |
| `passages`, `claims` | **append** | `extraction` |
| `fact_checked` | **append** | `fact_check` |
| `gaps` | **append** | `graph_writer`, `coverage_evaluator` |
| `warnings` | **append** | `extraction`, `graph_writer` |
| `persisted_claim_ids`, `indexed_urls` | **append** | `graph_writer` |
| `retry_count`, `coverage_sufficient`, `covered_dimensions`, `uncovered_dimensions` | overwrite | `planner`, `coverage_evaluator` |
| `report_markdown` | overwrite | `report` |

Because the retry loop re-enters nodes over accumulating state, **every node is
idempotent with respect to a retry**: each filters to the items it has not
already handled (`already_checked`, `already_extracted`, `persisted_claim_ids`).
Without that, pass two re-fetches pass one's URLs, re-adjudicates its claims and
writes duplicate audit rows and graph episodes.

`build_workflow()` ends with a bare `graph.compile()` — **no checkpointer**.
There is no thread id and no resumable run. The state object is garbage once the
response is serialized; the only thing that outlives a request is what
`graph_writer` and `report` persisted.

### Tier 2 — long-term memory: three stores

Three stores because there are three genuinely different questions, and a single
store answers one of them well and the others badly.

**SQLite — governance** (`stores/relational_store.py`)
*"Was this sourced legitimately, when, by what decision, and why?"* Exact,
row-level, auditable questions about individual records.

| Table | Contents | Write mode |
|---|---|---|
| `sources` | Every crawl verdict, **allowed and denied**, with reason, dimension, timestamp | `merge` on URL |
| `fact_audit` | Tier, reasoning, origin `source_url`, corroborators, `national_vs_city_flag`, and a `reviewed_by_human` column reserved for a review queue | append |
| `gaps` | Description + reason per gap | append |
| `reports` | The generated brief per city | `merge` on city |

It is the only store that keeps **denied** sources. Recording what we chose not
to read is part of the evidence trail: a reviewer can see the gate ran and what
it cost, instead of trusting that it did.

**Milvus — semantic recall** (`stores/vector_store.py`)
*"What is being done about X?"* One row per ALLOWED passage — city, dimension,
url, title, text, and a local `all-MiniLM-L6-v2` embedding, COSINE metric,
primary key = SHA-256 of the URL so re-researching upserts instead of
duplicating. Raw passages live here, never adjudicated facts: this store is for
recall over material the schema never anticipated, and fact status is not a
property you want resolved by nearest-neighbour distance. Queries are filtered
to the city so one city's passages can never be cited as evidence about another.

**Neo4j / Graphiti — institutional memory** (`stores/graph_store.py`)
*"How do these things relate, and what changed?"* Each fact-checked claim becomes
a Graphiti `add_episode`, with tier, dimension and source URLs written into the
episode body so provenance survives entity extraction. Graphiti extracts
entities and relationships and gives temporal edges: a superseded fact gains an
`invalid_at` rather than being overwritten, which is what lets this store answer
*"what did we believe about this city six months ago"*.

Read at query time by `GET /graph/{city}` and by `POST /ask`, through Graphiti's
hybrid search — semantic search over edge embeddings plus BM25, reranked. It
returns **edges, not nodes**, because in Graphiti the edge *is* the fact
("X runs programme Y") and the edge carries the validity window.

Graphiti normally wants an OpenAI key for chat, embeddings and reranking. Here
only chat is remote: `app/llm/embeddings.py` provides a local `EmbedderClient`
and a cosine-similarity `CrossEncoderClient` off the same sentence-transformers
model. That decouples the graph from the chat provider entirely — providers with
no `/embeddings` route work fine, and there is no second API key to manage.

### Tier 3 — LLM memory: none, deliberately

`llm_client.complete(system, user)` builds a fresh single-turn message array
every call. No conversation history is threaded between nodes or between claims.
That statelessness is what makes the fact-checker's independence real rather
than nominal — it cannot remember having just extracted the claim it is judging.

## Cost and latency budget

Unbounded, a five-dimension run fans out to roughly 50 candidate URLs, 50
extraction calls, and 100 fact-check calls each carrying every other passage as
context: minutes of latency and a context window spent on the first city. The
caps in `app/config.py` are the main answer to "what did you cut":

| Knob | Default | Bounds |
|---|---|---|
| `QUERIES_PER_DIMENSION` | 2 | searches per dimension per pass |
| `MAX_SOURCES_PER_DIMENSION` | 4 | candidate URLs kept per dimension |
| `MAX_CLAIMS_PER_PASSAGE` | 3 | claims extracted per source |
| `FACT_CHECK_CONTEXT_PASSAGES` | 4 | rival passages shown per claim |
| `FACT_CHECK_CONTEXT_CHARS` | 1500 | characters of each rival passage |
| `PASSAGE_CHAR_LIMIT` | 5000 | characters kept per fetched page |
| `LLM_MAX_CONCURRENCY` | 8 | parallel extraction / fact-check calls |
| `MIN_DIMENSIONS_COVERED` | 3 | dimensions needed before the brief is written |
| `MAX_PLANNER_RETRIES` | 2 | re-plan passes |

Selecting the fact-check context by lexical overlap rather than showing
everything is not only cheaper — it keeps the model's attention on material that
could actually settle the question.

## Datastore configuration

`RUN_MODE` is a single switch across four subsystems; they cannot currently be
selected independently.

| Subsystem | `MOCK` | `LIVE` |
|---|---|---|
| Relational | SQLite via `DATABASE_URL` | SQLite via `DATABASE_URL` (unchanged) |
| Vector | in-memory keyword-overlap stand-in | Milvus (Lite embedded, or a cluster) |
| Graph | in-memory adjacency lists | Neo4j Sandbox via Graphiti |
| LLM | canned deterministic responses | any OpenAI-compatible chat endpoint |
| Search | canned candidate URLs | Tavily, or duckduckgo-search without a key |
| Embeddings | not used | local sentence-transformers |

**SQLite needs no configuration** and never branches on run mode. Swapping in
Postgres is a `DATABASE_URL` change with no code edits.

**Milvus requires `RUN_MODE=LIVE`**, because the backend is chosen on first use.
Topology, though, is independent of run mode: `MILVUS_URI` ending in `.db` runs
Milvus Lite embedded, while an `http://` or Zilliz endpoint targets a cluster.

All three stores share one lazy proxy (`app/stores/lazy.py`) that defers
construction to the first attribute access, and the LLM client does the same
internally. An unreachable dependency therefore degrades one feature rather
than stopping `app.main` from importing — which would take `/health` down with
it and leave the container restarting.

For how each of those dependencies is actually provisioned, see
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Behaviour when the outside world is unavailable

Every external dependency has a defined degraded mode, and the rule behind all
of them is the same: **a dependency that fails costs the feature it serves, and
is disclosed on the run.** Nothing that fails is allowed to become a fabricated
fact, and nothing that fails silently.

| What fails | What happens | Where |
|---|---|---|
| One search query (rate limit, provider outage) | Returns no candidates for that query. The dimension it served becomes a gap if nothing else covers it | `search_agent._run_query` |
| Search entirely | Zero candidates, so the retry loop runs to exhaustion and every dimension is reported as a gap attributed to *"Search returned no candidate sources"* | `coverage_evaluator` |
| `robots.txt` unreachable | ALLOWED, because absence of a robots.txt is not a prohibition — but the reason string records that it was *"nobody said no"* rather than an explicit permission | `crawlability_agent._check_robots_txt` |
| `X-Robots-Tag` HEAD request fails | Inconclusive; the robots.txt verdict stands | `crawlability_agent._check_noindex_header` |
| One page fetch | Source dropped with a recorded warning. A failed fetch is not a fabricated fact | `extraction_agent._process_source` |
| Claim extraction call | Passage is still indexed for semantic search, but yields no claims, plus a warning | `extraction_agent._process_source` |
| Fact-check adjudication | Claim is forced to UNSUPPORTED — dropped rather than trusted — and therefore becomes a gap | `fact_check_agent._adjudicate` |
| Query planning call | Falls back to keyword templates for every dimension, plus a warning. Generic searches beat no searches, and beat a 500 | `query_gen._plan_with_model` |
| Narrative call | Deterministic factual summary instead of prose; the facts below are the substance | `report_agent._narrative` |
| Milvus | Passages are not indexed; a warning is recorded. Costs `/ask` recall only | `graph_writer_agent` |
| Neo4j (e.g. expired Sandbox) | Facts still land in the relational audit trail; graph exploration is unavailable and `/graph/{city}` returns 503 with that hint | `graph_writer_agent`, `main.py` |
| SQLite | **Propagates.** Losing the audit trail means losing the evidence guarantee, so this one is deliberately fatal | `graph_writer_agent` |

The composite case is a total blackout — no search *and* no reachable LLM,
which is the realistic one since the model is hosted too. That path is covered
by `test_total_outage_still_produces_an_honest_report`, which asserts the run
finishes with an all-gaps brief, spends its full retry budget, invents nothing,
and discloses the degradation in a **Run Warnings** section.

There is deliberately **no automatic fallback to `RUN_MODE=MOCK`**. Serving
canned data that looks like research would be a worse failure than an honest
empty report.

## How this is evaluated

Three layers, of which two exist.

**In-loop, at runtime.** The coverage evaluator grades each pass on dimensions
covered rather than facts found, and the fact-checker grades each claim on
independently cited corroboration. Both have teeth: the first drives the retry
edge, the second decides whether a claim can be reported at all.

**Offline, deterministic.** `tests/test_pipeline.py` runs the full nine-node
graph in MOCK mode with no internet, keys or Neo4j, asserting the
non-negotiables as contracts — the gate excludes denied URLs from extraction,
every reportable fact's source URL appears in the report body, UNSUPPORTED
becomes exactly one gap and never a fact, and claim IDs stay unique across
retries.

What makes the suite worth more than a smoke test is that the mock is
adversarial rather than cooperative. The mock adjudicator returns
`SINGLE_SOURCE` with an empty `supporting_sources` list, so the tier-derivation
rule is exercised instead of bypassed by a mock that hands back VERIFIED for
free. Tests then inject hostile adjudicator output directly — including a
`supporting_sources` label that was never offered — to prove a hallucinated
citation cannot become provenance.

**What does not exist.** No golden dataset of cities with reference facts, so
no precision/recall on extraction and no accuracy on tier assignment. No judge
or human rating of narrative faithfulness. No regression scoring across model
swaps — which is the sharp edge, because changing provider is currently
validated only by "the tests still pass", and they would pass even if fact
quality collapsed, since MOCK mode never calls the real model. Cost and latency
are bounded by config but never measured.

So the suite proves the machinery **cannot fabricate**. It does not prove the
facts it surfaces are the **right** facts. The first property is what the
non-negotiables gate on, which is why it was built first; the second is the
obvious next investment, and the cheapest useful version is a small golden set
(a few cities × five dimensions) scored on source precision and dimension
recall.

## Trade-offs and what was cut

**Cut deliberately:**

- **No human review queue.** The brief flags what needs review (every
  SINGLE_SOURCE and CONFLICTING fact, plus the `national_vs_city_flag`) and
  `fact_audit.reviewed_by_human` is the column a queue would write to, but
  nothing blocks on a human today. For a briefing tool where the reader is the
  domain expert, surfacing confidence beats gating on a reviewer who is the same
  person.
- **No streaming progress.** `POST /research` blocks and the UI moves its stage
  indicators as a group. Per-node streaming is a nicer demo and zero extra
  trust; it was not worth the SSE plumbing inside the timebox.
- **No PDF or non-HTML extraction.** Government portals publish a lot of PDFs
  and the extractor rejects them with a recorded warning rather than feeding
  their bytes to an LLM and getting confident nonsense. This is the single
  biggest recall limitation.
- **Named entities are Graphiti's job, not a dedicated NER pass.** A richer
  schema would extract `Organization`/`Programme`/`Policy` nodes explicitly with
  typed relationships. Episodes let Graphiti do that extraction, at the cost of
  less control over the resulting shape.
- **One embedding model, no dedicated cross-encoder.** Reranking is cosine
  similarity over the same bi-encoder. A real cross-encoder ranks better; it is
  a second model download for marginal gain at this corpus size.
- **No freshness policy.** `sources.fetched_at` and `reports.generated_at`
  record when evidence was gathered, and `/cities` surfaces the latter, but
  nothing acts on age. Re-running a city overwrites the brief; there is no
  staleness threshold that triggers a refresh.

- **Evaluation covers process, not output quality.** There is no golden
  dataset, so there is no precision/recall figure for extraction and no
  accuracy figure for tier assignment. See "How this is evaluated" above for
  what that does and does not buy.

**Known limitations:**

- Coverage is judged by *presence* of a usable fact per dimension, not by
  depth. Three thin facts about policy count the same as thirty.
- `duckduckgo-search` without a Tavily key returns noticeably noisier results,
  which shows up as more UNSUPPORTED verdicts rather than as wrong facts.
- Milvus Lite is Linux/macOS only, so Windows development needs Docker or a
  remote endpoint.
- A Neo4j Sandbox expires after 3 days (extendable to 10) and returns with a new
  IP and password, so a long-lived deployment needs Aura instead.
