# ProcureIQ — Explained Simply, Plus How to Learn & Build It
### A companion guide to the technical blueprint

---

## Part 1: What ProcureIQ Actually Does (No Jargon)

Imagine you hire a tireless junior analyst on day one and give them three superpowers:

1. They've **read every contract your company has ever signed** — thousands of pages of legal text — and remember exactly which clause is in which contract.
2. They have **the entire spend ledger memorized** — every purchase order, every dollar paid to every vendor.
3. Every morning they **check the news, financial filings, and sanctions lists** for anything bad happening to any of your suppliers (bankruptcy, lawsuits, scandals).

Now, instead of you having to ask three different people three different questions and manually combine the answers, you ask this one analyst something like: *"Which vendors have contracts renewing next quarter with bad liability terms, how much money do we have tied up with them, and has anything scary happened to them recently?"* — and they come back in under a minute with a clean, organized answer, **showing you exactly which document or record each fact came from** (so you can trust it, not just take their word for it).

That analyst is ProcureIQ. It isn't a chatbot that answers from "vibes" — it's a system that **actually goes and looks things up** every time, in real documents and real databases, and **checks its own work** before answering you.

### The "jobs" bundled inside it, explained one at a time

| Plain-English role | What it really is | Why it matters |
|---|---|---|
| 📖 **The Contract Reader** | Retrieval-Augmented Generation (RAG) — searches contract text by *meaning*, not just exact words, then double-checks the top results | Finds the right clause even if you don't use the exact legal phrasing |
| 🧮 **The Accountant** | A tool that translates your question into a database query (Text-to-SQL) | Gets you exact, correct numbers instead of the AI "guessing" a dollar figure |
| 📰 **The News Scout** | A tool that checks external sources (news, filings, sanctions lists) | Keeps the answer current — a contract doesn't tell you a vendor filed for bankruptcy last week |
| 🗂️ **The Project Manager** | The agent's planning/orchestration logic | Breaks a complex question into smaller solvable pieces and decides which "specialist" handles each piece |
| ✅ **The Editor** | A self-checking step (the "Critic") | Re-reads the draft answer and asks "is every claim actually backed by a source? Did we miss part of the question?" before it's shown to you |
| 🛡️ **The Security Guard** | Guardrails | Makes sure the system can't be tricked by hidden instructions buried in a document, and that people only see data they're allowed to see |
| ⚡ **The Fast Memory** | Semantic caching | Remembers recently asked similar questions so it doesn't redo all that work from scratch every time — faster and cheaper |
| 📋 **The Report Card** | Automated evaluation (Ragas/DeepEval) + logging (Langfuse) | Every time you tweak the system, an automatic test suite checks it didn't get *worse* before you ship the change |

That's the whole project, in plain words. Everything after this point is about **how** these pieces are built and **how you personally learn to build them**.

---

## Part 2: The Architecture, In Plain Terms — and What Happens When You Ask a Question

Think of the system as three physical places:

- 🏛️ **The Library** — where all the contracts and financial records live, organized so they can be found instantly (this is the "data" side: databases, the vector search index, the keyword index).
- 🧠 **The Brain** — the part that actually thinks: plans what to look up, goes and gets it, and puts together an answer (this is the "agent" side: LangGraph, the tools, the critic).
- 🛎️ **The Front Desk** — where you interact with the system: you type your question here, and it's also where security checks and speed shortcuts (caching) live.

### The journey of one question, step by step

Say you type: *"Which vendors have contracts auto-renewing next quarter with liability caps below $1M, and what's our spend exposure?"*

1. **You ask, at the Front Desk.** Your question hits the API.
2. **ID check.** The system checks who you are and what data your role is allowed to see (a legal team member might see different things than a junior procurement analyst).
3. **"Have we seen this before?"** The Front Desk checks its fast memory (semantic cache) for a near-identical recent question. If there's a close match, you get an instant answer — no need to redo the work.
4. **The Brain wakes up.** If it's a new question, the "Project Manager" (Planner) breaks it into smaller jobs: *(a)* find contracts with auto-renewal + low liability caps, *(b)* look up spend totals for those vendors.
5. **Specialists get dispatched.** The Contract Reader searches the library (combining "search by meaning" and "search by exact words," then double-checking the top matches with a more careful re-read — this is the reranking step). The Accountant runs a database lookup for the spend numbers.
6. **Findings come back.** Both specialists report their raw findings back to the Brain.
7. **The Editor stitches it together.** Everything gets combined into one clean, structured report — vendor names, clause details, dollar amounts, every fact tagged with where it came from.
8. **Self-check.** The Editor re-reads its own draft: *"Is every number here actually backed by something I looked up? Did I cover both parts of the question?"* If something's missing or unsupported, it goes back to step 4 and tries again (up to a few times) rather than guessing.
9. **Security sign-off.** A final automated check confirms the answer only contains information you're allowed to see, and that it's in the correct format.
10. **You get your answer** — cited, structured, and fast — while everything that just happened is quietly logged in the background so engineers can review or debug it later.

