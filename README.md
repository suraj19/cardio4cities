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
- [docs/WALKTHROUGH_CURRENT.md](docs/WALKTHROUGH_CURRENT.md) — every component
  of the application, in the order a request travels through it
- [docs/WALKTHROUGH_BASELINE.md](docs/WALKTHROUGH_BASELINE.md) — the same
  walkthrough for the previous version, with each change and its reason
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

It runs the full ten-node graph and asserts the non-negotiables as contracts,
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

Both default to **`mistral-small-latest`**, which is Mistral's only
**open-weight** model — Apache 2.0, weights published — so the entire pipeline
runs on something a reader can inspect, self-host or audit. Sharing a model
does not collapse the split: it moves to the thinking budget, `high` for the
~30 judgement calls and `none` for the ~120 bulk ones. One model, two efforts.
On the free Experiment plan that is **$0**; on a paid key, about **$0.12 per
city**.

> **On a paid key, `mistral-large-latest` is the judgement upgrade** — and it
> is also ~3× cheaper than Medium 3.5 ($0.50/$1.50 per 1M against $1.50/$7.50),
> so the name order is misleading. Two caveats: Large is **not served on the
> free tier** (absent from `GET /v1/models`, returns `403 tier_not_allowed`),
> and it does **not** implement `reasoning_effort` — so blank
> `LLM_REASONING_EFFORT` in the same change or every judgement call 400s.
> Both Medium and Large are closed-weight.

> **Mistral's free tier is the only one that can finish a city.** It gives you
> **1B tokens/month** against the 400–600k a run consumes, so you are
> throttled on request *rate* and never walled on volume — a slow run still
> produces a brief. Compare Gemini's free tier at **20 requests/day** (one
> city needs 150–250) or Groq's at **200k tokens/day** (about half of one
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
| Groq | `https://api.groq.com/openai/v1` | Fastest tokens/sec here, but the free tier is **200k tokens/day** — half a city. Llama is Enterprise-only now; the self-serve models are `openai/gpt-oss-120b` / `-20b` and `qwen/qwen3.8-27b`. **Only Qwen accepts `reasoning_effort=none`**; gpt-oss accepts `low`/`medium`/`high` only, so bulk extraction cannot stop paying for reasoning. JSON mode additionally needs `reasoning_format` set to `hidden` on Qwen |
| OpenAI | `https://api.openai.com/v1` | Paid |
| **Ollama** (local) | `http://localhost:11434/v1` | **No rate limit and no key.** Costs wall-clock instead of quota. See below |

### Running the whole pipeline locally with Ollama

The only configuration with **no rate limit at all**. A city is 150–250 calls,
and every hosted free tier meters that somewhere; here the only budget is your
own machine's time. No API key is involved at any point.

```bash
ollama pull qwen3:8b

# Windows PowerShell
$env:OLLAMA_CONTEXT_LENGTH=32768; ollama serve
# macOS / Linux
OLLAMA_CONTEXT_LENGTH=32768 ollama serve
```

Then in `.env` — leave `LLM_API_KEY` empty, and comment out the
`LLM_REASONING_EFFORT` line:

```bash
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen3:8b
LLM_BULK_MODEL=qwen3:8b
LLM_BULK_REASONING_EFFORT=none
```

Confirm it before running a city — the preflight calls both profiles and reads
the server's real context window:

```bash
python -m scripts.check_services
```

Three things are worth knowing before you rely on this:

- **`OLLAMA_CONTEXT_LENGTH` is not optional.** Ollama defaults to a small
  window and **truncates** longer prompts instead of refusing them. Graphiti's
  entity-extraction prompts are past 4k, so the default gives you a run that
  completes, writes a knowledge graph with **no fact edges**, and reports no
  error anywhere. The OpenAI-compatible API has no way to set context size per
  request, so it must be set on the server — which is why
  `check_services` probes `/api/ps` and fails the check when it is under 32768.
- **Unmetered is not fast.** 150–250 calls against a local 8B model is tens of
  minutes on a GPU and hours on CPU. This is survivable only because a run is
  a background job you poll — the same change that fixed the Railway timeout.
- **Quality drops with model size.** 8B runs the whole pipeline on a normal
  laptop, but extraction is weaker than hosted Small 4 and the brief will be
  thinner. `qwen3:30b` or `gpt-oss:20b` are closer if you have the memory.
  Below ~7B, structured extraction starts inventing fields.

No API key is required because `LLM_BASE_URL` resolves to a local host —
`Settings.llm_is_local` in `app/config.py`. Both reasoning-effort settings also
default to empty in that case, since LM Studio and llama.cpp are reached the
same way and do not all implement the field. This is a **local path only**: a
Railway free-tier container has 0.5 GB of RAM and cannot host a model, so
deployments keep using a hosted provider.

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

**Embeddings are generated locally** by fastembed (ONNX), shared between
Milvus and Graphiti's embedder *and* its reranker. That is deliberate: the
provider only has to serve chat completions, so chat-only APIs work without a
second key, and it removes a failure mode — Graphiti's default `OpenAIEmbedder`
pointed at a provider with no `/embeddings` route fails on every graph write.

## Observability (OpenTelemetry)

A city is 150–250 model calls across ten nodes and three datastores. When a
brief comes back thin, the question is always *which part gave up* — and the
pipeline already measured itself, but only after the run, only in-process, and
only if you went looking. Tracing turns the same measurements into something
you can open.

Off by default. The fastest way to see it, with no collector to run:

```bash
OTEL_ENABLED=true OTEL_CONSOLE_EXPORT=true uvicorn app.main:app
```

