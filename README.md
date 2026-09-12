# CARDIO4Cities — City Intelligence

Research any city on the public internet at request time, verify what comes
back, and turn it into a reusable, evidence-linked briefing for a City Lead
about to meet government stakeholders.

> Name a city → plan five research dimensions → search → crawlability gate →
> extract → independently fact-check → persist to three datastores → generate a
> cited brief, with everything unverifiable stated as a gap.

The governing design rule is that **nothing reaches the brief without a source,
and anything we could not establish is said out loud.** A confident paragraph of
invented context is worse than a blank section, because it gets repeated in a
meeting.

- [ARCHITECTURE.md](ARCHITECTURE.md) — design record: nodes, memory, stores,
  trade-offs, and what was cut
- [docs/PRESENTATION.md](docs/PRESENTATION.md) — the 7-slide deck
- [examples/](examples/) — a generated city brief

## What it does

| Capability | How |
|---|---|
| Research a previously unseen city | Live search + fetch at request time; no pre-seeded city data |
| Organize findings | Five fixed dimensions: burden, programmes, policy, stakeholders, health system |
| Preserve evidence | Every fact carries its origin URL and any cited corroborators, at every confidence tier |
| Explore findings | Report by dimension, full source registry, knowledge-graph traversal |
| Answer questions | `POST /ask` answers only from indexed passages and graph facts, with inline citations |
| Surface uncertainty | Four confidence tiers, a gap log per dimension, and a record of sources refused |
| Downloadable report | `GET /report/{city}/download` |

## Two run modes

| Mode | What it does | Requires |
|---|---|---|
| `MOCK` | Canned search results and a stub LLM, so the **full graph execution** runs with no keys and no internet | Nothing |
| `LIVE` (default) | Real search, real page fetches, real LLM calls, real Neo4j/Graphiti | `LLM_API_KEY` + a Neo4j Sandbox in `.env` |

`LIVE` is the default because pre-seeded data does not qualify as research.

## Setup

```bash
cd cardio4cities
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Run the offline test suite — no keys, no internet, no Neo4j:

```bash
RUN_MODE=MOCK pytest -v
```

It runs the full nine-node graph and asserts the non-negotiables as contracts,
including that a total loss of internet still yields an honest all-gaps brief
rather than an error. What it does and does not prove is set out in
[ARCHITECTURE.md](ARCHITECTURE.md#how-this-is-evaluated), alongside the
[degraded mode for every external dependency](ARCHITECTURE.md#behaviour-when-the-outside-world-is-unavailable).

Run the app:

```bash
uvicorn app.main:app --reload
```

Open <http://localhost:8000> for the UI, or call it directly:

```bash
curl -X POST http://localhost:8000/research \
  -H "Content-Type: application/json" \
  -d '{"city": "Pune", "country": "India"}'