This loop — plan → fetch → combine → self-check → maybe retry — is the actual "agentic" part of Agentic RAG. It's what separates this from a simple chatbot that just answers whatever comes to mind.

---

## Part 3: How to Learn Everything You Need (Topic by Topic)

You don't need to master all of this before you start — the phased plan in Part 4 interleaves learning with building, so you learn each piece right before you use it. This table is your reference map.

### A. Backend foundations
| Topic | Why you need it | Where to learn it |
|---|---|---|
| Python `async`/`await` | Your agent calls the retriever, the database, and external APIs — doing this concurrently (not one-at-a-time) keeps it fast | Real Python's async IO guide, or the official Python `asyncio` docs |
| FastAPI | This is the web framework that turns your agent into an API other apps can call | Official FastAPI tutorial at fastapi.tiangolo.com — it's genuinely one of the best-written docs in the Python ecosystem, follow it top to bottom |
| Pydantic v2 | Defines the "shape" of your data (e.g., what a `RiskReport` object must contain) so the LLM's output can be validated, not just trusted | Official Pydantic docs, "Models" and "Validators" sections |

### B. Data & search
| Topic | Why you need it | Where to learn it |
|---|---|---|
| PostgreSQL + full-text search | Stores your structured spend/vendor data, and doubles as your "keyword search" index for exact-term matches | Official PostgreSQL tutorial, plus the docs section on full-text search (`tsvector`/`tsquery`) |
| Embeddings (the concept) | The technique that lets you search text "by meaning" instead of exact words | Any reputable intro (OpenAI's or Cohere's "what are embeddings" explainer) — you need the intuition more than the math |
| Qdrant (vector database) | Where your contract-clause embeddings are stored and searched | Qdrant's official "Local Quickstart," then their "Hybrid Search with FastEmbed" and "Reranking in Hybrid Search" tutorials — these walk through exactly the dense + sparse + rerank pattern this project needs |
| Cross-encoder reranking | The "double-check the top results carefully" step that meaningfully improves retrieval accuracy | Qdrant's reranking tutorial (above) plus the Sentence-Transformers library docs on cross-encoders |

### C. Agentic AI & orchestration
| Topic | Why you need it | Where to learn it |
|---|---|---|
| Tool calling / function calling | The core mechanism that lets an LLM "decide" to run a database query or call an API instead of just generating text | Anthropic's tool-use guide or OpenAI's function-calling guide — both are short and hands-on |
| LangGraph | The framework you'll use to build the Planner → Router → Tools → Critic loop as a real, stateful graph | LangChain Academy's free "Introduction to LangGraph" course, then the official quickstart at docs.langchain.com — build the tutorial's examples yourself before touching your own project |
| Model Context Protocol (MCP) | The standard you'll use to expose your SQL tool and risk-monitoring tool to the agent in a clean, reusable way | The official quickstart at modelcontextprotocol.io ("Build your first MCP server" — budget about 30 minutes for the walkthrough) |

### D. Production / MLOps
| Topic | Why you need it | Where to learn it |
|---|---|---|
| LLM evaluation (Ragas / DeepEval) | Turns "does this feel right?" into a repeatable, numeric test suite | Ragas's official quickstart docs; DeepEval's "Getting Started" (their pytest-style workflow is genuinely fast to pick up if you already know pytest) |
| Observability (Langfuse) | Lets you see exactly what your agent did on any given run — which tool it called, what it retrieved, how long each step took | Langfuse's official "Get Started" docs — the self-hosted Docker Compose setup is a good way to also practice your Docker skills |
| Docker & Docker Compose | Packages your whole stack (API, Postgres, Qdrant, Redis) so it runs identically on your laptop and in the cloud | Docker's official "Get Started" guide, then practice by writing a `docker-compose.yml` for just two services (API + Postgres) before adding the rest |
| CI/CD (GitHub Actions) | Automatically runs your eval suite and blocks a deploy if quality regresses | GitHub's official Actions quickstart docs |
| Cloud deployment | Gets your project off your laptop and into something you can demo with a public URL | Pick **one**: AWS ECS "Getting Started" or Google Cloud Run's quickstart — both have generous free tiers and are enough for a portfolio deployment |

### E. Security
| Topic | Why you need it | Where to learn it |
|---|---|---|
| Prompt injection & LLM security basics | Contracts and web content are *untrusted input* to your agent — you need to understand this attack class to defend against it | The OWASP "Top 10 for LLM Applications" project — it's the industry-standard reference and is written to be approachable |
| Row-level security / RBAC in Postgres | Ensures a user can only query the vendor/spend rows their role permits | PostgreSQL's official docs section on "Row Security Policies" |

