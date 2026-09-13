# Deployment

Everything needed to stand this up, from a laptop smoke test to a public demo
URL. [README.md](../README.md) covers what the application does;
[ARCHITECTURE.md](../ARCHITECTURE.md) covers why it is built this way.

---

## 1. Dependent services at a glance

Eight things the application talks to. Only **two** are mandatory for a live
demo, and only **one** of the eight is a server you have to operate yourself.

| # | Service | Role | Runs where | Required? | Cost |
|---|---|---|---|---|---|
| 1 | **Application container** (FastAPI + UI) | Serves the API *and* `frontend/index.html` from one port | Your VM, via `docker compose` | **Yes** | VM cost only |
| 2 | **SQLite** | Relational store — source registry, fact audit trail, gaps, saved briefs | Embedded. A file on the app's volume | **Yes** | Free |
| 3 | **Milvus** | Vector store — semantic recall over extracted passages | Embedded (Milvus Lite, default), or a standalone cluster, or Zilliz Cloud | **Yes** — but Lite needs no server | Free (Lite) |
| 4 | **Neo4j Graph Database Sandbox** | Temporal knowledge graph, written through Graphiti | Hosted by Neo4j at [sandbox.neo4j.com](https://sandbox.neo4j.com) | **Yes** in LIVE mode | Free, **expires** |
| 5 | **LLM provider** (Mistral by default) | Query planning, claim extraction, fact-check adjudication, narrative, `/ask` | Hosted, any OpenAI-compatible chat endpoint | **Yes** in LIVE mode | $0 on Mistral's free plan, or ~$0.14/city paid — see §3.5 |
| 6 | **Tavily** | Higher-quality search discovery | Hosted | No — falls back to DuckDuckGo | Free tier |
| 7 | **sentence-transformers** (`all-MiniLM-L6-v2`) | Local embeddings, shared by Milvus and Graphiti | In-process, baked into the image | **Yes** | Free, no key |
| 8 | **WHO GHO + World Bank Open Data** | Country-level indicators for `cv_burden` and `health_system` | Hosted, public REST | No — falls back to web discovery | Free, **no key** |

Two things follow from this table, and both are worth being able to say out
loud in a review:

- **The default deployment is one container.** Of the three datastores, only
  one is a server. SQLite is a library writing to a file, Milvus Lite is a
  library writing to a file, and Neo4j is hosted by Neo4j. Three datastores,
  one process, no orchestration.
- **No embedding API is in the list.** Embeddings run locally, which is what
  lets the chat provider be swapped with two environment variables — see
  [`app/llm/embeddings.py`](../app/llm/embeddings.py) for the full argument.

### Dependency graph

```mermaid
graph LR
  U[Browser] --> A[App container<br/>FastAPI + static UI<br/>:8000]
  A --> S[(SQLite<br/>embedded file)]
  A --> M[(Milvus Lite<br/>embedded file)]
  A --> E[sentence-transformers<br/>in-process]
  A -.bolt.-> N[(Neo4j Sandbox<br/>hosted)]
  A -.https.-> L[LLM provider<br/>Mistral / DeepSeek / Gemini / Groq<br/>judgement + bulk models]
  A -.https.-> T[Tavily or DuckDuckGo]
  A -.https.-> O[WHO GHO + World Bank<br/>public REST, no key]

  subgraph vol["Docker volume app-data"]
    S
    M
  end
```

Solid edges are in-process or on-disk. Dotted edges leave the VM, and every
one of them is wrapped in error handling that degrades the run rather than
failing it — except the LLM, which the pipeline genuinely cannot proceed
without.

---

## 2. Fastest path to a running demo

```bash
git clone <repo> && cd cardio4cities
cp .env.example .env
#   fill in: LLM_API_KEY, NEO4J_URI, NEO4J_PASSWORD
docker compose up -d --build
```

Open `http://<host>/`. The UI and the API are on the same origin, so there is
no separate frontend host and no CORS configuration to get wrong.

Before trusting it, run the preflight — it checks all four external
dependencies and tells you which one is wrong rather than making you read a
stack trace:

```bash
docker compose exec app python -m scripts.check_services
```

---

## 3. Per-service deployment

### 3.1 Application container

Built from the [`Dockerfile`](../Dockerfile): `python:3.12-slim`, dependencies
from `requirements.txt`, the embedding model baked in at build time, and
`uvicorn` serving `app.main:app` on port 8000. `docker-compose.yml` publishes
that on port 80 and mounts a named volume at `/app/data`.

```bash
docker compose up -d --build
docker compose logs -f app
```

Two details in the image that exist for a reason:

- `HF_HOME=/opt/hf-cache`, deliberately **outside** `/app/data`. The model is
  downloaded at build time; `/app/data` is a volume mount at runtime, so
  caching the model there would shadow it and send the first request off to
  re-download ~90 MB.
- `build-essential` is installed because several wheels have no ARM build, and
  the free demo VMs (Oracle Ampere, AWS Graviton) are ARM.

**Healthcheck.** `GET /health` reports `run_mode` and per-store reachability.
Docker polls it every 30s with a 40s start period — the start period covers
loading the embedding model into RAM.

**Resources.** ~1 GB RAM for the embedding model plus headroom; 2 GB total is
comfortable with Milvus Lite. One vCPU is fine; research runs are
network-bound, not CPU-bound.

### 3.2 SQLite (relational store)

No deployment step. SQLAlchemy creates the schema on first use at
`sqlite:///./data/cardio4cities.db`, which is inside the mounted volume, so it
survives `docker compose down` and image rebuilds.

To move to Postgres — the right call the moment more than one app instance
writes, since SQLite's single-writer lock does not survive horizontal
scaling — change one variable and add the driver:

```bash
DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/cardio4cities
```

No application code changes; every query goes through SQLAlchemy.

**Backup.** `docker compose cp app:/app/data/cardio4cities.db ./backup.db`.

### 3.3 Milvus (vector store)

`MILVUS_URI` alone selects the topology — there is no code path that differs.

**Option A — Milvus Lite (default).** A path ending in `.db` runs the embedded
build. No server, no extra containers.

```bash
MILVUS_URI=./data/milvus.db
```

> Milvus Lite ships Linux and macOS wheels only. On Windows, run inside Docker
> or WSL2, or use option B or C. This is the usual reason a native Windows
> `pip install` appears to succeed and then fails at first query.

**Option B — Milvus standalone.** A real cluster: three extra containers
(etcd for metadata, MinIO for object storage, milvus itself) and roughly
2–3 GB more RAM. Use it to demo the cluster topology; the API and the query
results are identical.

```bash
docker compose -f docker-compose.yml -f docker-compose.milvus.yml up -d --build
```

The override sets `MILVUS_URI=http://milvus:19530` and makes the app wait on
Milvus's healthcheck. Below about 4 GB of RAM, stay on Lite.

**Option C — Zilliz Cloud.** Managed Milvus, free tier. Set both:

```bash
MILVUS_URI=https://<your-endpoint>.api.gcp-us-west1.zillizcloud.com
MILVUS_TOKEN=<api-key>
```

The collection (`city_passages`, 384-dim, COSINE) is created automatically on
first write in all three cases.

### 3.4 Neo4j Graph Database Sandbox

The case study mandates the Sandbox specifically, and it is hosted — there is
nothing to deploy, but there **is** something to maintain.

1. Sign in at [sandbox.neo4j.com](https://sandbox.neo4j.com) and create a
   **Blank Sandbox**.
2. Open **Connection details** and copy the **Bolt URL** and **Password**.
3. Put them in `.env`:

```bash
NEO4J_URI=bolt://<ip>:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<password>
```

> **Sandboxes expire after 3 days, extendable to 10.** A restarted Sandbox
> comes back with a **new IP and a new password**, so re-copy both rather than
> only the password. Check yours the morning of any demo. `/graph/{city}`
> returns a 503 with this hint when the connection fails, and a research run
> that cannot reach Neo4j records a warning and completes anyway — the facts
> are still in the relational audit trail, you just lose graph exploration.

**Neo4j Aura** works unchanged if you want something that does not expire: the
URI is `neo4j+s://<id>.databases.neo4j.io` and nothing else differs.

Graphiti's indices and constraints are built automatically on the first write
(`_ensure_indices` in [`app/stores/graph_store.py`](../app/stores/graph_store.py)),
so a blank Sandbox needs no manual setup.

**Verify facts actually landed**, because index creation succeeding and facts
being written are independent — the first creates property keys even with zero
data, which makes an empty graph look partly populated:

```cypher
MATCH ()-[e:RELATES_TO]->() RETURN count(e) AS fact_edges;
MATCH (n:Entity) RETURN count(n) AS entities;
```

`fact_edges` is the number that matters; `count(n)` can be non-zero with no
facts in the graph at all. `scripts/check_services.py` reports both.

> `graphiti-core` is pinned exactly (`==0.30.2`) and the pin is **load-bearing**,
> not hygiene. Its `LLMConfig` and `add_episode` signatures have both changed
> incompatibly across minor versions, and because graph writes degrade to a
> warning rather than an error, a mismatch presents as a silently empty graph
> rather than a stack trace. Do not float this dependency.

### 3.5 LLM provider

Any OpenAI-compatible chat endpoint. Only chat completions are used — no
embeddings endpoint is required, which is why providers without one still
work.

**Choose on the shape of the limit, not on capability or headline size.** A
single city costs 150–250 calls and 400–600k tokens: roughly 80 of ours plus
Graphiti's entity extraction, which runs several calls per fact and is usually
the larger half. Free tiers meter that three different ways and only one is
survivable:

| Metering | Effect on a run | Example |
|---|---|---|
| Per **day** | A wall. Stops unfinished, waits 24h | `gemini-3.5-flash` 20 req/day; Groq 70B 100k tok/day |
| Per **month** | A budget. 600k against 1B is noise | Mistral 1B tok/month |
| Per **second** | A throttle. Slower, still finishes | Mistral's request rate |

Only the throttle is absorbable, and only because `POST /research` no longer
holds an HTTP request open (see §4.1 and `app/jobs.py`). An hour-long run is
now merely slow; before that change it was impossible on any host.

**Two models, not one.** `LLM_MODEL` and `LLM_BULK_MODEL` are both set, and
the split is the difference between cents and dollars per city:

| Role | Setting | Calls/run | What it does |
|---|---|---|---|
| Judgement | `LLM_MODEL` | ~30 | Query planning, fact-check adjudication, the narrative, `/ask` |
| Bulk | `LLM_BULK_MODEL` | ~120 | Claim extraction, and Graphiti's entity extraction behind the graph writes |

The bulk calls turn text the model has been handed into JSON against a fixed
schema. There is no judgement in them and nothing to deliberate about, which
is why they also run at `LLM_BULK_REASONING_EFFORT=none` — thinking tokens
bill at the **output** rate, so effort is a cost setting. Pointing both
variables at the judgement model is the most expensive single change here:

| Model | Input /1M | Output /1M (incl. thinking) | Free tier | ~Cost per city |
|---|---|---|---|---|
| `mistral-medium-latest` (judgement default) | $1.50 | $7.50 | **yes** | ~$1.40 if used for everything |
| `mistral-large-latest` | $0.50 | $1.50 | **no — 403** | ~$0.38 if used for everything |
| `mistral-small-latest` (bulk default) | $0.15 | $0.60 | **yes** | ~$0.11 if used for everything |
| **The shipped split** (Medium + Small) | — | — | **yes** | **$0** free, ~$0.40 paid |
| Large + Small (paid keys only) | — | — | no | **~$0.19** |

Two things here are counter-intuitive and both were verified against a live
free key rather than against documentation:

1. **Mistral's generation numbers do not order by price.** Large 3 is *three
   times cheaper* than Medium 3.5 ($0.50/$1.50 against $1.50/$7.50) as well as
   stronger.
2. **Large 3 is not served on the free tier.** It is absent from
   `GET /v1/models` for a free key and returns `403 tier_not_allowed`
   (code 1910) if requested anyway.

Together those mean the shipped default is the *expensive* choice the moment
you start paying. **On a paid key, set `LLM_MODEL=mistral-large-latest`** and a
run drops from ~$0.40 to ~$0.19. The default is Medium only because it is the
best judgement model a no-bill account can actually reach.

> **Mistral's free "Experiment" plan is the only free tier that finishes a
> city.** 1B tokens/month against 400–600k per run means volume is a
> non-issue; you are throttled on request rate instead, and a throttle only
> costs time. Requests on the free plan may also be used for training unless
> you opt out in the Admin Console → Privacy.

Mistral does not publish the free-tier request rate, and third-party figures
for it disagree by two orders of magnitude. Do not trust either — every
response carries the real answer:

```
x-ratelimit-limit-req-minute      your actual RPM ceiling
x-ratelimit-remaining-req-minute  what is left in this window
```

**A ceiling of `0` is an account problem, not a throttle**, and it is the most
confusing failure on this provider: the key authenticates fine, so you get
`429 Rate limit exceeded` (code 1300) rather than a `401`, which sends you
looking at your concurrency settings instead of your subscription. An
unactivated Experiment plan looks exactly like this. Activate it in the Admin
Console — phone verification, no card.

**Other providers**, setting both variables each:

| Provider | `LLM_BASE_URL` | Notes |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | No free tier, but publishes **no RPM, TPM or RPD** — only 2500 concurrency, the best-shaped limit available for this workload. ~$0.13/city, halved again by its $0.003/1M cache-hit input on our repeated system prompts. Thinking defaults to **on at `high`**: clear `LLM_BULK_REASONING_EFFORT` and send `extra_body={"thinking": {"type": "disabled"}}`, since `minimal` is not a value it accepts |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-3.5-flash` / `gemini-3.1-flash-lite` at `low` / `minimal`. Tier 1 costs ~$0.45/city. The **free tier cannot finish one pass** — 20 req/day, failing with `quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier` |
| Groq | `https://api.groq.com/openai/v1` | Fast, but **rejects** `reasoning_effort` — clear both settings or every call 400s. Free tier caps at 100k tokens/day on the 70B model, about a fifth of one city. Avoid models below ~30B; structured extraction degrades sharply |
| Cerebras | `https://api.cerebras.ai/v1` | 1M tokens/day free, but needs a verified payment method and caps free context at 8k — below `GRAPHITI_MAX_TOKENS` alone, so Graphiti fails |
| OpenAI | `https://api.openai.com/v1` | Paid |
| Ollama (self-hosted) | `http://host.docker.internal:11434/v1` | Unmetered, no key. Same size caveat as Groq |

**Not wired up, but the obvious next move:** Mistral's Batch API is 50%
cheaper *and* exempt from rate limits, which would remove the only real
constraint on the free plan. It needs a submit-and-poll rewrite of
`app/llm/client.py` rather than a config change, so it is out of scope here —
but the job registry added in §4.1 is already the right shape to hang it on.

`scripts/check_services.py` calls **both** models, because they fail
independently: a retired id in `LLM_BULK_MODEL` produces a run with no claims
and an empty graph while the judgement model answers fine.

Four settings cause confusing failures if ignored. The first two are the ones
that break a provider switch outright:

- **`LLM_REASONING_EFFORT`** must be **empty** on a model that does not
  implement it — which includes the default, `mistral-large-latest`. An
  OpenAI-compatible shim *rejects* a parameter it does not support rather than
  ignoring it, so a stray value fails every call with an opaque 400. The two
  effort settings therefore differ by *model*, not by role: Mistral Small 4 is
  a hybrid reasoning model and takes `none`, Large 3 takes nothing. On a
  thinking model (Gemini 3, OpenAI o-series) set it low rather than empty, or
  thinking tokens come out of the answer's budget and a long extraction prompt
  returns `""`.
  Do **not** set `high` on Mistral Small 4 expecting better claims: at `high`
  it returns `message.content` as a list of thinking-and-text chunks rather
  than a string. `app/llm/client._answer_text` now unwraps that, but every
  other consumer of a raw OpenAI response would not.
- **`GRAPHITI_SEMAPHORE_LIMIT`** bounds Graphiti's *own* concurrency, which
  `LLM_MAX_CONCURRENCY` does not govern. `graphiti-core` reads `SEMAPHORE_LIMIT`
  once at module scope and defaults to **20**. Graphiti is the largest consumer
  of calls in a run, so leaving it at 20 against a per-second limit puts the
  retry storm exactly where it hurts most. This is the setting people miss.
- **`LLM_MAX_CONCURRENCY`** bounds our thread pool during extraction and
  fact-checking. Keep it at or below the provider's request-rate allowance —
  **1** for Mistral's free plan (the default), **4–8** on a paid key or a
  per-day provider for roughly that much more speed. Retry with exponential
  backoff is built in, but a throttled run spends its time waiting rather than
  working, and bursts of retries are what a rate limiter punishes hardest.
- **`GRAPHITI_MAX_TOKENS`** needs headroom because Graphiti has no
  reasoning-effort knob. Too low shows up as a graph with no fact edges rather
  than as an error.

### 3.6 Tavily (optional)

Improves source discovery. Without a key the app falls back to
`ddgs`, which needs no credentials and returns noisier results.

```bash
TAVILY_API_KEY=tvly-...
```

Nothing else changes; the fallback is automatic and silent.

### 3.7 WHO GHO and World Bank Open Data (optional)

No deployment step, no account, no key — two public REST endpoints:

- `https://ghoapi.azureedge.net/api/{indicator}` — WHO Global Health
  Observatory, OData. Filtered by `$filter=SpatialDim eq 'IND'`.
- `https://api.worldbank.org/v2/country/{iso3}/indicator/{code}?format=json` —
  World Bank Indicators.

These serve `cv_burden` and `health_system`. All outbound HTTPS, so the only
infrastructure requirement is egress on 443.

```bash
ENABLE_OFFICIAL_DATA=true   # set false to demo web discovery on its own
```

Two operational facts worth knowing before they surprise you:

- **They are country-level.** A run needs `country` in the request body or
  this path is skipped with a warning. Everything it produces is flagged as
  national context, never as a city finding.
- **Indicator codes get archived, and the failure is quiet.** The World Bank
  returns **HTTP 200** with a message object where the data array should be —
  `SH.UHC.SRVS.CV.XD` does this today. WHO GHO can return HTTP 200 with zero
  rows for a country, which is why `WHS2_161` is not in the indicator list
  despite being the most on-topic name in the catalogue. `check_services.py`
  pulls a real value rather than checking a status code, precisely because a
  status code cannot see this.

### 3.8 Embedding model

No deployment step. `all-MiniLM-L6-v2` (384-dim, ~90 MB) is downloaded during
`docker build` and loaded lazily on first use, shared by the Milvus store and
by Graphiti's embedder and reranker.

If you run outside Docker, the first request downloads it from Hugging Face —
budget a minute and outbound HTTPS access.

---

## 4. Where to host

| Component | Where | Why |
|---|---|---|
| App + SQLite + Milvus Lite | One small VM, via `docker compose` | Milvus Lite is embedded, so there is no cluster to operate — fits comfortably in 2 GB |
| Neo4j | **Neo4j Sandbox**, hosted | Mandated by the case study, and not something you self-host |
| Milvus (alternative) | Zilliz Cloud free tier | Only `MILVUS_URI` and `MILVUS_TOKEN` change |

**VM options, cheapest first:**

- **Oracle Cloud Always Free** — an Ampere A1 instance gives up to 4 cores and
  24 GB RAM free indefinitely, enough to run full Milvus standalone next to the
  app. Caveats: ARM images, and capacity in popular regions can be hard to get.
- **Hetzner Cloud CX22** — roughly €4/month, x86, provisions in seconds. The
  most reliable option if a few euros is acceptable.
- **Fly.io / Railway / Render** — simplest if you would rather not manage a VM.
  Use a persistent volume for `/app/data`, and avoid free tiers that sleep
  after inactivity; a cold start during a live demo looks like an outage.

**Firewall.** Inbound: port 80 only. Outbound: HTTPS to the LLM provider,
Tavily and the sites being researched, plus **TCP 7687** to the Neo4j Sandbox —
that last one is the rule people forget on locked-down VPCs.

### 4.1 Railway (or any Dockerfile-reading PaaS)

Railway builds the [`Dockerfile`](../Dockerfile) directly and needs **no extra
services for the datastores**. That is the whole point of §1: SQLite and Milvus
Lite are libraries writing files, not servers, so the `docker-compose.yml`
topology collapses to a single Railway service plus a volume. Do **not** port
`docker-compose.milvus.yml` across — that would mean three more Railway
services (etcd, MinIO, milvus) and ~3 GB of RAM to get identical query results.

1. **Volume.** Attach one at mount path `/app/data`. Without it the SQLite file
   and the Milvus Lite database are recreated empty on every deploy, so every
   researched city is lost on the next push. Free and Trial plans cap volumes
   at 0.5 GB; Hobby gives 5 GB, which is the realistic floor here because the
   passage embeddings grow per city.
2. **Variables.** Everything from `.env.example` goes in the service variables
   — there is no `.env` file in the image, and `load_dotenv()` simply finds
   nothing and falls through to the real environment. The three that pin
   storage to the volume:

   ```bash
   DATA_DIR=/app/data
   DATABASE_URL=sqlite:////app/data/cardio4cities.db   # four slashes = absolute
   MILVUS_URI=/app/data/milvus.db
   ```

   Plus `LLM_API_KEY`, `NEO4J_URI` and `NEO4J_PASSWORD`, exactly as on a VM.
3. **Port.** Railway injects `PORT` and routes to it. The image defaults
   `PORT=8000` and the start command expands it, so this works either way —
   but a hardcoded `--port 8000` is the usual cause of a service that builds
   green and then never answers.
4. **Healthcheck.** Railway ignores the Docker `HEALTHCHECK` instruction and
   uses its own; [`railway.json`](../railway.json) points it at `/health` with
   a 300 s timeout to cover image start-up.

Three things behave differently here than on a VM:

- **Redeploys are not zero-downtime.** A service with a volume cannot run two
  active deployments against it, so every push has a short gap. This is a
  feature, not a limit to work around: SQLite's single-writer lock and Milvus
  Lite's file lock both assume exactly one process.
- **Replicas are unavailable** with a volume attached, and `numReplicas` is
  pinned to 1 for that reason. Scaling out means moving off both embedded
  stores — Railway Postgres (see §3.2) and Zilliz Cloud (§3.3 option C) — at
  which point the volume can go away entirely.
- **Sleeping is fatal to a demo.** `PREWARM_EMBEDDINGS=true` (the default)
  loads the model during startup rather than on the first request, so a warm
  service answers immediately — but a service that has been asleep pays the
  whole cold start, container plus model, while someone is watching. Keep it
  always-on.

**What no longer applies here, and why it is worth knowing.** Railway closes an
HTTP request after **5 minutes with no bytes transferred** and caps even a
chatty one at **15 minutes**. That used to make this host unusable for a real
run, because `POST /research` held the request open for the full 10–20 minutes
and then returned the brief: the browser got a 502 while the container quietly
finished the work, and the deliverable existed but was unreachable. `/research`
now registers a job and returns an id in milliseconds, so no request is ever
long enough for a proxy deadline to apply — on Railway, behind nginx, or
through a corporate proxy. See [`app/jobs.py`](../app/jobs.py) and §6.

**Railway Postgres**, if you want it instead of SQLite: the injected
`DATABASE_URL` is `postgresql://…`, which SQLAlchemy 2.0 resolves to psycopg2 —
not in `requirements.txt`. Add `psycopg2-binary` before switching, or the first
store construction fails with `ModuleNotFoundError: psycopg2`.

Render and Fly.io differ only in the names: a persistent disk mounted at
`/app/data`, the same three storage variables, and the same request-duration
question to check before trusting a live `/research` call.

---

## 5. Running without Docker

For development, or on a machine where Docker is not available.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

`RUN_MODE=MOCK` needs no keys and no external service at all — it exercises the
full nine-node graph against canned data, which is what the test suite uses:

```bash
RUN_MODE=MOCK pytest -v
```

On Windows, either use `RUN_MODE=MOCK` (the mock vector store is in-memory) or
point `MILVUS_URI` at a server, since Milvus Lite has no Windows wheel.

---

## 6. Operating it

**Verify a deployment** in this order — each step isolates a different
dependency:

```bash
curl http://<host>/health                       # app up; per-store reachability
docker compose exec app python -m scripts.check_services   # both models, Neo4j, Milvus, search

# Research returns a job id immediately; poll it for progress and the brief.
JOB=$(curl -sX POST http://<host>/research \
       -H 'Content-Type: application/json' \
       -d '{"city":"Pune","country":"India"}' | jq -r .job_id)

# `wait` holds the response open until the job finishes, up to 60s, so this
# loop costs one request a minute rather than one a second.
#
# Test for the TERMINAL states, not for "running": a job is `queued` until a
# slot frees up, so `while status = running` falls straight through on a busy
# server and the curl below prints a job with no result in it.
while :; do
  STATUS=$(curl -s "http://<host>/research/$JOB?wait=60" | jq -r .status)
  case "$STATUS" in succeeded|failed|cancelled) break ;; esac
done
curl -s "http://<host>/research/$JOB" | jq '.llm_usage, .result.counts'
```

`llm_usage` is worth reading on the first run of a new provider or model: it
reports calls and tokens **per model**, which is the only way to confirm the
bulk calls are actually going to `LLM_BULK_MODEL`. A run showing all ~150 calls
against `LLM_MODEL` means the split is misconfigured and the bill is roughly
six times what it should be.

**Persistence.** Everything durable lives in the `app-data` volume and in the
Neo4j Sandbox. `docker compose down` keeps the volume; `docker compose down -v`
destroys it.

**Upgrades.** `docker compose up -d --build` rebuilds and replaces the
container; the volume and the Sandbox are untouched.

**Secrets.** `.env` is gitignored and excluded from the Docker build context,
and is read at runtime via `env_file`. No key has a hardcoded default in
`app/config.py` — a missing one raises a clear error on first use instead of
silently falling back to someone else's account.

---

## 7. Troubleshooting

Every external dependency has a defined degraded mode, so most failures show up
as a thinner brief with a **Run Warnings** section rather than as an error.
Read the warnings first — they name the dependency that failed. The full table
of degraded modes is in
[ARCHITECTURE.md](../ARCHITECTURE.md#behaviour-when-the-outside-world-is-unavailable).

| Symptom | Cause | Fix |
|---|---|---|
| `POST /research` returns immediately with no brief | Working as designed — it returns a `job_id`, because a 10–20 minute request does not survive any proxy in the path. Poll `GET /research/{job_id}` | Nothing to fix. `GET /research` lists recent jobs if you lost the id |
| A run is slow and you want to know where | `GET /research/{job_id}` reports every node's state, elapsed seconds and what it produced — the same measurement the log prints | If `graph_writer` is the slow one that is expected: Graphiti re-extracts entities for every fact. Raise `GRAPH_WRITE_CONCURRENCY`, or use the demo profile in `.env.example` §Cost |
| A job disappeared with a 404 | Finished jobs are kept for `JOB_RETENTION_SECONDS` (1h), and a restart forgets in-flight ones | The brief is stored permanently — `GET /report/{city}`. Only the progress detail expires |
| `llm_usage` shows every call against one model | The two-model split is misconfigured — `LLM_BULK_MODEL` is unset or equal to `LLM_MODEL` | Set both. On the Mistral defaults this is roughly a 3x difference in the bill for identical JSON |
| The very first run is slow before any node finishes | `sentence-transformers` downloads `all-MiniLM-L6-v2` (~90 MB) lazily on the first embed | One-off. It is cached afterwards; pre-warm with `python -c "from app.llm import embeddings; embeddings.get_model()"` |
| `/health` reports `graph: unreachable`; `/graph/{city}` returns 503 | Sandbox expired or was restarted | Re-copy **both** the Bolt URI and the password from sandbox.neo4j.com |
| Research completes but the brief is nearly all Gaps | LLM returning empty or malformed output | Check `LLM_API_KEY`; raise `LLM_MAX_TOKENS`. On a *thinking* model also set `LLM_REASONING_EFFORT=low`, since thinking tokens can consume the whole answer budget |
| Run Warnings say *"No source could be read because 'beautifulsoup4' is not installed"* | The HTML parser is missing from the environment running uvicorn, so no page can be parsed | `pip install -r requirements.txt` **in the interpreter uvicorn is using**. Confirm with `python scripts/check_services.py`, whose first check is now the import list |
| A **"Could not read \<url\>"** warning for every single source | Same cause as above, on a build predating the single-warning check | As above. If the errors differ per URL it is genuinely the sites, not you |
| `XMLParsedAsHTMLWarning: It looks like you're using an HTML parser to parse an XML document` | An RSS/Atom feed or sitemap served as `text/xml`, which the old substring content-type check let through to the HTML parser | Fixed: content types are matched exactly and XML gets `lxml-xml`. If it still appears, a server is mislabelling XML as `text/html` — the warning is then correct and worth reading, which is why it is **not** filtered |
| Warnings saying *"unsupported content type"* | Working as designed. PDFs, CSVs and JSON are refused rather than fed to the model as prose | Nothing to fix. Many government portals publish as PDF; supporting them is future work, not a failure |
| Warnings saying *"only N characters of readable text"* | The page parsed but is essentially empty — a JS-rendered shell, a login wall, or a redirect stub | Nothing to fix. Dropping it saves a model call that could only have returned nothing |
| 429 with `'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'` and `'quotaValue': '20'` | A Gemini **free-tier** key. `gemini-3.5-flash` allows 20 requests/day; one city needs 150–250 | Switch to Mistral (the default), whose free plan is throttled on rate but gives 1B tokens/month, or link Gemini billing. `RUN_MODE=MOCK` needs no key at all |
| Every LLM call fails with an opaque 400 right after switching provider | A reasoning-effort setting is populated for a model that doesn't implement it. Unimplemented parameters are **rejected**, not ignored | Clear the setting for *that role*. The two are separate because `mistral-small-latest` takes `none` while `mistral-large-latest` takes nothing at all |
| Claims and search queries come back empty on a model that is definitely answering | A reasoning model returned `message.content` as a list of thinking/text chunks rather than a string, so `json.loads` saw nothing usable. Happens on `mistral-small-latest` at `reasoning_effort=high` | Use `none`. `app/llm/client._answer_text` unwraps the chunk list defensively, so this should not reach you — if it does, a provider is using a chunk shape it does not describe |
| 401 / "API key not valid" right after switching provider | Keys are per-provider; the base URL changed but the key didn't | Issue a key from the provider that owns `LLM_BASE_URL`. `check_services.py` now says this explicitly |
| Mistral returns `429 Rate limit exceeded` (code 1300) on the **very first** call, and waiting does not help | Not a throttle. Check the response header `x-ratelimit-limit-req-minute` — if it reads **`0`**, the account has no completions allowance at all. The key authenticates, which is why this presents as 429 rather than 401 | Activate the free Experiment plan in the Mistral Admin Console (phone verification, no card), or check whether the Organization/Workspace monthly spending limit is set to zero. No code change will fix it |
| Mistral returns `403 tier_not_allowed` (code 1910) for `mistral-large-latest` | Large 3 is **not served on the free tier**. It is also absent from `GET /v1/models` for a free key | Use `mistral-medium-latest` (the default). List what your key can actually reach with `curl https://api.mistral.ai/v1/models -H "Authorization: Bearer $LLM_API_KEY"` |
| Constant 429s and `Retrying _generate_response_with_retry after N attempts` | Concurrency exceeds the provider's allowance. Graphiti is the biggest caller and has its **own** limit | Set `LLM_MAX_CONCURRENCY=1` **and** `GRAPHITI_SEMAPHORE_LIMIT=1`. The second one is the one people miss — `graphiti-core` defaults to 20 concurrent calls. `llm_usage.failures` on the job counts the retries |
| Quota exhausts partway, then the brief fills with off-topic sources | Query planning fell back to keyword templates once the LLM stopped answering, so searches went generic. Cascade, not a search bug | Fix the quota first, then re-run. The irrelevant URLs are a symptom |
| Brief warns *"Query planning fell back to keyword templates"* | The LLM was unreachable on that pass | Working as designed — the run continued on generic searches. Check the provider and the key |
| Brief is entirely Gaps citing *"Search returned no candidate sources"* | No outbound internet, or the search provider is blocked | **Read the Run Warnings first** — a provider failure now names itself and the exception type there. Silence means search really did return nothing |
| Run Warnings say *"DuckDuckGo search failed (ImportError...)"* | The `duckduckgo-search` package was renamed; an old virtualenv still has the pre-rename name | `pip install -U ddgs` |
| Run Warnings say *"DuckDuckGo search failed (... Ratelimit)"* | `ddgs` scrapes consumer engines and a pass issues ~10 queries back to back | Set `TAVILY_API_KEY`, or lower `QUERIES_PER_DIMENSION` |
| Sources tab is **empty** after a run | Discovery failed, so the LLM was never reached | This is a search problem, not a quota problem. Sources tab populated but no facts is the opposite case |
| Brief has facts, but `/graph/{city}` and the Knowledge graph tab are empty | Graph writes failed and were downgraded to a warning | Read the Run Warnings. `TypeError`/`AttributeError` means a `graphiti-core` version mismatch; anything else is the Sandbox |
| Neo4j logs *"property key does not exist"* for `episodes` / `fact_embedding` | Advisory only, but it means **no fact edges exist** — those keys only appear once an edge is written | Run `check_services`; if nodes > 0 and fact edges = 0, the writes are failing. `reference_time` warns permanently and can be ignored |
| `check_services` shows nodes but **0 fact edges** | Entity extraction returned nothing, or writes failed | Raise `GRAPHITI_MAX_TOKENS`; a thinking model can spend the whole budget reasoning and return empty |
| Brief warns *"Skipped official statistics: ... no country was supplied"* | WHO GHO and the World Bank are keyed by country | Send `country` in the request body — the UI form has the field |
| Brief warns *"publishes no usable {code} value"* | That indicator has been archived, or has no rows for this country | Working as designed; the dimension still has its other indicators and the web path. Replace the code in `official_data_agent.py` if it is permanent |
| No WHO or World Bank facts at all, no warning either | `ENABLE_OFFICIAL_DATA` is false, or neither served dimension was in focus | Check `.env`; the path only runs for `cv_burden` and `health_system` |
| Run is very slow, logs show retries | Provider rate limiting | Lower `LLM_MAX_CONCURRENCY` to 2–3. Note this does **not** throttle the crawler any more — that is `HTTP_MAX_CONCURRENCY`, which costs no tokens |
| `DataNotMatchException: {id} field should be a int64` | A `city_passages` collection from an earlier build has an Int64 primary key; passage ids are URL hashes | Fixed automatically now — the store validates the schema on init and recreates a mismatched collection. If you are on older code, delete `./data/milvus.db` |
| Startup logs *"Milvus collection ... is unusable"* | The self-repair above just ran | Informational. Passage embeddings for already-researched cities were dropped and rebuild on the next run for those cities |
| `FutureWarning: get_sentence_embedding_dimension ... renamed` | sentence-transformers 5.x renamed the method | Fixed; `embedding_dim()` now prefers `get_embedding_dimension` and falls back |
| Log floods with *"Retrying _generate_response_with_retry"* | Graphiti's own entity extraction is being rate-limited | **This is the LLM provider, not Neo4j.** Graphiti runs several model calls per fact, so it is usually the largest LLM consumer and first to hit a free-tier quota. Check the Run Warnings for the fact-level count |
| `ModuleNotFoundError: milvus_lite` | Native Windows | Run in Docker or WSL2, or point `MILVUS_URI` at a server |
| First request hangs ~60s | Embedding model loading, which now happens at startup instead | Set `PREWARM_EMBEDDINGS=true` (the default). With it off, this is expected once per container start |
| Many sources DENIED | robots.txt or the ToS denylist | Working as intended — `/sources/{city}` lists each URL with its reason |
| Container restart loop | Bad `.env` | `docker compose logs app`; stores are constructed lazily, so this is nearly always a parse error in `.env` itself |