```

## Switching to LIVE mode

1. Set `RUN_MODE=LIVE` in `.env` (this is the default).
2. Add `LLM_API_KEY`, plus the `LLM_BASE_URL` / `LLM_MODEL` pair for your
   provider (see the table below).
3. Add `TAVILY_API_KEY`. Optional — search falls back to `ddgs`
   with no key, but results are noticeably noisier.
4. Create a **Neo4j Sandbox** at [sandbox.neo4j.com](https://sandbox.neo4j.com),
   open its *Connection details* tab, and copy the Bolt URL and password into
   `NEO4J_URI` / `NEO4J_PASSWORD`.
5. Leave `MILVUS_URI` as `./data/milvus.db` for embedded Milvus Lite, or point
   it at a Milvus cluster or Zilliz Cloud endpoint.

Then verify all three services answer *before* you rely on them:

```bash
python -m scripts.check_services
```

That makes one tiny LLM call, counts nodes in Neo4j and lists Milvus
collections, so a bad key, a retired model ID or an expired Sandbox shows up as
a labelled failure rather than an error in the middle of a run.

Milvus Lite is Linux and macOS only. On Windows, either run the app in Docker or
set `MILVUS_URI` to a real endpoint (Zilliz Cloud's free tier works).

All three stores and the LLM client are constructed lazily on first use, so a
missing dependency degrades one feature instead of preventing the API from
starting.

## Choosing an LLM

The LLM is configured by endpoint, not by vendor — anything speaking the OpenAI
chat-completions protocol works, selected entirely by `LLM_BASE_URL` and
`LLM_MODEL`:

Pick on **throughput, not capability**. One city costs 150–250 calls and
400–600k tokens — about 80 of ours, plus Graphiti's entity extraction, which
runs several calls per fact and is usually the larger half. Free tiers meter
that either per second or per day, and the difference decides whether a run
finishes: a per-second limit just makes a batch job slower, a per-day cap stops
it until tomorrow.

| Provider | `LLM_BASE_URL` | `LLM_MODEL` | Free-tier ceiling | Runs |
|---|---|---|---|---|
| **Mistral** (default) | `https://api.mistral.ai/v1` | `mistral-small-latest` | ~1 req/**sec**, 500k TPM, 1B tok/month | ~2,000/month |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash-lite` | 1,000 req/**day** | ~4/day |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | 1,000 RPD but **100k tok/day** | ~¼ of a city |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-flash` | paid | — |
| OpenAI | `https://api.openai.com/v1` | `gpt-4.1-mini` | paid, most reliable | — |

Groq looks best on requests — 14,400/day on `llama-3.1-8b-instant` — but that
model allows only 6k tokens per minute, and one extraction call carrying a page
body can exceed a whole minute's budget by itself. Avoid Gemini 3.x previews
entirely: they cap at **20 requests/day**, which cannot finish a single pass.

Two settings break a provider switch outright if you get them wrong:

- **`LLM_REASONING_EFFORT` must be empty** unless the model actually thinks.
  OpenAI-compatible shims *reject* parameters they do not implement rather than
  ignoring them, so a stray value fails every call with an opaque 400. Set it
  to `low` on Gemini 3 or OpenAI o-series, where thinking tokens otherwise come
  out of the answer's budget and a long prompt returns an empty string.
- **`GRAPHITI_SEMAPHORE_LIMIT`** bounds Graphiti's own concurrency, which
  `LLM_MAX_CONCURRENCY` does not reach. `graphiti-core` defaults to 20 parallel
  extraction calls, and it is the largest caller in the run.

Keep `LLM_MAX_CONCURRENCY` at or under the provider's requests-per-second
allowance — 1 on Mistral's free plan, 8 on a per-day provider for roughly 8×
the speed. Rate limits are retried with backoff, but staying under the limit is
faster than recovering from it.

If you would rather pay than migrate, Gemini Tier 1 on `gemini-2.5-flash-lite`
costs roughly **$0.10 per city**.

**Embeddings are generated locally** by sentence-transformers, shared between
Milvus and Graphiti's embedder *and* its reranker. That is deliberate: the
provider only has to serve chat completions, so chat-only APIs work without a
second key, and it removes a failure mode — Graphiti's default `OpenAIEmbedder`
pointed at a provider with no `/embeddings` route fails on every graph write.

## API

| Endpoint | Purpose |
|---|---|
| `POST /research` | Run the pipeline for a city. Body: `{"city": "...", "country": "..."}` |
| `POST /ask` | Cited answer over the vector store + knowledge graph |
| `GET /graph/{city}` | Graph facts with temporal validity. Optional `?question=` scopes the traversal |
| `GET /report/{city}` | The stored brief as JSON |
| `GET /report/{city}/download` | The brief as a `.md` download |
| `GET /sources/{city}` | Every URL considered and the crawl gate's verdict, refusals included |
| `GET /facts/{city}` | Audit trail: tier, reasoning, origin URL, corroborators |
| `GET /cities` | Cities already researched |
| `GET /health` | Liveness + run mode |

## Generating the example brief

