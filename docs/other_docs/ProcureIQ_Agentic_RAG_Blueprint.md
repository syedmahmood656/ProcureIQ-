# ProcureIQ — Autonomous Procurement Contract Intelligence & Supplier Risk Agent
### A Flagship Agentic RAG Portfolio Project (Design Blueprint)

---

## 0. Why this use case (and not a generic "chat with your PDFs" bot)

Every large enterprise (retail, manufacturing, pharma, tech) runs procurement through two disconnected worlds:

- **Unstructured legal knowledge**: thousands of vendor contracts (MSAs, SOWs, NDAs) sitting in a document repository, full of clauses on auto-renewal, liability caps, termination-for-convenience, indemnification, and SLAs.
- **Structured operational data**: an ERP/procurement system (SAP Ariba, Coupa, Oracle) tracking spend, purchase orders, vendor master data, and payment terms.

Procurement, legal, and finance teams manually cross-reference these two worlds — "which vendors with >$500K annual spend have contracts auto-renewing next quarter with non-standard liability terms, and has anything happened to those vendors in the news recently?" — a query that today takes an analyst hours of clicking between a contract repository, an ERP report, and Google. This is exactly the class of **multi-hop, multi-source reasoning problem** that a naive single-index RAG chatbot cannot solve, but an **agentic system with tool use, structured data access, and external monitoring** can.

This makes it a stronger portfolio piece than a generic support bot or a single-PDF Q&A app, because it forces you to build (and defend in an interview) hybrid retrieval, text-to-SQL, multi-tool orchestration, guardrails against untrusted document content, and measurable business ROI — the exact things a Staff/Principal AI engineer interview panel probes.

---

## 1. Project Title & Business Value

**ProcureIQ** — an agent that answers natural-language questions spanning contract text, spend data, and live supplier risk signals, and returns a structured, cited "Risk & Exposure Report" rather than a free-text answer.

**Example queries it should handle:**
- "List vendors with contracts expiring in Q1 2027 that have auto-renewal clauses and a liability cap below our $1M standard. What's our total annual spend with them?"
- "Are any of our top-20 suppliers by spend showing financial distress signals or adverse news in the last 90 days?"
- "Compare the indemnification terms across our three cloud infrastructure vendors and flag which one deviates most from our legal playbook."

**Enterprise ROI you can credibly claim on a resume/portfolio (frame as target/benchmarked metrics from your own evaluation, not fabricated production numbers):**
| Metric | Manual Process (baseline) | ProcureIQ (measured in your eval harness) |
|---|---|---|
| Time to answer a cross-domain contract+spend query | 2–4 hours (legal + procurement + finance pulled in) | < 60 seconds |
| Contracts reviewed for renewal/liability risk per analyst-day | ~15–20 | Full portfolio (1000s) continuously monitored |
| Missed auto-renewal / unfavorable clause incidents | Reactive, discovered after the fact | Proactive alerting before renewal deadlines |
| Time-to-detect supplier financial distress | Days to weeks (manual news scanning) | Near real-time via automated monitoring tool |

This is the same category of problem solved commercially by Icertis, SirionLabs, and Zycus (CLM + supplier risk) — building a lean, well-architected version of this yourself is a very legible signal to a hiring panel that you understand enterprise AI, not just chatbots.

---

## 2. High-Level System Architecture

Think of the system as three planes: **Data Plane → Reasoning Plane → Serving Plane.**

