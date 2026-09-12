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
| 5 | **LLM provider** (Mistral by default) | Query planning, claim extraction, fact-check adjudication, narrative, `/ask` | Hosted, any OpenAI-compatible chat endpoint | **Yes** in LIVE mode | Free tier is enough — but see §3.5, the choice is throughput-bound |
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
  A -.https.-> L[LLM provider<br/>Mistral / Gemini / Groq / OpenAI]
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

**Choose on throughput, not capability.** A single city costs 150–250 calls
and 400–600k tokens: roughly 80 of ours plus Graphiti's entity extraction,
which runs several calls per fact and is usually the larger half. Free tiers
meter that two different ways, and only one is survivable — a per-second limit
just makes a batch job slower, while a per-day cap stops it dead until
tomorrow.

| Provider | `LLM_BASE_URL` | `LLM_MODEL` | Free-tier ceiling | Runs |
|---|---|---|---|---|
| **Mistral** (default) | `https://api.mistral.ai/v1` | `mistral-small-latest` | ~1 req/**sec**, 500k TPM, 1B tok/month | ~2,000/month |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash-lite` | 1,000 req/**day** | ~4/day |
| Google Gemini | as above | `gemini-3.x` previews | **20 req/day** | cannot finish one pass |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | 1,000 RPD but **100k tok/day** | ~¼ of one city |
| Groq | as above | `llama-3.1-8b-instant` | 14,400 RPD but **6k TPM** | one page body exceeds a minute's budget |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-flash` | paid | — |
| OpenAI | `https://api.openai.com/v1` | `gpt-4.1-mini` | paid | — |
| Ollama (self-hosted) | `http://host.docker.internal:11434/v1` | any local model | unmetered | no key needed; structured extraction degrades sharply below ~30B |

If you would rather pay than migrate, Gemini Tier 1 on `gemini-2.5-flash-lite`
($0.10/1M in, $0.40/1M out) works out to roughly **$0.10 per city**.

Four settings cause confusing failures if ignored. The first two are the ones
that break a provider switch outright:

- **`LLM_REASONING_EFFORT`** must be **empty** on providers that do not
  implement it — which includes the default. An OpenAI-compatible shim
  *rejects* a parameter it does not support rather than ignoring it, so a
  stray value fails every call with an opaque 400. Set it to `low` only on a
  thinking model (Gemini 3, OpenAI o-series), where thinking tokens otherwise
  come out of the answer's budget and a long extraction prompt returns `""`.
- **`GRAPHITI_SEMAPHORE_LIMIT`** bounds Graphiti's *own* concurrency, which
  `LLM_MAX_CONCURRENCY` does not govern. `graphiti-core` reads `SEMAPHORE_LIMIT`
  once at module scope and defaults to **20**. Graphiti is the largest consumer
  of calls in a run, so leaving it at 20 against a per-second limit puts the
  retry storm exactly where it hurts most. This is the setting people miss.
- **`LLM_MAX_CONCURRENCY`** bounds our thread pool during extraction and
  fact-checking. Keep it at or below the provider's requests-per-second
  allowance — **1** for Mistral's free plan, **8** on a per-day provider for
  roughly 8× the speed. Retry with exponential backoff is built in, but a
  throttled run spends its time waiting rather than working.
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
docker compose exec app python -m scripts.check_services   # LLM, Neo4j, Milvus, search
curl -X POST http://<host>/research \
     -H 'Content-Type: application/json' \
     -d '{"city":"Pune","country":"India"}'     # end-to-end, ~1-3 min
```

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
| `POST /research` appears to hang — nothing logged after `GET /cities` | Normally not a hang: a thorough run is 10–20 minutes and uvicorn only logs a request once it *completes*. `LOG_LEVEL=INFO` now prints each node with its duration | Watch the node log. If `graph_writer` is the slow one that is expected — Graphiti re-extracts entities for every fact. Use the demo profile in `.env.example` §Cost |
| The very first run is slow before any node finishes | `sentence-transformers` downloads `all-MiniLM-L6-v2` (~90 MB) lazily on the first embed | One-off. It is cached afterwards; pre-warm with `python -c "from app.llm import embeddings; embeddings.get_model()"` |
| `/health` reports `graph: unreachable`; `/graph/{city}` returns 503 | Sandbox expired or was restarted | Re-copy **both** the Bolt URI and the password from sandbox.neo4j.com |
| Research completes but the brief is nearly all Gaps | LLM returning empty or malformed output | Check `LLM_API_KEY`; raise `LLM_MAX_TOKENS`. On a *thinking* model also set `LLM_REASONING_EFFORT=low`, since thinking tokens can consume the whole answer budget |
| Run Warnings say *"No source could be read because 'beautifulsoup4' is not installed"* | The HTML parser is missing from the environment running uvicorn, so no page can be parsed | `pip install -r requirements.txt` **in the interpreter uvicorn is using**. Confirm with `python scripts/check_services.py`, whose first check is now the import list |
| A **"Could not read \<url\>"** warning for every single source | Same cause as above, on a build predating the single-warning check | As above. If the errors differ per URL it is genuinely the sites, not you |
| `XMLParsedAsHTMLWarning: It looks like you're using an HTML parser to parse an XML document` | An RSS/Atom feed or sitemap served as `text/xml`, which the old substring content-type check let through to the HTML parser | Fixed: content types are matched exactly and XML gets `lxml-xml`. If it still appears, a server is mislabelling XML as `text/html` — the warning is then correct and worth reading, which is why it is **not** filtered |
| Warnings saying *"unsupported content type"* | Working as designed. PDFs, CSVs and JSON are refused rather than fed to the model as prose | Nothing to fix. Many government portals publish as PDF; supporting them is future work, not a failure |
| Warnings saying *"only N characters of readable text"* | The page parsed but is essentially empty — a JS-rendered shell, a login wall, or a redirect stub | Nothing to fix. Dropping it saves a model call that could only have returned nothing |
| 429 with `'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'` and `'quotaValue': '20'` | A Gemini 3.x **preview** model is selected. Those allow 20 requests/day; one city needs 150–250 | Switch to a provider metered per *second* rather than per *day* — see the table in `.env.example`. The shipped default (Mistral Small, free Experiment plan) is ~1 RPS with 1B tokens/month |
| Every LLM call fails with an opaque 400 right after switching provider | `LLM_REASONING_EFFORT` is set on a provider that doesn't implement it. Unimplemented parameters are **rejected**, not ignored | Clear `LLM_REASONING_EFFORT` in `.env`. Only set it on thinking models (Gemini 3, OpenAI o-series) |
| 401 / "API key not valid" right after switching provider | Keys are per-provider; the base URL changed but the key didn't | Issue a key from the provider that owns `LLM_BASE_URL`. `check_services.py` now says this explicitly |
| Constant 429s and `Retrying _generate_response_with_retry after N attempts` | Concurrency exceeds the provider's requests-per-second allowance. Graphiti is the biggest caller and has its **own** limit | Set `LLM_MAX_CONCURRENCY=1` **and** `GRAPHITI_SEMAPHORE_LIMIT=1`. The second one is the one people miss — `graphiti-core` defaults to 20 concurrent calls |
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
| Run is very slow, logs show retries | Provider rate limiting | Lower `LLM_MAX_CONCURRENCY` to 2–3 |
| `DataNotMatchException: {id} field should be a int64` | A `city_passages` collection from an earlier build has an Int64 primary key; passage ids are URL hashes | Fixed automatically now — the store validates the schema on init and recreates a mismatched collection. If you are on older code, delete `./data/milvus.db` |
| Startup logs *"Milvus collection ... is unusable"* | The self-repair above just ran | Informational. Passage embeddings for already-researched cities were dropped and rebuild on the next run for those cities |
| `FutureWarning: get_sentence_embedding_dimension ... renamed` | sentence-transformers 5.x renamed the method | Fixed; `embedding_dim()` now prefers `get_embedding_dimension` and falls back |
| Log floods with *"Retrying _generate_response_with_retry"* | Graphiti's own entity extraction is being rate-limited | **This is the LLM provider, not Neo4j.** Graphiti runs several model calls per fact, so it is usually the largest LLM consumer and first to hit a free-tier quota. Check the Run Warnings for the fact-level count |
| `ModuleNotFoundError: milvus_lite` | Native Windows | Run in Docker or WSL2, or point `MILVUS_URI` at a server |
| First request hangs ~60s | Embedding model loading | Expected once per container start; the healthcheck's 40s start period covers it |
| Many sources DENIED | robots.txt or the ToS denylist | Working as intended — `/sources/{city}` lists each URL with its reason |
| Container restart loop | Bad `.env` | `docker compose logs app`; stores are constructed lazily, so this is nearly always a parse error in `.env` itself |
