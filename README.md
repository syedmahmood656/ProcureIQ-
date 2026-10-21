# ProcureIQ — Agentic RAG for Procurement Contract & Spend Intelligence

An AI system that answers multi-hop questions spanning real contract text, structured spend
data, and (later) external supplier-risk signals — returning a cited, structured Risk Report
instead of free-text.

See `docs/PRD.md` and `docs/ARCHITECTURE.md` for the full design.

## Status

| Phase | What | Status |
|---|---|---|
| 0 | Architecture & setup | ✅ Done (this repo) |
| 1 | Procurement data (CUAD + synthetic ERP) | ✅ Done (this repo) |
| 2 | Document ingestion (clause-aware chunking, Qdrant + full-text) | ✅ Done (this repo) |
| 3 | Advanced retrieval (dense + keyword + RRF + rerank) + baseline RAG | ✅ Done (this repo) |
| 4 | Baseline evaluation (gold questions + RAGAS) | ⏭️ Skipped for now — jumped to Phase 5 |
| 5 | Agentic pipeline: LangGraph Planner → Router → RAG tool → Synthesizer → Critic, with a self-correcting retry loop | ✅ Done (this repo) |
| 6 | SQL tool (read-only, validated Text-to-SQL) + real router branching (`rag` / `sql` / `both`) + SQL-narrowed multi-hop retrieval | ✅ Done (this repo) |
| 7 | Structured outputs (validated `RiskReport`) + prompt-injection guardrails + adversarial test suite | ✅ Done (this repo) |
| 8 | FastAPI serving layer: `/ask`, `/ask/stream` (SSE), `/health`, `/ready`, answer cache, API key, concurrency limit | ✅ Done (this repo) |
| 9-13 | MCP wrapper, Risk-Monitor tool, observability, containerization, deployment | ⬜ |

## Phase 0 — what's here
- `docs/PRD.md` — problem statement, scope (incl. what was deliberately cut), success criteria
- `docs/ARCHITECTURE.md` — system diagram + component-to-phase mapping
- This repo scaffold

## Phase 1 — what's here and how to run it

**Data sources:**
- **CUAD** — 510 real commercial contracts with expert-labeled clause spans (41 categories).
  Cloned from `github.com/TheAtticusProject/cuad` (not included in this zip — re-clone it,
  see below).
- **Synthetic ERP** — Faker-generated vendors, purchase orders, invoices, with 120 vendors
  linked 1:1 to real CUAD contracts by `contract_id`. Clearly synthetic; never presented as
  real production data.

### 1. Get the CUAD source data
```bash
git clone --depth 1 https://github.com/TheAtticusProject/cuad.git data_source_cuad
cd data_source_cuad && unzip data.zip -d data && cd ..
```