```
┌─────────────────────────────── DATA PLANE (offline + streaming) ───────────────────────────────┐
│                                                                                                    │
│  Contract PDFs/DOCX ─▶ Parser (docling/unstructured) ─▶ Clause-aware chunker ─▶ Embedder          │
│         │                                                        │                                │
│         │                                                        ▼                                │
│         │                                              Qdrant (vector index, metadata:            │
│         │                                              vendor_id, clause_type, effective_date)     │
│         │                                                        │                                │
│         └────────────▶ Postgres full-text (tsvector/BM25 index) ─┘   ── Hybrid Retrieval pair     │
│                                                                                                     │
│  ERP source (simulated) ─▶ Postgres (vendor master, PO, spend, invoices)                          │
│         │                                                                                          │
│         └─▶ Debezium CDC ─▶ Kafka ─▶ stream consumer ─▶ refresh vector metadata / trigger alerts   │
│                                                                                                     │
│  External risk sources: SEC EDGAR filings API, OFAC sanctions list, news/GDELT ─▶ MCP tool servers│
└────────────────────────────────────────────────────────────────────────────────────────────────┘
                                              │
                                              ▼
┌────────────────────────────── REASONING PLANE (per-request, agentic) ───────────────────────────┐
│  FastAPI (async) ─▶ LangGraph StateGraph:                                                         │
│     Planner ─▶ Router ─▶ {RAG Retriever | SQL Tool | Risk-Monitor Tool (MCP)} ─▶ Synthesizer       │
│        ▲                                                                          │                │
│        └──────────────────────── Critic / Evaluator (loop if incomplete) ◀────────┘                │
│                                              │                                                      │
│                                    Guardrail + Structured Output (Pydantic RiskReport)             │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
                                              │
                                              ▼
┌───────────────────────────────────── SERVING PLANE ──────────────────────────────────────────────┐
│  REST API (FastAPI) ── Redis semantic cache ── Langfuse tracing on every span ── Auth/RBAC          │
│  Docker Compose (local) → CI/CD (GitHub Actions, Ragas/DeepEval regression gate) → Cloud (K8s/ECS) │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Ingestion pipeline detail:**
1. Parse contracts with a layout-aware parser (tables, signature blocks, clause headers preserved).
2. Chunk by clause/section (not fixed token windows) — critical for legal text, since a "Limitation of Liability" clause loses meaning if split mid-sentence.
3. Generate embeddings (e.g., an open-source `bge-large` or a hosted embedding model) and upsert into Qdrant with rich metadata for filtering (vendor, contract type, expiry date, clause category).
4. Simultaneously index the same chunks into Postgres full-text search (or Elasticsearch/BM25) for exact keyword matches — legal queries often need exact term matches ("termination for convenience", specific dollar figures) that pure semantic search misses.
5. Mirror ERP data into Postgres; use Debezium CDC to simulate a live enterprise system where spend/vendor data changes continuously (see Wow Factor #1).

**Data sourcing for a credible portfolio build:** don't fabricate contract data — use the **CUAD (Contract Understanding Atticus Dataset)**, a real, public dataset of ~510 commercial contracts with 41 expert-labeled clause categories, purpose-built for exactly this kind of legal-NLP task. Generate synthetic but realistic ERP/spend data with `Faker`. Pull real, free external signals from **SEC EDGAR** (financial filings), the public **OFAC SDN list** (sanctions screening), and a free-tier news API.

---

## 3. LangGraph Agent Design

### State schema (Pydantic, shared across nodes)
```python
class AgentState(BaseModel):
    user_query: str
    plan: list[SubTask] = []
    retrieved_chunks: list[RetrievedChunk] = []
    sql_result: SQLResult | None = None
    risk_signals: list[RiskSignal] = []
    draft_report: RiskReport | None = None
    critique: Critique | None = None
    retry_count: int = 0
    final_output: RiskReport | None = None
```

### Node map

| Node | Type | Responsibility | Key tech |
|---|---|---|---|
| **Planner** | LLM call | Decomposes the natural-language query into typed sub-tasks (`rag_lookup`, `sql_query`, `risk_check`) with explicit success criteria | Structured output via Pydantic + function calling |
| **Router** | Conditional edge | Fans out to one or more tool nodes in parallel based on the plan | LangGraph conditional/parallel edges |
| **RAG Retriever** | Tool node | Hybrid retrieval: semantic search (Qdrant) + BM25/keyword search (Postgres) → merge → **cross-encoder reranker** re-scores top-k candidates for precision | Qdrant, Postgres FTS, cross-encoder (e.g., `bge-reranker`) |
| **SQL Tool** | Tool node | Text-to-SQL against a **read-only** ERP replica; query is schema-constrained and validated (via `sqlglot`) before execution — no write/DDL ever permitted | Postgres, constrained text-to-SQL prompting |
| **Risk-Monitor Tool** | Tool node (via MCP) | Calls external MCP servers wrapping SEC EDGAR, sanctions list, and news search | MCP client (2026-07-28 stateless spec), external APIs |
| **Synthesizer** | LLM call | Merges retrieved evidence, SQL results, and risk signals into a structured `RiskReport` object with per-field citations | Pydantic structured output |
| **Critic / Evaluator** | LLM call (self-reflection) | Checks: (a) every claim is grounded in a citation, (b) the plan's success criteria are met, (c) no hallucinated numbers. Routes back to Planner if incomplete (max 3 iterations, then escalate to human) | LLM-as-judge, grounding checker |
| **Guardrail (pre/post)** | Deterministic code | Input: strips/flags prompt-injection patterns found *inside retrieved contract text or external content* before it reaches the LLM context. Output: validates the final object against the Pydantic schema and an allow-listed tool/action set | Regex + classifier + schema validation |
| **Finalizer** | Formatter | Produces the final cited, structured response returned by the API | — |

### Reasoning loop (the part interviewers will ask you to whiteboard)

```
START → Planner → Router ─┬─▶ RAG Retriever ──┐
                           ├─▶ SQL Tool ───────┼─▶ Synthesizer → Critic ─┬─(pass)──▶ Finalizer → END
                           └─▶ Risk Monitor ────┘                        └─(fail, retries<3)──▶ Planner (refine)
                                                                          └─(fail, retries==3)──▶ Human-in-the-loop interrupt