```bash
python -m scripts.generate_sample_report "Pune" --country India
```

Writes `examples/pune-brief.md` using the same workflow the API calls, so the
committed example is genuinely generated by the system. Run it in LIVE mode for
a real brief; MOCK mode produces a deterministic structural example.

## Directory layout

```
app/
  config.py                 run mode, provider, and the cost/latency budget
  main.py                   FastAPI app + all read endpoints
  models/schemas.py         shared contracts + the research dimension set
  graph/state.py            LangGraph state, and which fields accumulate
  graph/workflow.py         StateGraph wiring (the actual orchestration)
  agents/planner.py            decomposition + retry narrowing
  agents/query_gen.py          dimension → search queries
  agents/search_agent.py       discovery only, capped per dimension
  agents/crawlability_agent.py ToS denylist → robots.txt → X-Robots-Tag
  agents/extraction_agent.py   fetch ALLOWED urls only, then atomic claims
  agents/fact_check_agent.py   independent adjudication + tier derivation
  agents/graph_writer_agent.py the only node that persists
  agents/coverage_evaluator.py sufficiency per dimension, bounded retries
  agents/report_agent.py       the brief, plus gap and report persistence
  stores/relational_store.py   SQLite governance layer (Postgres via URL)
  stores/vector_store.py       Milvus semantic recall
  stores/graph_store.py        Neo4j + Graphiti, read and written
  stores/lazy.py               shared connect-on-first-use proxy
  llm/client.py                OpenAI-compatible chat client, retrying
  llm/embeddings.py            local sentence-transformers, shared
  llm/json_utils.py            tolerant parsing of "ONLY JSON" responses
  util.py                      url → domain, shared by three layers
frontend/index.html          single-page UI, served by FastAPI itself
scripts/check_services.py    preflight check for LLM + Neo4j + Milvus
scripts/generate_sample_report.py
tests/test_pipeline.py       full graph in MOCK mode, asserting the non-negotiables
tests/test_json_utils.py     the tolerant LLM-output parser
docs/DEPLOYMENT.md           every dependent service and how to deploy it
docs/PRESENTATION.md         the deck
Dockerfile                   single-container image (app + UI)
docker-compose.yml           demo deployment
docker-compose.milvus.yml    opt-in override for a real Milvus cluster
```

## Deployment

Full instructions, including every dependent service and how to deploy each
one, are in **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**. The short version:

```bash
cp .env.example .env      # fill in LLM_API_KEY, NEO4J_URI, NEO4J_PASSWORD
docker compose up -d --build
docker compose exec app python -m scripts.check_services   # preflight
```

That publishes the UI and the API together on port 80, with SQLite and the
Milvus Lite database on a named volume.

**It starts exactly one container.** That is intentional, and worth being able
to explain: of the three datastores, only one is a server. SQLite is a library
writing to a file, Milvus Lite is a library writing to a file, and Neo4j is the
hosted Sandbox. Three datastores, one process, no orchestration. Of the eight
dependent services, only two — the LLM provider and the Neo4j Sandbox — have to
exist before the app will do real research.

## What is deliberately not built

Summarised here, argued in [ARCHITECTURE.md](ARCHITECTURE.md#trade-offs-and-what-was-cut):

- **No human review queue.** The brief flags what needs review and
  `fact_audit.reviewed_by_human` is the column a queue would write to, but
  nothing blocks on a human today.
- **No streaming progress.** `POST /research` blocks; the UI moves its stage
  indicators as a group.
- **No PDF or non-HTML extraction.** Rejected with a recorded warning rather
  than feeding bytes to an LLM. The biggest recall limitation.
- **No freshness policy.** `sources.fetched_at` and `reports.generated_at`
  record when evidence was gathered, but nothing acts on age; re-running a
  city overwrites its brief.
- **No dedicated NER pass or cross-encoder.** Graphiti handles entity
  extraction; reranking is cosine similarity over the same bi-encoder.