### 2. Set up Python environment
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```
> If you set up `.venv` earlier and are picking this repo back up after a later phase added
> new dependencies (e.g. Phase 2 added `qdrant-client`/`sentence-transformers`), re-run
> `pip install -r requirements.txt` — it's safe to run repeatedly and only installs what's
> missing.

### 3. Extract CUAD into per-contract files
```bash
python scripts/extract_cuad_contracts.py --source data_source_cuad/data/CUADv1.json --out data/raw/cuad
```
Produces:
- `data/raw/cuad/contracts/*.txt` — one file per contract (510 total)
- `data/raw/cuad/contracts_metadata.csv` — contract_id, title, char length, clause count
- `data/raw/cuad/clauses.jsonl` — every labeled clause span (13,823 rows), ready to use as
  chunking metadata and gold-question source material in Phase 2/4

### 4. Start Postgres
Either via Docker:
```bash
docker compose -f infra/docker-compose.yml up -d
```
...or a local install — either way, apply the schema:
```bash
psql -h localhost -U postgres -d procureiq -f erp/schema.sql
```

> **⚠️ `schema.sql` is destructive.** It opens with `DROP TABLE ... CASCADE` on every table, so
> re-running it wipes ALL data, not just the table you care about. If you ever re-apply it,
> you must redo the *entire* load sequence afterward, in this exact order:
> ```bash
> python ingestion/load_cuad_documents.py
> python erp/load_erp_to_postgres.py           # reuses existing CSVs in data/processed/erp/
> python ingestion/load_fulltext_postgres.py
> ```
> (Qdrant is a separate database and is unaffected by a Postgres schema re-apply.)

> **No local `psql` client? (common on Windows, especially if you're using Docker for Postgres)**
> `psql` lives *inside* the Postgres container, not on your host. Run it there instead:
> ```bash
> docker exec -it procureiq_postgres psql -U postgres -d procureiq -f /docker-entrypoint-initdb.d/schema.sql
> ```
> That file path works because `docker-compose.yml` already mounts `erp/schema.sql` there. This
> only matters if you need to *re-apply* the schema — on a completely fresh Docker volume it
> auto-applies on first container start, so you likely won't need this at all in a normal run.
> For every other `psql -h localhost ...` command in this README, swap in
> `docker exec -it procureiq_postgres psql -U postgres -d procureiq ...` the same way — or open
> an interactive session once with `docker exec -it procureiq_postgres psql -U postgres -d procureiq`
> and paste queries directly into it (avoids PowerShell/bash quoting issues entirely). **Don't**
> paste a second `docker exec ...` command while already inside that interactive session — it'll
> be interpreted as SQL, not a shell command, and just hang on a continuation prompt (`procureiq-#`).
> Type `\q` and Enter to get out cleanly if that happens.

### 5. Load the CUAD document registry (required before step 6 — Phase 2 schema change)
`contracts` now has a foreign key to `cuad_documents`, a new table registering all 510 CUAD
contracts (not just the 120 vendor-linked ones). This must be loaded first.
```bash
python ingestion/load_cuad_documents.py
```

### 6. Generate and load synthetic ERP data
```bash
python erp/generate_synthetic_erp.py --n-vendors 120 --seed 42
cp .env.example .env   # edit if your Postgres creds differ
python erp/load_erp_to_postgres.py
```

### 7. Sanity-check the join that makes this project real
```sql
SELECT v.vendor_name, c.contract_id, c.expiration_date, c.auto_renewal,
       SUM(po.amount) AS total_spend
FROM vendors v
JOIN contracts c ON c.vendor_id = v.vendor_id
JOIN purchase_orders po ON po.vendor_id = v.vendor_id
WHERE c.auto_renewal = TRUE AND c.expiration_date < '2027-01-01'
GROUP BY v.vendor_name, c.contract_id, c.expiration_date, c.auto_renewal
ORDER BY total_spend DESC LIMIT 5;
```
If this returns rows joining a real vendor to a real contract_id and a spend total, Phase 1
is working correctly — this is the exact multi-hop pattern the agent will automate later.

## Phase 2 — document ingestion (clause-aware chunking, Qdrant + full-text)

**What it does:** chunks all 510 CUAD contracts by clause boundary (never splits a liability
clause mid-sentence), then loads the same chunks into both Qdrant (dense/semantic search) and
Postgres (keyword/full-text search) — the two halves of hybrid retrieval that get fused in
Phase 3.

### 1. Chunk the contracts
```bash
python ingestion/clause_chunker.py
```
Produces `data/processed/chunks.jsonl`. On the full CUAD corpus this yields ~42,000 chunks:
~6,200 anchored to one of the 41 labeled clause categories, the rest ("GENERAL") filling the
uncovered boilerplate so no part of a contract is silently dropped from the index.

### 2. Load chunks into Postgres full-text search
```bash
python ingestion/load_fulltext_postgres.py
```
Test it:
```sql
SELECT chunk_id, categories, ts_rank(search_vector, query) AS rank, LEFT(chunk_text, 120)
FROM contract_chunks, to_tsquery('english', 'liability & cap') query
WHERE search_vector @@ query
ORDER BY rank DESC LIMIT 5;
```

### 3. Load chunks into Qdrant
**Important — embedding model download:** `SentenceTransformerEmbedder` pulls model weights
from Hugging Face Hub the first time it runs. If you're in a network-restricted environment
(e.g. a sandboxed container) and that's blocked, use `--fake-embedder` to validate the
pipeline plumbing only — it produces deterministic but semantically meaningless vectors, so
similarity search results won't be meaningful, only the data flow (payload structure,
filtering) is proven correct. Run with a real model on a machine with normal internet access
before Phase 3.

Real run (recommended — needs `docker compose up` for the `qdrant` service first):
```bash
python ingestion/build_qdrant_index.py --url http://localhost:6333
```
Offline plumbing test (no server, no internet, no real embeddings):
```bash
python ingestion/build_qdrant_index.py --local-path data/processed/qdrant_local --fake-embedder
```
Note: Qdrant's local file-based mode isn't recommended above ~20K points — fine for this
smoke test, but use the real Qdrant server (Docker) for actual retrieval work in Phase 3.

> **`qdrant-client version X is incompatible with server version Y` warning?** This happens
> when `pip` installs a newer `qdrant-client` than the `qdrant/qdrant` Docker image version
> pinned in `infra/docker-compose.yml`. It's a warning, not an error — a 1-2 minor-version gap
> works fine in practice (this was tested with client 1.19.1 against server 1.12.4 with no
> issues). To silence it, either pin the client to match the server exactly
> (`pip install qdrant-client==1.12.1`) or bump the server image tag in `docker-compose.yml`
> to something closer to your installed client, then `docker compose up -d qdrant` again.

## Phase 3 — hybrid retrieval + baseline RAG

**What it does:** for a given question, searches Qdrant (dense/semantic) and Postgres
(keyword/full-text) in parallel, fuses the two ranked lists with Reciprocal Rank Fusion, then
reranks the fused shortlist with a cross-encoder for a final, accurate top-K. A simple
single-pass baseline RAG endpoint sits on top of this — no tool use, no retry loop — as the
yardstick the eventual agentic pipeline (Phase 5+) has to beat.

### Try each piece independently
```bash
python retrieval/keyword_retriever.py "termination for convenience"
python retrieval/dense_retriever.py "what happens if a vendor terminates for convenience"
python retrieval/fusion.py   # pure-logic smoke test, no DB/model needed
```
Worth noticing: a long natural-language query can return **zero** keyword matches (full-text
search requires the literal words to co-occur) while still working fine for dense search, and
vice versa for exact defined terms an embedding model under-weights. That gap is exactly why
both are fused rather than picking one.

### Try the full pipeline
```bash
python retrieval/hybrid_retriever.py "what happens if a vendor terminates for convenience"
```

### Try the baseline RAG endpoint (needs a generation provider configured in `.env`)
```bash
python retrieval/baseline_rag.py "what is the liability cap for our vendors" --provider gemini
python retrieval/baseline_rag.py "what is the liability cap for our vendors" --provider openrouter
python retrieval/baseline_rag.py "what is the liability cap for our vendors" --provider huggingface
```
Set the matching API key in `.env` first (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, or `HF_TOKEN`).
`--provider fake` runs the whole retrieve → prompt → generate → cite pipeline with no API key
or network call at all — useful for confirming the wiring before spending any API credits.

### Offline plumbing testing (no real model/API needed at all)
Every script above accepts `--fake-embedder` and/or `--fake-reranker` (and `baseline_rag.py`
additionally takes `--provider fake`) to validate data flow with deterministic stand-ins
instead of real models — the same pattern used in Phase 2 for `build_qdrant_index.py`.

## Repo structure
```
procureiq/
├── docs/                 # PRD, architecture, project overview
├── data/
│   ├── raw/cuad/         # extracted contract text + clause labels (generated, gitignored)
│   └── processed/        # chunks.jsonl, erp/ CSVs, qdrant_local/ (all generated, gitignored)
├── erp/                  # schema.sql, ERP generator, ERP loader, create_readonly_role.sql (Phase 6)
├── ingestion/            # Phase 2: clause_chunker, embedder, Qdrant loader, full-text loader,
│                         #           cuad_documents loader
├── retrieval/            # Phase 3: keyword_retriever, dense_retriever, fusion, reranker,
│                         #           hybrid_retriever, generator, baseline_rag
├── agent/                # Phase 5: state, nodes, graph  |  Phase 6: router, sql_tool, prompts  |  Phase 7: guardrails, schemas
├── scripts/              # CUAD extraction
├── infra/                # docker-compose.yml (postgres + qdrant)
├── requirements.txt
└── .env.example
```

## Phase 5 — agentic pipeline (LangGraph, self-correcting retry loop)

**What it does:** wraps Phase 3's baseline retrieval + generation in a LangGraph state
machine — Planner → Router → RAG tool → Synthesizer → Critic — where the Critic
deterministically checks that every `[contract_id]` the answer cites was actually retrieved
(no extra LLM call needed), and routes back to the Planner to rewrite the query and try again
if not. Still single-tool (RAG only) — Phase 6-7 add a SQL tool over the ERP tables and turn
the Router into a real multi-tool decision.

```bash
python agent/graph.py "what is the liability cap for our vendors" --provider gemini
python agent/graph.py "what is the liability cap for our vendors" --provider fake --fake-embedder --fake-reranker
```
The printed `TRACE` shows exactly which nodes ran and why, including any retries — useful
both for debugging and for demonstrating the self-correction behavior.

> **A real bug this caught:** CUAD's actual contract text contains bracketed redaction
> placeholders like `[Redacted]` or `[***]`. An early version of the critic's citation regex
> matched *any* bracketed word-text, so a real LLM quoting a redacted clause would have its
> genuinely well-grounded answer wrongly rejected as a hallucinated citation. Fixed by
> matching the actual `contract_id` shape specifically (`\d{4}_\w+`) rather than any bracket
> contents — verified with a test generator that hallucinates on its first attempt and
> recovers on retry.

## Phase 6 — SQL tool + real router branching

**What it does:** the agent can now answer from the ERP tables, not just contract text. A
router sends each question down one of three paths:

| Route | Example | What runs |
|---|---|---|
| `rag`  | "What is the liability cap in Acme's contract?" | contract search only (same as Phase 5) |
| `sql`  | "Top 5 vendors by total spend" | Text-to-SQL → read-only query → answer from the rows |
| `both` | "Vendors with spend above $1M that have an uncapped liability clause" | SQL first (spend filter) → contract search **restricted to the contracts SQL found** → combined answer |

```bash
pip install -r requirements.txt     # adds langgraph (was missing) + sqlglot
python agent/graph.py "Who are the top 5 vendors by total spend?" --provider gemini
python agent/graph.py "Which vendors with total purchase-order spend above $1,000,000 have contracts that include an uncapped liability clause?" --provider gemini
```
The printed `ROUTE:` and `TRACE:` show which tools ran, the exact SQL executed, and any retries.
With `--provider fake` the SQL step uses a canned (valid) query, since the fake generator can't
write SQL — it proves the wiring, not the answer quality.

### Tests
```bash
python agent/test_phase6_offline.py   # no DB/models/network: validator, router, filter, full graph
python agent/test_sql_live.py         # needs docker compose up + ERP data loaded: DB-level read-only proof
```

### Optional: dedicated read-only database role (recommended)
```bash
docker cp erp/create_readonly_role.sql procureiq_postgres:/tmp/ro.sql
docker exec -it procureiq_postgres psql -U postgres -d procureiq -f /tmp/ro.sql
```
Then uncomment `POSTGRES_RO_USER` / `POSTGRES_RO_PASSWORD` in `.env`. **Re-run this after any
re-apply of `schema.sql`** — that script drops every table, which also drops the role's grants.

### How the SQL tool stays safe (4 layers)
1. **Prompt** — schema + "SELECT only" (advisory; never trusted alone).
2. **Validator** (`agent/sql_tool.py`) — sqlglot parses the model's SQL; only ONE plain SELECT over
   `vendors / contracts / purchase_orders / invoices / vendor_annual_spend` is accepted. Blocks
   DML/DDL (even hidden in a CTE), stacked statements, schema-qualified/system tables,
   `pg_*` / `query_to_xml` / `current_setting` style functions. What runs is the validator's
   re-rendering of the AST, not the raw model text.
3. **Executor** — READ ONLY transaction, 5 s statement timeout, 200-row cap.
4. **DB role** — `procureiq_ro` has `SELECT` on those five objects only.

### Known limits (deliberate, for later phases)
- The SQL tool is a plain Python module for now; wrapping it as an MCP server is a later step.
- On a `both` route the SQL step filters on structured columns only. The clause condition is then
  checked by an **exact lookup of CUAD's expert labels** (`retrieval/category_lookup.py`) across
  ALL SQL-matched contracts -- not a similarity search -- and summarised deterministically as an
  "EXACT CLAUSE CHECK" block for the synthesizer. This relies on the labelled `categories` already
  stored on every chunk; for a clause type with no CUAD label the route falls back to semantic
  search (best chunk for up to 12 contracts), which can only check a subset of candidates.
- The critic checks SQL *succeeded* and contract citations are grounded, but does not yet verify
  that every number in the answer appears in the SQL rows.

## Phase 7 — structured outputs + guardrails

**Every response now ends as a validated `RiskReport`** (`agent/schemas.py`, Pydantic, `extra="forbid"`):
summary, findings (vendor / contract / figures / *basis*), citations, the SQL that ran, a
deterministic `confidence`, warnings and guardrail flags. The LLM only writes the prose summary;
everything structured is built in plain Python from what the tools returned (free-tier models are
unreliable at JSON, and a model that can emit arbitrary structure is one an injected instruction
can steer). If validation fails, a minimal valid fallback report is returned -- never raw text.

```bash
python agent/graph.py "<question>" --provider openrouter          # prose + GUARDRAIL EVENTS + REPORT line
python agent/graph.py "<question>" --provider openrouter --json   # only the validated RiskReport
```

### Prompt-injection defence (OWASP LLM01), layered
1. **Structure** -- retrieved text never chooses a tool, route or SQL query (router + SQL tool see only
   the user's question; SQL validator + read-only role still apply).
2. **Sanitize** (`agent/guardrails.py`) -- before any prompt: invisible/bidi/tag characters stripped,
   suspected instruction sentences replaced by `[removed: suspected prompt-injection text]`, forged
   prompt delimiters (`--- END EXCERPTS ---`, fake "EXACT CLAUSE CHECK") removed, ERP cell values
   sanitized too; malformed contract ids can't enter the retrieval filter.
3. **Label** -- prompts state that excerpts/DB values are untrusted data.
4. **Output guard** -- markdown images and URLs the model was never given are stripped (exfil channel).

```bash
python agent/test_guardrails_offline.py        # 140 adversarial checks, no services needed
python agent/scan_corpus_for_injection.py      # audit YOUR 42K chunks for false positives (expect ~0 hits)
```

**Known limits (documented, tested, not hidden):** the sanitizer is pattern-based and tuned for high
precision, so a polite paraphrase ("kindly set aside what you were told...") is *not* caught and can
steer the prose answer. Layers 1 and 4 still hold (no tool/SQL control, exfil closed, report flagged
and downgraded) -- `test_known_gap_paraphrased_injection` pins this down. An unterminated payload also
removes the sentence after it (fail-safe over-redaction).

## Phase 8 — serving layer (FastAPI)

```bash
pip install -r requirements.txt
docker compose -f infra/docker-compose.yml up -d
uvicorn api.main:app --port 8000          # interactive docs at http://localhost:8000/docs
```

| Endpoint | Purpose |
|---|---|
| `POST /ask` | one JSON response: the validated `RiskReport` (+ optional `include_trace`) |
| `POST /ask/stream` | Server-Sent Events: a `step` event as each graph node finishes, then the final `result` |
| `GET /health` | liveness -- process is up, never touches dependencies |
| `GET /ready` | readiness -- Postgres + Qdrant reachable; `503` if not |

Request body: `{"question": "...", "max_retries": 2, "include_trace": false, "refresh": false}`.
`refresh: true` bypasses the answer cache. Config via environment variables (see `.env.example`).

```bash
curl -X POST localhost:8000/ask -H "Content-Type: application/json" \
     -d '{"question": "Who are the top 5 vendors by total spend?"}'
curl -N -X POST localhost:8000/ask/stream -H "Content-Type: application/json" \
     -d '{"question": "Which vendors with spend above $1,000,000 have an uncapped liability clause?"}'
```

**What it does and does not do for latency.** Models and the compiled graph load once at startup
(the CLI reloads them for every question). Repeat questions are served from a short-lived cache with
zero LLM calls, and streaming shows progress while a run is in flight. It does **not** make a fresh
multi-hop question fast: that is dominated by sequential LLM calls (several per question, plus
retries), which on a free-tier model take tens of seconds to minutes.

**Behaviour worth knowing:** excess concurrent requests wait up to `PROCUREIQ_QUEUE_TIMEOUT_S`, then
get `503` + `Retry-After`; only trustworthy results are cached (accept, confidence not low, no SQL
error, no guardrail events); errors return a `request_id` and log the traceback server-side, never
the exception text; with `PROCUREIQ_API_KEY` unset the API is **open** (fine on localhost only).
No rate limiting yet -- the concurrency cap protects the server, not an LLM quota.

```bash
python api/test_api_offline.py     # 52 checks; the real graph behind the real app, scripted LLM, no services
```