```

This is a **plan-and-execute** pattern with a **reflection/critic loop** (similar in spirit to Corrective-RAG / self-RAG patterns) — a level above the basic "retrieve then generate" chain that most portfolio projects stop at, and exactly the kind of cyclic, stateful graph LangGraph is designed for (as opposed to a linear chain).

---

## 4. Step-by-Step Implementation Plan

| Phase | Focus | Key deliverables | Rough effort |
|---|---|---|---|
| **0. Framing & data** | Problem scoping, data acquisition | CUAD contracts loaded, synthetic ERP schema + Faker generator, evaluation question set (30–50 hand-labeled queries with gold answers) | 3–4 days |
| **1. Ingestion pipeline** | Parsing → chunking → embeddings → indices | Async ingestion service, Qdrant collection with metadata schema, Postgres FTS index, idempotent re-ingestion | 1 week |
| **2. Baseline RAG + eval harness** | Non-agentic hybrid retrieval + reranking | Working hybrid search endpoint, Ragas baseline scores (faithfulness, context precision/recall, answer relevancy) committed as the regression floor | 1 week |
| **3. Agentic orchestration** | LangGraph state machine, MCP tool servers | Planner/Router/Retriever/SQL/Risk-Monitor/Critic nodes wired up; MCP servers for SQL and external risk tools | 1.5–2 weeks |
| **4. Structured outputs & guardrails** | Pydantic schemas, prompt-injection defense, tool allow-listing | `RiskReport` schema enforced end-to-end; adversarial test suite (malicious text embedded in a contract) proven blocked | 1 week |
| **5. Serving layer** | FastAPI async API, semantic caching, auth | REST endpoints, Redis semantic cache, rate limiting, JWT-based RBAC (row-level filtering by org/role) | 1 week |
| **6. Observability & regression testing** | Langfuse tracing, DeepEval/Ragas in CI | Every agent run traced end-to-end in Langfuse; GitHub Actions job fails the build if eval scores regress beyond a threshold | 3–4 days |
| **7. Containerization & deployment** | Docker, CI/CD, cloud | `docker-compose` for local full-stack (Postgres, Qdrant, Redis, Kafka, app); GitHub Actions → build/test/push → deploy to cloud (ECS/Cloud Run/EKS) | 1 week |
| **8. Wow-factor add-ons** | See Section 5 | CDC pipeline, semantic caching benchmark, dynamic query rewriting | 1–1.5 weeks |
| **9. Polish for hiring committee** | Docs, diagrams, demo | Architecture diagram, README with metrics, 3-minute demo video/GIF, load test report (latency/cost under concurrency) | 3–4 days |

**Total: ~8–10 weeks part-time** — appropriately scoped for a flagship portfolio project without ballooning indefinitely.

---

## 5. "Wow Factor" Features (pick 2–3, implement them deeply rather than shallowly)

### 1. CDC-driven real-time procurement intelligence
Use **Debezium** to capture row-level changes on the simulated ERP Postgres tables (new PO, spend update, vendor status change) and stream them through **Kafka**. A downstream consumer updates the agent's context in near real-time and can proactively trigger the agent (e.g., "a new $2M PO was just issued to a vendor whose contract has an unresolved liability-cap flag → auto-generate an alert"). This demonstrates event-driven architecture and separates you from portfolio projects that only do batch/offline ingestion.

### 2. Semantic caching for cost & latency control
Put a **Redis-backed semantic cache** in front of both the embedding calls and the final LLM responses: near-duplicate queries (cosine similarity above a threshold) are served from cache instead of re-running the full agentic loop. Build a small before/after benchmark dashboard showing p50/p95 latency and $/query cost with and without the cache — this turns an abstract MLOps concept into a concrete, defensible number you can quote in an interview ("cut average query cost by X% and p95 latency by Y ms via semantic caching").

### 3. Corrective retrieval with dynamic query rewriting
When the Critic node judges retrieval confidence too low (e.g., reranker scores below threshold, or the Critic can't find grounding for a claim), the Planner rewrites the query — using techniques like HyDE (hypothetical document embeddings) or multi-query expansion — and re-runs retrieval before falling back to a broader search or flagging "insufficient evidence" rather than hallucinating. This self-correcting loop is the difference between a project that looks like a demo and one that behaves like production software.

### Bonus (if you want a 4th differentiator): human-in-the-loop escalation
Use LangGraph's interrupt capability so that any answer implying financial exposure above a configurable threshold (e.g., >$1M) pauses for human approval before being returned — a realistic enterprise trust-and-safety pattern that most portfolio agents skip entirely.

---

## 6. Security & Guardrails (a section hiring panels increasingly probe hard)

Treat **all retrieved content — contract text and external web/API content — as untrusted input**, not just the user's message. This matters more than it used to: 2026 has seen real production MCP incidents in the wild (cross-tenant data leaks, path-traversal bugs, and "tool poisoning" attacks where malicious instructions are hidden inside content an agent retrieves), which is exactly the injection surface a document-and-tool agent like this one exposes. Concretely:

- **Prompt-injection defense**: sanitize/flag suspicious instruction-like text inside retrieved contract chunks or external API responses before they enter the LLM context; never let retrieved content alter the tool-execution plan directly.
- **Tool allow-listing**: the SQL tool is read-only against a replica, with query validation (`sqlglot` parse + table/column allow-list) rejecting anything outside a narrow schema — no DDL, no writes, no arbitrary table access.
- **RBAC / row-level security**: vector DB metadata filters and Postgres row-level security ensure a user only ever retrieves contracts and spend data their role is entitled to see.
- **Structured output validation**: every agent response must validate against the Pydantic `RiskReport` schema before being returned — reject and retry rather than pass through malformed or partially-hallucinated structured data.
- **Cost/action circuit breakers**: hard caps on tokens, tool calls, and loop iterations per request to prevent runaway agent loops.

---

## 7. Core Tech Stack Recap

| Layer | Tools |
|---|---|
| Backend/API | Python (async), FastAPI, Pydantic v2 |
| Orchestration | LangGraph, MCP (tool servers for SQL + external risk APIs) |
| Retrieval | Qdrant (vector), Postgres full-text (keyword), cross-encoder reranker |
| Data | Postgres (ERP + metadata), Debezium + Kafka (CDC) |
| Caching | Redis (semantic + result cache) |
| Evaluation | Ragas, DeepEval, a hand-labeled gold-answer regression set |
| Observability | Langfuse (full trace of every planner/tool/critic span) |
| CI/CD & infra | Docker, GitHub Actions (eval-gated deploys), cloud deploy (ECS/Cloud Run/EKS) |

*Practical note: the LLM-eval/observability tooling landscape moves fast — Langfuse and DeepEval/Ragas are the current strong open-source defaults for tracing and regression testing respectively as of 2026, but re-check the latest docs when you actually build, since versions and feature sets change quickly.*

---

## 8. How to Frame This on Your Resume / in Interviews

- "Designed and shipped a production-grade agentic RAG system combining hybrid semantic+keyword retrieval, cross-encoder reranking, text-to-SQL, and MCP-based external tool orchestration, reducing multi-source procurement research time from hours to under a minute (benchmarked on a 40-query gold eval set)."
- "Built a LangGraph plan-execute-critique agent loop with self-correcting retrieval, cutting hallucinated/unsupported claims by X% (measured via DeepEval faithfulness scoring) versus a naive single-pass RAG baseline."
- "Implemented CDC-based real-time data sync (Debezium/Kafka) and Redis semantic caching, reducing average query cost by X% and p95 latency by Y ms."
- "Established an eval-gated CI/CD pipeline (Ragas/DeepEval + Langfuse) that blocks deployment on retrieval or generation quality regressions."

Be ready to whiteboard the LangGraph state machine and explain *why* each guardrail exists (interviewers will push on "what if the contract text contains a prompt injection?" — you now have a concrete, defensible answer).

---

## Suggested repo structure

```
procureiq/
├── ingestion/          # parsers, chunkers, embedding + indexing jobs
├── agent/               # LangGraph graph definition, nodes, state schema
├── tools/                # MCP tool server implementations (sql, risk_monitor)
├── api/                  # FastAPI app, routers, auth/RBAC
├── eval/                 # Ragas/DeepEval suites, gold dataset, CI eval runner
├── infra/                # docker-compose.yml, Dockerfiles, k8s/terraform manifests
├── observability/        # Langfuse config, tracing decorators
└── docs/                 # architecture diagrams, README, demo assets
```

---

*Next step suggestion: start with Phase 0–2 (data + baseline hybrid RAG + eval harness) before touching LangGraph — a solid, measured retrieval baseline is what makes the later "agentic uplift" numbers meaningful and defensible in an interview.*
