# ProcureIQ — Product Requirements Document

## 1. Problem Statement
Procurement, legal, and finance teams manually cross-reference two disconnected systems to
answer risk questions: unstructured contract text (MSAs, SOWs, NDAs) and structured ERP data
(spend, purchase orders, vendor master). A question like "which vendors with >$500K annual
spend have contracts auto-renewing next quarter with non-standard liability terms?" today takes
an analyst hours of manual cross-referencing across systems.

## 2. Goal
Build an agentic AI system that answers multi-hop questions spanning contract text, spend data,
and (later) external supplier-risk signals, returning a structured, cited Risk & Exposure Report.

## 3. Users (for framing, not literal build targets)
- **Procurement analyst** — "which contracts are exposed on renewal terms this quarter?"
- **Legal reviewer** — "compare indemnification clauses across our top vendors."
- **Finance controller** — "how much spend is tied up in contracts with uncapped liability?"

## 4. Scope for v1 (deliberately reduced from the original flagship blueprint)
**In scope:** Agentic RAG, hybrid retrieval (dense + keyword) with reranking, LangGraph
orchestration, read-only text-to-SQL tool, MCP tool server(s), structured Pydantic outputs,
critic/self-check loop with retry, guardrails (prompt-injection defense, RBAC, SQL validation),
RAGAS/DeepEval evaluation harness, Langfuse observability, FastAPI serving, Redis caching,
Docker + GitHub Actions CI/CD, single-target cloud deployment.

**Explicitly out of scope for v1:** Kafka/Debezium CDC streaming, Kubernetes, Terraform,
human-in-the-loop approval workflows, elaborate semantic-cache benchmarking dashboards. These
are documented as "v2 ideas" — the reasoning/retrieval/tool-use/eval/security stack is what
carries the interview signal, not infrastructure breadth.

## 5. Data sources
| Source | What it provides | Why this one |
|---|---|---|
| **CUAD (Contract Understanding Atticus Dataset)** | 510 real commercial contracts, full text, expert-labeled clause spans across 41 categories (Cap On Liability, Termination For Convenience, Renewal Term, Anti-Assignment, etc.) | Free, public, real legal language — not fabricated contracts. Labels double as ground truth for later retrieval evaluation. |
| **Synthetic ERP data (Faker-generated)** | Vendors, purchase orders, invoices/spend, linked to a subset of CUAD contracts | No public dataset realistically links contracts to spend — this must be synthetic, but is clearly labeled as such (never presented as real production data). |

## 6. Example queries the system must eventually answer
1. "List vendors with contracts expiring in Q1 2027 that have auto-renewal clauses and a
   liability cap below our $1M standard. What's our total annual spend with them?"
2. "Compare the indemnification terms across our three cloud infrastructure vendors."
3. "Which vendors have uncapped liability and more than $500K in annual spend?"

## 7. Success criteria (targets to benchmark, not numbers to claim yet)
- Faithfulness / context precision / context recall scores on a hand-built gold-question set
  (Phase 4), with the agentic pipeline measurably beating a naive single-pass RAG baseline
  (Phase 2).
- Guardrail test suite (adversarial prompt-injection cases) passes 100%.
- End-to-end latency and cost per query benchmarked under concurrent load (Phase 12/13).

## 8. Phased plan reference
See `docs/ARCHITECTURE.md` for the system diagram and `README.md` for the phase-by-phase build
order (14 phases, Phase 0 → Phase 13, ~6-8 weeks at ~4 hrs/day).
