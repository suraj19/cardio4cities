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
3. Add `TAVILY_API_KEY`. Optional — search falls back to `duckduckgo-search`
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

| Provider | `LLM_BASE_URL` | `LLM_MODEL` | Notes |
|---|---|---|---|
| **Google Gemini** (default) | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-3.5-flash` | Best JSON adherence, which is what Graphiti's entity extraction depends on. Key from [AI Studio](https://aistudio.google.com/apikey). |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-flash` | Cheapest. `deepseek-chat` was retired 2026-07-24. |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | Fastest. Avoid small models; they break structured extraction. |
| OpenAI | `https://api.openai.com/v1` | `gpt-4.1-mini` | Paid, most reliable. |

Two Gemini-specific notes. Avoid `gemini-2.5-flash` — it retires on 2026-10-20;
`gemini-3.8-flash` is the most capable Flash if you want to trade cost for
accuracy. And Gemini 3 models cannot disable thinking, with thinking tokens
billed against the same budget as the answer, so `LLM_REASONING_EFFORT=low` and
a roomy `LLM_MAX_TOKENS` are set by default — without them a long extraction
prompt can spend its whole budget reasoning and return an empty string.

On a free-tier key, lower `LLM_MAX_CONCURRENCY` to 2 or 3. Extraction and
fact-checking fan out in parallel, and the client retries rate limits with
backoff, but staying under the limit is faster than recovering from it.

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
hosted Sandbox. Three datastores, one process, no orchestration. Of the seven
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