### Real datasets to practice on (don't fabricate data — use these)
- **CUAD (Contract Understanding Atticus Dataset)** — ~510 real commercial contracts with expert-labeled clauses, free and public. This is your contract corpus.
- **SEC EDGAR full-text search API** — free, public financial filings for the "risk monitoring" tool.
- **OFAC SDN list** — the public U.S. sanctions list, free to download, good for a realistic "sanctions screening" tool.
- **Faker** (Python library) — generate a realistic-looking synthetic ERP/spend dataset.

---

## Part 4: The Phased Implementation Plan (Learn → Build, in order)

Each phase pairs **what to learn right before you need it** with **what to actually build**, so you're never learning in the abstract.

### Phase 0 — Foundations & Data (3–4 days)
- **Learn:** Python async basics, refresh on Pydantic.
- **Build:** Download CUAD and explore it manually. Write a Faker-based script to generate your synthetic vendor/spend/PO tables. Hand-write 30–50 realistic test questions with "gold" answers you work out yourself — this becomes your ground truth for every later evaluation.

### Phase 1 — Ingestion Pipeline (1 week)
- **Learn:** PostgreSQL full-text search, embeddings concept, Qdrant local quickstart.
- **Build:** A pipeline that parses CUAD contracts, chunks them by clause (not fixed token windows), embeds each chunk, and loads them into Qdrant with metadata (vendor, clause type, date). In parallel, load the same chunks into Postgres full-text search. Load your synthetic ERP data into Postgres tables.

### Phase 2 — Baseline Retrieval + Evaluation Harness (1 week)
- **Learn:** Qdrant's hybrid search and reranking tutorials; Ragas quickstart.
- **Build:** A simple (non-agentic) endpoint that does hybrid search + cross-encoder reranking and answers a question from retrieved chunks alone. Run your 30–50 gold questions through Ragas to get a **baseline score** (faithfulness, context precision/recall). This number is your yardstick for everything that follows — without it, you can't prove the "agentic" version is actually better.

### Phase 3 — Agentic Orchestration (1.5–2 weeks)
- **Learn:** Tool-calling basics, LangChain Academy's LangGraph course, MCP quickstart.
- **Build:** Implement the LangGraph state machine — Planner, Router, RAG Retriever node, SQL Tool node (wrapped as an MCP server), Risk-Monitor tool node (wrapped as an MCP server calling SEC EDGAR / OFAC / news), Synthesizer, Critic with a retry loop.

### Phase 4 — Structured Outputs & Guardrails (1 week)
- **Learn:** OWASP Top 10 for LLM Applications, Postgres row-level security.
- **Build:** A strict Pydantic `RiskReport` schema enforced on every response. Add input sanitization for content pulled from contracts/web (never let retrieved text directly alter the tool-execution plan). Add a read-only, schema-constrained SQL tool with query validation. Write a small adversarial test suite — a contract chunk with a hidden malicious instruction embedded in it — and prove your guardrails catch it.

### Phase 5 — Serving Layer (1 week)
- **Learn:** FastAPI tutorial (if not already comfortable), Redis basics.
- **Build:** Wrap everything in an async FastAPI app with proper routers, JWT-based auth, and row-level filtering by role. Add a Redis-backed semantic cache in front of embeddings and final answers.

### Phase 6 — Observability & Regression Testing (3–4 days)
- **Learn:** Langfuse "Get Started" docs, GitHub Actions quickstart.
- **Build:** Instrument every node/tool call with Langfuse tracing. Wire your Ragas/DeepEval gold-question suite into a GitHub Actions workflow that fails the build if scores drop below your Phase-2 baseline.

### Phase 7 — Containerization & Deployment (1 week)
- **Learn:** Docker "Get Started," then either AWS ECS or Google Cloud Run quickstart (pick one).
- **Build:** A `docker-compose.yml` that runs the full local stack (API, Postgres, Qdrant, Redis). Then a GitHub Actions pipeline that builds, tests, and deploys to your chosen cloud target.

### Phase 8 — "Wow Factor" Add-ons (1–1.5 weeks)
- **Build (pick 2–3):** Debezium/Kafka CDC pipeline simulating real-time ERP updates; a before/after latency & cost benchmark dashboard proving the semantic cache's impact; a corrective-retrieval loop (query rewriting when the Critic flags low-confidence retrieval).

### Phase 9 — Polish for the Hiring Committee (3–4 days)
- **Build:** Architecture diagram, a README with your actual measured metrics (not vague claims), a 2–3 minute demo video or GIF walkthrough, and a short load-test report showing latency/cost under concurrent requests.

**Total: roughly 8–10 weeks, part-time** — and by the end of it you'll have hands-on, defensible experience in every technology listed in your original requirements, not just surface familiarity.

---

*Tip: at each phase, resist the urge to jump ahead — the ordering matters. A solid Phase 2 baseline is what makes your Phase 3 "agentic uplift" numbers meaningful in an interview, and Phase 4's guardrails are much easier to reason about once the happy-path agent in Phase 3 already works.*
