---
marp: true
paginate: true
title: CARDIO4Cities — City Intelligence
---

<!--
Seven slides, in the order the brief asks for: problem, solution and
architecture, trust and evidence, user experience, trade-offs and limitations.

Renders as slides with Marp (`npx @marp-team/marp-cli docs/PRESENTATION.md -o deck.pdf`)
and reads fine as a document without it.
-->

# CARDIO4Cities — City Intelligence

Preparing a City Lead for a stakeholder meeting in a city nobody has researched.

Live internet research → independent verification → a reusable, evidence-linked
intelligence asset.

---

## 1 · The problem, as I understand it

A City Lead meets health officials in a city the team has never researched.
Today that means days of manual work across government portals, reports,
statistics and policy documents, producing knowledge that is fragmented,
hard to verify, and not reusable by the next person.

The naive AI answer — "ask a model about the city" — fails in the one way that
matters most here:

> **A confident paragraph of invented context is worse than a blank section,
> because it gets repeated in a meeting with a health minister.**

So the design goal is not completeness. It is **honesty under time pressure**:
every statement traceable, and everything unknown said out loud.

---

## 2 · What "understanding a city" is decomposed into

Five fixed dimensions, matching what the Lead actually needs in the room:

| | |
|---|---|
| **Burden** | hypertension / diabetes / lipid prevalence, mortality, screening coverage |
| **Programmes** | what is running, who runs it, since when |
| **Policy** | NCD plans, municipal strategies, regulation, budget |
| **Stakeholders** | department, ministries, hospitals, universities, NGOs, funders |
| **System** | primary care capacity, workforce, medicines, financing |

**Fixed, not generated per city.** A generated set fits each city better and
makes no two cities comparable. Fixing it is what lets the system say
*"we know nothing about policy here"* — a sentence that requires knowing policy
was in scope.

**"Opportunities and risks" is deliberately not a dimension.** That is the
Lead's judgement. The honest machine contribution is the gap log.

---

## 3 · The agentic workflow

LangGraph, nine nodes, one conditional edge.

```
planner → query_gen → search → crawlability → extraction
                                                   ↓
                        report ← coverage ← graph_writer ← fact_check
                                    ↑ insufficient: re-plan the empty dimensions
                                      (bounded by MAX_PLANNER_RETRIES)
```

- **One** conditional edge, on purpose. Branching claims two paths are different
  work; elsewhere the variation is in what a node *finds*, not what happens next.
- The crawlability gate is **not** a branch — extraction filters to ALLOWED URLs
  before issuing a request. A structural guarantee beats a routing rule a later
  edit could bypass.
- Retries **narrow**: the planner re-plans only the dimensions that came back
  empty, so pass two is cheaper and targeted.
- Every node is **idempotent under retry**, because state accumulates.

---

## 4 · Trust and evidence — three mechanisms

**1 · Structural independence.** The fact-checker gets the claim plus passages
from *other domains*, labelled `[S1]`, `[S2]`. It is never told which passage
produced the claim. Same-domain passages are excluded — a site agreeing with
itself is not corroboration.

**2 · Cited provenance, not asserted provenance.** The checker must *name* the
labels it relied on. Labels map back to real URLs; a label never offered is
discarded. So `corroborating_urls` holds only sources actually cited.

**3 · Derived tiers.** `VERIFIED` is recomputed from distinct supporting
domains. A claim cannot reach VERIFIED without the required independent sources
regardless of what tier the model asked for — and the downgrade is written into
the fact's reasoning, visible to the reader.

`UNSUPPORTED` has a **consequence**: those claims are filtered out of
persistence and rewritten as gaps. They cannot physically reach a store as facts.

---

## 5 · Data architecture — three stores, three questions

| Store | Question it answers | Why not the others |
|---|---|---|
| **SQLite** | *Was this sourced legitimately, when, by what decision?* | Exact row-level audit. Keeps **denied** sources too — that is how you can tell the gate ran. |
| **Milvus** | *What is being done about X?* | Fuzzy recall over material the schema never anticipated. Raw passages only; fact status should not be decided by vector distance. |
| **Neo4j + Graphiti** | *How do these things relate, and what changed?* | Temporal edges: a superseded fact gains `invalid_at` instead of being overwritten. Institutional memory over time. |

Read at query time, not just written: `/graph/{city}` and `/ask` both traverse
the graph through Graphiti's hybrid search.

**Embeddings run locally** (`all-MiniLM-L6-v2`), serving both Milvus and
Graphiti's embedder *and* cross-encoder — so the graph is decoupled from the
chat provider and there is no second API key.

---

## 6 · User experience

One URL. The pipeline is the left rail, so a non-technical user can see the
work being done rather than watching a spinner.

- **Report** — grouped by dimension (how you read before a meeting), with the
  confidence tier on **every** fact and its sources under it. Downloadable `.md`.
- **Sources** — every URL considered and the gate's verdict, refusals included.
- **Knowledge graph** — relationships with their validity windows.
- **Ask** — answers strictly from indexed evidence, with inline `[1]` citations
  back to the URL or graph edge; refuses when the evidence does not support one.
- **Already researched** — click a city to reopen its stored brief. The second
  person to ask should not pay for the research again.

Colour carries confidence consistently: green verified, amber single-source,
orange conflicting, red gap.

---

## 7 · Trade-offs, and what I cut

**Cut on purpose**
- **No human review queue** — the brief flags what needs review and the audit
  table has the column, but nothing blocks. The reader *is* the domain expert.
- **No streaming progress** — nicer demo, zero extra trust, real SSE plumbing.
- **No PDF extraction** — rejected with a recorded warning rather than feeding
  bytes to an LLM. My biggest recall limitation, and the first thing I would add.
- **No dedicated NER pass or cross-encoder** — Graphiti extracts entities;
  reranking is cosine over the same bi-encoder.

**Honest limitations**
- Coverage measures *presence* per dimension, not depth.
- Without a Tavily key, search is noisier — which shows up as more UNSUPPORTED
  verdicts, not as wrong facts. That is the failure mode I wanted.
- A Neo4j Sandbox expires in 3–10 days; a durable deployment needs Aura.

**If I had another two days:** PDF/table extraction, a real cross-encoder,
a freshness policy driving re-research, and per-node streaming.