Or ship to anything speaking OTLP/HTTP — Jaeger, Grafana Tempo, Honeycomb,
Datadog, an OpenTelemetry Collector:

```bash
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one
# .env
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

### An LLM-native dashboard — Opik or Langfuse

Both ingest OTLP over HTTP natively, so either is **configuration only, no
code change**. Set `OTEL_ENABLED=true` and two more variables.

**Opik** (Comet). Self-hosting is free — `./opik.sh`, UI on `localhost:5173`:

```bash
# Cloud
OTEL_EXPORTER_OTLP_ENDPOINT=https://www.comet.com/opik/api/v1/private/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=<api-key>,projectName=cardio4cities,Comet-Workspace=<workspace>

# Self-hosted — no headers needed
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:5173/api/v1/private/otel
```

**Langfuse.** Auth is Basic, built from the two project keys:

```bash
# AUTH=$(printf '%s' 'pk-lf-...:sk-lf-...' | base64 -w0)
OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel   # EU
# https://us.cloud.langfuse.com/api/public/otel                          # US
# http://localhost:3000/api/public/otel                                  # self-hosted
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <AUTH>,x-langfuse-ingestion-version=4
```

Langfuse reads the `gen_ai.*` attributes this project already emits and turns
those spans into *generations* with model, token and cost figures, nesting the
node spans around them as observations. So the semantic conventions used in
`app/llm/client.py` are not decoration — they are what makes the dashboard
understand the trace.

Three things worth knowing before you point either at it:

- **Both require OTLP over HTTP, not gRPC.** This project already uses
  `opentelemetry-exporter-otlp-proto-http`, so that is already right. A test
  pins the exact trace URL the exporter builds against the one Opik
  documents.
- **Turn on `OTEL_CAPTURE_CONTENT=true`, or the dashboard will look broken.**
  Prompts and completions are not recorded by default, because they contain
  scraped third-party page content. For these tools the prompt/completion
  pair *is* the product, so with the flag off you get a correct trace tree
  with timings and token counts and an empty panel on every span — which
  reads as a failed integration rather than a deliberate default. This is a
  real decision, not a formality: turning it on copies page content into a
  third-party service. `OTEL_CAPTURE_CONTENT_CHARS` (default 4000) caps each
  field.
- **Metrics switch off automatically** for both, because both ingest traces
  only. Deriving the metrics URL from the shared endpoint the way the OTel
  convention says to would POST every 60 seconds to something that can never
  accept it — a steady drip of export errors while traces arrive perfectly,
  which is a confusing thing to debug. Set
  `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` to send metrics somewhere that wants
  them.

Opik also has a native `OpikTracer` LangGraph callback, which is richer — it
captures each node's input and output state automatically and unlocks Opik's
evaluation features. It is also a second, parallel instrumentation path, it
would upload full node state (scraped page bodies included) with no
equivalent of the content flag, and with `ainvoke` it needs explicit context
propagation. The OTLP route reuses the instrumentation that is already tested
here, so it is the one this README recommends. Reach for `OpikTracer` if what
you want is Opik's *evaluation* side — scoring brief quality across runs,
comparing prompt versions — which tracing alone will not give you.

One run is **one trace**, and it looks like this (real output, MOCK mode):

```
research Springfield                             151.3 ms
  node.planner                                     0.1 ms
  node.query_gen                                   0.7 ms
    chat mistral-small-latest                      0.4 ms
  node.extraction                                  3.9 ms
    chat mistral-small-latest   x10
  node.fact_check                                  4.5 ms
    chat mistral-small-latest   x12
  node.graph_writer                              115.4 ms      <- 76% of the run
  node.report                                      8.2 ms
```

That last line is the point. "graph_writer is the slow one because Graphiti
re-extracts entities for every fact" is a claim in this README; on a trace it
is a measurement, and each of those extractions is a child span you can open.

| Instrumented | How |
|---|---|
| The whole run | Root span, created in `app/jobs.py` — not in the request handler, which returns a job id in milliseconds while the work runs for minutes |
| The ten nodes | `workflow._timed`, carrying the same summary string the log line prints, so a trace and a log of one run cannot disagree |
| Every model call | GenAI semantic conventions (`gen_ai.request.model`, `gen_ai.usage.input_tokens`, …), one span per call **including its retries**, each attempt an event — a rate-limited call that succeeded on its fourth attempt after 30s of backoff is the thing you most want to see |
| Every embedding batch | The component both evidence stores share; see below |
| Milvus and Neo4j | `milvus.search`, `milvus.upsert`, `graphiti.search`, `graphiti.add_episode` |
| Outbound HTTP, FastAPI, SQLAlchemy | Auto-instrumentation |

**Prompts and completions are deliberately not recorded.** They carry scraped
page content, and a trace backend is not a place to put that. Spans carry
lengths and token counts; the tests assert no attribute is long enough to be
content.

Two implementation notes worth knowing:

- **Thread context propagation is not optional.** Extraction, fact-checking,
  crawlability and search all fan out across `ThreadPoolExecutor`s, and trace
  context is a contextvar — asyncio tasks inherit it, threads do not. Without
  `opentelemetry-instrumentation-threading`, every model call made inside a
  pool starts its own root trace: ~150 orphans per city. The suite asserts a
  run is exactly one trace, which is how this was caught.
- **MOCK runs are traced too**, with `gen_ai.system=mock`, so the trace shape
  is identical offline. That is what makes the instrumentation testable with
  no keys and no collector — a MOCK trace missing a span is how you find a
  call site nobody instrumented.

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
  llm/embeddings.py            local ONNX embeddings, shared
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
