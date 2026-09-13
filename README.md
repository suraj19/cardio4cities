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

Open <http://localhost:8000> for the UI, or call it directly. Research returns
a job id rather than the brief — a run is 10–20 minutes and no proxy will hold
a silent request open that long:

```bash
JOB=$(curl -sX POST http://localhost:8000/research \
  -H "Content-Type: application/json" \
  -d '{"city": "Pune", "country": "India"}' | jq -r .job_id)

# Per-node progress while it runs, the brief and the token cost when it lands.
curl -s "http://localhost:8000/research/$JOB?wait=60" | jq '.status, .current_stage'
```

## Switching to LIVE mode

1. Set `RUN_MODE=LIVE` in `.env` (this is the default).
2. Add `LLM_API_KEY`, plus `LLM_BASE_URL` and **both** model settings —
   `LLM_MODEL` and `LLM_BULK_MODEL` — for your provider (see below).
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

That makes one tiny call against **each** configured model, counts nodes in
Neo4j and lists Milvus collections, so a bad key, a retired model ID or an
expired Sandbox shows up as a labelled failure rather than an error in the
middle of a run. Both models are checked because they fail independently: a
wrong `LLM_BULK_MODEL` yields a run with no claims and an empty graph while the
judgement model answers perfectly.

Milvus Lite is Linux and macOS only. On Windows, either run the app in Docker or
set `MILVUS_URI` to a real endpoint (Zilliz Cloud's free tier works).

All three stores and the LLM client are constructed lazily on first use, so a
missing dependency degrades one feature instead of preventing the API from
starting.

## Choosing an LLM

The LLM is configured by endpoint, not by vendor — anything speaking the OpenAI
chat-completions protocol works, selected by `LLM_BASE_URL` and two model
settings.

**Two models, because a run makes two kinds of call.** One city costs 150–250
calls and 400–600k tokens, and they are not the same work:

| Role | Setting | Calls/run | What it does |
|---|---|---|---|
| Judgement | `LLM_MODEL` | ~30 | Query planning, adjudicating a claim, the narrative, `/ask` |
| Bulk | `LLM_BULK_MODEL` | ~120 | Claim extraction, and Graphiti's entity extraction behind the graph writes |

The bulk calls turn text the model has been handed into JSON against a fixed
schema. Nothing in them is a judgement, which is why they also run at
`LLM_BULK_REASONING_EFFORT=none` — thinking tokens bill at the **output**
rate, so effort is a cost setting rather than a quality dial.

Defaults are `mistral-medium-latest` for judgement and `mistral-small-latest`
for bulk. On the free Experiment plan that is **$0**; on a paid key it is
roughly **$0.40 per city**.

> **On a paid key, change `LLM_MODEL` to `mistral-large-latest`** — Large 3 is
> both stronger and ~3× cheaper than Medium 3.5 ($0.50/$1.50 per 1M against
> $1.50/$7.50), taking a run to about **$0.19**. The default is Medium only
> because **Large 3 is not served on the free tier**: it is absent from
> `GET /v1/models` for a free key and returns `403 tier_not_allowed`.

> **Mistral's free tier is the only one that can finish a city.** It gives you
> **1B tokens/month** against the 400–600k a run consumes, so you are
> throttled on request *rate* and never walled on volume — a slow run still
> produces a brief. Compare Gemini's free tier at **20 requests/day** (one
> city needs 150–250) or Groq's at **100k tokens/day** (about a fifth of one
> city); both stop mid-run and wait 24 hours. `RUN_MODE=MOCK` needs no key at
> all.

Mistral does not publish the free-tier request rate — read yours off
**Admin Panel → API → Limits**, or off the response headers, which is faster:

```
x-ratelimit-limit-req-minute      your actual RPM ceiling
x-ratelimit-remaining-req-minute  what is left in this window
```

