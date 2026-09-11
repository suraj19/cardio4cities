# Deployment

Everything needed to stand this up, from a laptop smoke test to a public demo
URL. [README.md](../README.md) covers what the application does;
[ARCHITECTURE.md](../ARCHITECTURE.md) covers why it is built this way.

---

## 1. Dependent services at a glance

Seven things the application talks to. Only **two** are mandatory for a live
demo, and only **one** of the seven is a server you have to operate yourself.

| # | Service | Role | Runs where | Required? | Cost |
|---|---|---|---|---|---|
| 1 | **Application container** (FastAPI + UI) | Serves the API *and* `frontend/index.html` from one port | Your VM, via `docker compose` | **Yes** | VM cost only |
| 2 | **SQLite** | Relational store — source registry, fact audit trail, gaps, saved briefs | Embedded. A file on the app's volume | **Yes** | Free |
| 3 | **Milvus** | Vector store — semantic recall over extracted passages | Embedded (Milvus Lite, default), or a standalone cluster, or Zilliz Cloud | **Yes** — but Lite needs no server | Free (Lite) |
| 4 | **Neo4j Graph Database Sandbox** | Temporal knowledge graph, written through Graphiti | Hosted by Neo4j at [sandbox.neo4j.com](https://sandbox.neo4j.com) | **Yes** in LIVE mode | Free, **expires** |
| 5 | **LLM provider** (Gemini by default) | Query planning, claim extraction, fact-check adjudication, narrative, `/ask` | Hosted, any OpenAI-compatible chat endpoint | **Yes** in LIVE mode | Free tier is enough |
| 6 | **Tavily** | Higher-quality search discovery | Hosted | No — falls back to DuckDuckGo | Free tier |
| 7 | **sentence-transformers** (`all-MiniLM-L6-v2`) | Local embeddings, shared by Milvus and Graphiti | In-process, baked into the image | **Yes** | Free, no key |

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
  A -.https.-> L[LLM provider<br/>Gemini / DeepSeek / Groq / OpenAI]
  A -.https.-> T[Tavily or DuckDuckGo]

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

### 3.5 LLM provider

Any OpenAI-compatible chat endpoint. Only chat completions are used — no
embeddings endpoint is required, which is why providers without one still
work.

| Provider | `LLM_BASE_URL` | `LLM_MODEL` | Notes |
|---|---|---|---|
| **Google Gemini** (default) | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-3.5-flash` | Free tier; strongest JSON adherence, which the graph layer depends on. Key from [AI Studio](https://aistudio.google.com/apikey) |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-flash` | Cheapest paid option |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | Fastest. Clear `LLM_REASONING_EFFORT` — it rejects the parameter |
| OpenAI | `https://api.openai.com/v1` | `gpt-4.1-mini` | |
| Ollama (self-hosted) | `http://host.docker.internal:11434/v1` | any local model | No key needed; quality of structured extraction drops sharply below ~30B |

Two provider-specific settings that cause confusing failures if ignored:

- **`LLM_MAX_CONCURRENCY`** bounds parallel calls during extraction and
  fact-checking. The Gemini free tier wants this at **2 or 3**; the default of
  8 will hit rate limits. Retries with exponential backoff are built in, but
  they cost wall-clock time.
- **`LLM_REASONING_EFFORT=low`.** Gemini 3 always thinks, and thinking tokens
  come out of the same budget as the answer. Left unbounded, a long extraction
  prompt can spend the whole budget reasoning and return an empty string.

### 3.6 Tavily (optional)

Improves source discovery. Without a key the app falls back to
`duckduckgo-search`, which needs no credentials and returns noisier results.

```bash
TAVILY_API_KEY=tvly-...
```

Nothing else changes; the fallback is automatic and silent.

### 3.7 Embedding model

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
| `/health` reports `graph: unreachable`; `/graph/{city}` returns 503 | Sandbox expired or was restarted | Re-copy **both** the Bolt URI and the password from sandbox.neo4j.com |
| Research completes but the brief is nearly all Gaps | LLM returning empty or malformed output | Check `LLM_API_KEY`; raise `LLM_MAX_TOKENS`; confirm `LLM_REASONING_EFFORT=low` |
| Brief warns *"Query planning fell back to keyword templates"* | The LLM was unreachable on that pass | Working as designed — the run continued on generic searches. Check the provider and the key |
| Brief is entirely Gaps citing *"Search returned no candidate sources"* | No outbound internet, or the search provider is blocked | Check egress from the VM; confirm `TAVILY_API_KEY` if set |
| Run is very slow, logs show retries | Provider rate limiting | Lower `LLM_MAX_CONCURRENCY` to 2–3 |
| `ModuleNotFoundError: milvus_lite` | Native Windows | Run in Docker or WSL2, or point `MILVUS_URI` at a server |
| First request hangs ~60s | Embedding model loading | Expected once per container start; the healthcheck's 40s start period covers it |
| Many sources DENIED | robots.txt or the ToS denylist | Working as intended — `/sources/{city}` lists each URL with its reason |
| Container restart loop | Bad `.env` | `docker compose logs app`; stores are constructed lazily, so this is nearly always a parse error in `.env` itself |