A ceiling of **`0`** means the account has no completions allowance at all —
not a throttle you can wait out. That is what an unactivated free plan looks
like: the key authenticates, so you get `429 Rate limit exceeded` rather than
a `401`, which points you at your code instead of at your account. Activate
the Experiment plan (phone verification, no card) in the Admin Console.

| Provider | `LLM_BASE_URL` | Notes |
|---|---|---|
| **Mistral** (default) | `https://api.mistral.ai/v1` | Only viable free tier. `mistral-large-latest` is **paid-tier only**; bulk runs `reasoning_effort=none` |
| DeepSeek | `https://api.deepseek.com/v1` | No free tier, but **no RPM/TPM/RPD at all** — just 2500 concurrency, the best shape for this burst workload. ~$0.13/city |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-3.5-flash` / `gemini-3.1-flash-lite` at `low` / `minimal`. Tier 1 works (~$0.45/city); free tier does not |
| Groq | `https://api.groq.com/openai/v1` | Fast, but **rejects** `reasoning_effort`. Avoid models below ~30B — structured extraction degrades sharply |
| OpenAI | `https://api.openai.com/v1` | Paid |
| Ollama (self-hosted) | `http://host.docker.internal:11434/v1` | Unmetered and keyless. Same size caveat as Groq |

Three settings break a provider switch outright if you get them wrong:

- **Each reasoning-effort setting must be empty** unless *that* model
  implements it. OpenAI-compatible shims *reject* parameters they do not
  support rather than ignoring them, so a stray value fails every call with an
  opaque 400. The two defaults differ for exactly this reason: Mistral Small 4
  is a hybrid reasoning model and takes the parameter, Large 3 does not.
- **`GRAPHITI_SEMAPHORE_LIMIT`** bounds Graphiti's own concurrency, which
  `LLM_MAX_CONCURRENCY` does not reach. `graphiti-core` defaults to 20 parallel
  extraction calls, and it is the largest caller in the run.
- **`LLM_MAX_CONCURRENCY`** must stay at or under the provider's rate
  allowance. It no longer throttles the crawler — robots.txt checks and page
  fetches are sized by `HTTP_MAX_CONCURRENCY`, since they cost no tokens.

Every finished job reports `llm_usage` with calls and tokens **per model**,
which is how you confirm the split is actually in effect: all ~150 calls
landing on one model means the bulk setting is not being used.

**Embeddings are generated locally** by sentence-transformers, shared between
Milvus and Graphiti's embedder *and* its reranker. That is deliberate: the
provider only has to serve chat completions, so chat-only APIs work without a
second key, and it removes a failure mode — Graphiti's default `OpenAIEmbedder`
pointed at a provider with no `/embeddings` route fails on every graph write.

## API

| Endpoint | Purpose |
|---|---|
| `POST /research` | Start a run for a city. Body: `{"city": "...", "country": "..."}`. Returns `202` with a `job_id` — it does not wait |
| `GET /research/{job_id}` | Status, per-node progress, token cost, and the brief once it exists. `?wait=N` long-polls up to 60s |
| `DELETE /research/{job_id}` | Cancel an in-flight run |
| `GET /research` | Recent jobs |
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
- **No push progress.** A run reports per-node progress, but the client has to
  poll for it — there is no SSE or websocket stream. Polling was the right
  trade at a 2-second interval against a 10-minute run, and it is one endpoint
  rather than a second transport to keep alive through a proxy.
- **No durable job queue.** Jobs live in the process, so a restart forgets the
  ones in flight. Finished work survives regardless — the brief, facts and
  sources are written to the relational store by the pipeline itself. A real
  queue is the change horizontal scaling would force, alongside Postgres.
- **No PDF or non-HTML extraction.** Rejected with a recorded warning rather
  than feeding bytes to an LLM. The biggest recall limitation.
- **No freshness policy.** `sources.fetched_at` and `reports.generated_at`
  record when evidence was gathered, but nothing acts on age; re-running a
  city overwrites its brief.
- **No dedicated NER pass or cross-encoder.** Graphiti handles entity
  extraction; reranking is cosine similarity over the same bi-encoder.
