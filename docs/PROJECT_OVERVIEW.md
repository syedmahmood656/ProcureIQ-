# ProcureIQ — Project Overview (Phases 0-3, 5)

This document is a complete map of everything built so far: what each file does, what each
function/class in it does, what data exists on disk and in the databases right now, and how
it all connects. Read this before starting Phase 4 to re-orient.

---

## 1. The one-paragraph version

ProcureIQ answers multi-hop procurement questions ("which vendors with >$500K spend have
contracts auto-renewing next quarter with uncapped liability?") by joining unstructured
contract text with structured spend data. Phase 0 defined the scope. Phase 1 sourced and
loaded the raw data — 510 real contracts (CUAD) and synthetic ERP records (Faker), linked by
`vendor_id`/`contract_id`. Phase 2 turned the raw contract text into a searchable index: 42,350
clause-aware chunks, loaded into both Qdrant (semantic search) and Postgres (keyword search) —
the two halves of hybrid retrieval. Phase 3 built that fusion: dense + keyword search, merged
with Reciprocal Rank Fusion, reranked with a cross-encoder, and wrapped in a simple baseline
RAG endpoint (pluggable across OpenRouter, Hugging Face, and Gemini) that Phase 4's evaluation
will score.

---

## 2. Current data state (as of end of Phase 5)

| Store | Table / Collection | Row / point count | What it is |
|---|---|---|---|
| Postgres | `vendors` | 120 | Synthetic vendor master data |
| Postgres | `cuad_documents` | 510 | Registry of every CUAD contract (title, source file, size, clause count) |
| Postgres | `contracts` | 120 | Subset of `cuad_documents` linked to a vendor + synthetic dates |
| Postgres | `purchase_orders` | 1,063 | Synthetic POs, 3-15 per vendor |
| Postgres | `invoices` | 1,079 | Synthetic invoices against POs |
| Postgres | `contract_chunks` | 42,350 | Clause-aware chunks with `tsvector` full-text index |
| Qdrant | `contract_chunks` | 42,350 — **real embeddings confirmed loaded on your machine** (verified via a real Gemini-backed `baseline_rag.py` run) | Same chunks, dense-vector index |
| Disk | `data/raw/cuad/contracts/*.txt` | 510 files | Full text of every CUAD contract |
| Disk | `data/raw/cuad/clauses.jsonl` | 13,823 lines | Every expert-labeled clause span from CUAD |
| Disk | `data/processed/chunks.jsonl` | 42,350 lines | Output of the clause-aware chunker — the shared source for both Qdrant and Postgres loads |

---

## 3. Repo tree
```
procureiq/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── docs/
│   ├── PRD.md
│   └── ARCHITECTURE.md
├── scripts/
│   └── extract_cuad_contracts.py
├── erp/
│   ├── schema.sql
│   ├── generate_synthetic_erp.py
│   └── load_erp_to_postgres.py
├── ingestion/
│   ├── clause_chunker.py
│   ├── embedder.py
│   ├── build_qdrant_index.py
│   ├── load_cuad_documents.py
│   └── load_fulltext_postgres.py
├── retrieval/
│   ├── keyword_retriever.py
│   ├── dense_retriever.py
│   ├── fusion.py
│   ├── reranker.py
│   ├── hybrid_retriever.py
│   ├── generator.py
│   └── baseline_rag.py
├── agent/
│   ├── state.py
│   ├── nodes.py
│   └── graph.py
├── infra/
│   └── docker-compose.yml
└── data/                          (generated — gitignored)
    ├── raw/cuad/
    └── processed/
```

---

## 4. Phase 0 — Architecture & Setup

### `docs/PRD.md`
No functions (it's a spec document). Contents: problem statement, goal, target users (for
framing only), scope table (what's in/out of v1 — explicitly documents that Kafka/Debezium,
Kubernetes, Terraform, and human-approval workflows were cut from the original flagship
blueprint), the two data sources and why each was chosen, three example queries the system
must eventually answer, and success criteria to benchmark against later (not numbers claimed
yet).

### `docs/ARCHITECTURE.md`
No functions. Contents: a Mermaid diagram splitting the system into three planes — **Data
Plane** (offline: chunking → embeddings → Qdrant, plus Postgres full-text and synthetic ERP),
**Reasoning Plane** (per-request: LangGraph planner → router → RAG/SQL/risk tools →
synthesizer → critic, Phase 5-9), **Serving Plane** (FastAPI, Redis, Langfuse, auth, Phase
10-13) — plus a table mapping every component to the phase that builds it, and a short
rationale for why CUAD + synthetic ERP specifically (the 41 labeled clause categories double
as chunking anchors *and* a near-automatic path to the Phase 4 gold-question set).

---

## 5. Phase 1 — Procurement Data

### `scripts/extract_cuad_contracts.py`
Pulls the raw `CUADv1.json` (SQuAD-style: one paragraph per contract, with the full contract
text as `context` and 41 clause-category question/answer spans) apart into clean, per-contract
files.
- **`slugify(title)`** — turns a messy contract title into a filesystem-safe slug (strips
  non-alphanumerics, caps length at 120 chars).
- **`main(source_path, out_dir)`** — the whole pipeline: loops over all 510 contracts, writes
  each one's full text to `contracts/<contract_id>.txt`, collects every non-impossible clause
  answer span into a list, then writes two summary outputs: `contracts_metadata.csv`
  (contract_id, title, source_file, char_len, num_labeled_clauses) and `clauses.jsonl` (one row
  per labeled clause span: contract_id, category, clause_text, char_start).

### `erp/schema.sql`
Not Python — DDL. Current tables (after the Phase 2 update):
- **`vendors`** — vendor_id (PK), vendor_name, category, country, risk_tier (defaults
  `'UNKNOWN'`, meant to be populated by a future risk-monitor tool), created_at.
- **`cuad_documents`** *(added in Phase 2)* — registers all 510 CUAD contracts regardless of
  whether they're linked to a vendor: contract_id (PK), cuad_title, source_file, char_len,
  num_labeled_clauses.
- **`contracts`** — the 120 vendor-linked contracts. contract_id (PK, **FK →
  cuad_documents**), vendor_id (FK → vendors), cuad_title, source_file, effective_date,
  expiration_date, auto_renewal.
- **`purchase_orders`** — po_id (PK), vendor_id (FK), po_date, amount, currency, status.
- **`invoices`** — invoice_id (PK), po_id (FK), vendor_id (FK), invoice_date, amount,
  paid_date, status.
- **`contract_chunks`** *(added in Phase 2)* — chunk_id (PK), contract_id (FK →
  cuad_documents), categories (TEXT[]), char_start, char_end, chunk_text, and a **generated**
  `search_vector tsvector` column (auto-computed from chunk_text via `to_tsvector('english',
  ...)`, stored, not recomputed on every query).
- Indexes: B-tree on every FK column, GIN on `contract_chunks.categories` (array containment
  lookups) and `contract_chunks.search_vector` (full-text search).
- **View `vendor_annual_spend`** — vendor_id, vendor_name, spend_year, total_spend, grouped
  from `purchase_orders`. This is exactly the kind of number the SQL tool will compute at
  query time in Phase 6-7.

### `erp/generate_synthetic_erp.py`
Faker-based generator that produces realistic (clearly synthetic) ERP data and links a subset
of vendors to real CUAD contracts.
- **`load_cuad_contract_ids(metadata_csv)`** — reads `contracts_metadata.csv` into a list of
  dicts (contract_id, title, source_file) so real contract IDs can be assigned to vendors.
- **`main(n_vendors, seed, cuad_metadata, out_dir)`** — the generator loop. For each of
  `n_vendors`: creates a vendor (Faker company name, random category/country); if its index is
  within `n_with_contracts` (min of n_vendors and available CUAD contracts), attaches one real,
  never-fabricated CUAD contract_id with synthetic effective/expiration dates and an
  auto-renewal flag; generates 3-15 purchase orders per vendor across the last 2 years
  ($5K-$250K each); generates 0-2 invoices per PO with realistic pay/overdue status. Writes
  four CSVs via `_write_csv`.
- **`_write_csv(path, rows)`** — plain CSV writer helper, no-ops on an empty row list.

### `erp/load_erp_to_postgres.py`
Loads the CSVs from the generator into Postgres, in FK-safe order.
- **`get_conn()`** — opens a `psycopg2` connection using `POSTGRES_*` env vars (defaults to
  localhost/procureiq/postgres/postgres), so the same script works unchanged against local,
  Docker, or cloud Postgres.
- **`load_csv(cur, table, csv_path, columns)`** — generic CSV → `INSERT` helper; converts empty
  strings to `NULL`, builds the parameterized `INSERT` from the given column list, and
  `executemany`s it.
- **`main()`** — loads `vendors` → `contracts` → `purchase_orders` → `invoices` in that order
  (respecting foreign keys), resetting each table's `SERIAL` sequence after loading explicit
  IDs so future inserts don't collide, and commits once at the end (rolls back the whole batch
  on any failure).

### `infra/docker-compose.yml`
Not Python. Currently defines two services: `postgres` (image `postgres:16`, auto-runs
`erp/schema.sql` on first boot via the init-scripts volume mount) and, as of Phase 2, `qdrant`
(image `qdrant/qdrant:v1.12.4`, REST on 6333 / gRPC on 6334). Named volumes persist data across
container restarts.

### `.env.example`, `requirements.txt`, `.gitignore`
No functions. `.env.example` documents the five Postgres connection variables every script
reads. `requirements.txt` pins `Faker`, `psycopg2-binary`, `python-dotenv` (Phase 1) plus
`qdrant-client` and `sentence-transformers` (Phase 2). `.gitignore` excludes everything
regenerable — the CUAD source clone, extracted contract `.txt` files, generated CSVs,
`chunks.jsonl`, and any local Qdrant storage — so the repo itself stays small; only code and
docs are meant to be committed.

---

## 6. Phase 2 — Document Ingestion

### `ingestion/clause_chunker.py`
The core of Phase 2: turns 510 raw contract text files into 42,350 chunks that respect clause
boundaries instead of naive fixed-size splitting.
- **`load_clauses(clauses_path)`** — reads `clauses.jsonl`, groups every labeled span by
  contract_id, and computes each span's end offset (`char_start + len(clause_text)`).
- **`merge_spans(spans, gap_tolerance=50)`** — sorts a contract's clause spans by position and
  merges any that overlap or sit within `gap_tolerance` characters of each other, unioning
  their category labels (a single sentence can legitimately belong to two categories, e.g. both
  "Cap On Liability" and "Uncapped Liability" show up as adjacent spans in some contracts).
- **`expand_to_paragraph(text, start, end)`** — grows a merged span outward to the nearest
  paragraph break (blank line) in each direction, capped at `MAX_EXPAND` (400 chars), so a
  chunk has enough surrounding context to stand alone instead of being a bare answer fragment.
- **`pack_fallback_chunks(text, gap_start, gap_end)`** — for the *uncovered* text between (or
  before/after) clause chunks, packs paragraphs together up to `FALLBACK_CHUNK_SIZE` (800
  chars) each; falls back to fixed-size slicing only if a single paragraph itself exceeds 1.5×
  that size (rare, but some CUAD contracts have no paragraph breaks at all).
- **`chunk_contract(contract_id, text, clause_spans)`** — orchestrates the above for one
  contract: merge spans → expand each to paragraph boundaries → re-merge any that now overlap
  after expansion → walk through in order, filling every gap with fallback chunks → return the
  final ordered list of chunk dicts (contract_id, chunk_id, categories, char_start, char_end,
  text).
- **`main(contracts_dir, clauses_jsonl, out_path)`** — runs `chunk_contract` over every `.txt`
  file in `contracts_dir`, writes results to `chunks.jsonl`, and prints summary stats (total
  chunks, clause-anchored vs. GENERAL fallback split, average chunk length, average
  chunks/contract). Current output: 42,350 chunks (6,235 clause-anchored / 36,115 fallback),
  ~629 chars average.

### `ingestion/embedder.py`
One interface (`embed(texts) -> list[list[float]]`), two interchangeable implementations.
- **`SentenceTransformerEmbedder`** — the production embedder.
  - `__init__(model_name="BAAI/bge-small-en-v1.5")` — loads the model via
    `sentence-transformers` (downloads weights from Hugging Face Hub on first use — requires
    normal internet access, which this sandbox does not have); stores the model's native
    embedding dimension.
  - `embed(texts)` — encodes a batch of texts with `normalize_embeddings=True` (so cosine
    similarity reduces to a dot product).
- **`FakeEmbedder`** — offline, dependency-free stand-in used only to test pipeline plumbing.
  - `__init__(dim=384)` — sets the output dimension (matches bge-small's real dimension, so
    downstream code doesn't need to branch on which embedder is active).
  - `embed(texts)` — maps each text to a deterministic vector via `_hash_vector`.
  - `_hash_vector(text)` — repeatedly SHA-256-hashes the text, converts each 4-byte chunk of
    hash output to an integer and rescales it into `[-1, 1]` (deliberately *not* reinterpreting
    raw bytes as IEEE-754 floats — that approach can silently produce NaN bit patterns, which
    is exactly the bug this file's first version had and which Qdrant's insert validator
    caught immediately), then L2-normalizes the result. Same text always → same vector, but the
    vectors carry no real semantic meaning — similarity search on them is meaningless, only
    useful for confirming data flows correctly.

### `ingestion/build_qdrant_index.py`
Loads `chunks.jsonl` into a Qdrant collection, real or fake vectors.
- **`get_client(url, local_path)`** — returns a server-backed `QdrantClient` if `url` is given,
  otherwise a file-based local client (no server needed at all — useful for offline testing,
  but Qdrant warns against local mode above ~20K points, which this corpus already exceeds at
  42,350).
- **`load_chunks(path)`** — reads `chunks.jsonl` into a list of dicts.
- **`main(chunks_path, url, local_path, use_fake_embedder, model_name)`** — picks the embedder
  class based on `use_fake_embedder`, drops and recreates the `contract_chunks` collection
  (cosine distance, dimension taken from the embedder), then batches through all chunks
  (`BATCH_SIZE=128`): embeds each batch's text, builds `PointStruct`s carrying the vector plus
  a payload (contract_id, chunk_id, categories, char_start, char_end, text), and upserts.
  Prints progress every 20 batches and a final point count.

### `ingestion/load_cuad_documents.py`
- **`main(metadata_csv)`** — reads `contracts_metadata.csv` (all 510 rows) and bulk-inserts
  into the new `cuad_documents` table. **Must run before** `erp/load_erp_to_postgres.py`, since
  `contracts.contract_id` now has a foreign key into this table.

### `ingestion/load_fulltext_postgres.py`
- **`main(chunks_path)`** — reads `chunks.jsonl` (all 42,350 rows) and bulk-inserts into
  `contract_chunks` using `psycopg2.extras.execute_values` (batched at `BATCH_SIZE=1000` for
  speed on a 42K-row insert). The `search_vector` column populates itself automatically — it's
  a Postgres *generated* column, never written directly.

### Updated `infra/docker-compose.yml`
Added the `qdrant` service (see Phase 1 section above — documented once, applies to the
current file).

### Updated `requirements.txt`
Added `qdrant-client==1.12.1` and `sentence-transformers==3.3.1`.

### Updated `README.md`
Added the Phase 2 status row, a full run-through section (chunk → load full-text → load
Qdrant, with the fake-embedder caveat spelled out), an updated repo-structure diagram, and
corrected the Phase 1 run order (document registry must load before the ERP tables now).

---

## 7. Phase 3 — Advanced Retrieval + Baseline RAG

### `retrieval/fusion.py`
- **`reciprocal_rank_fusion(ranked_lists, k=60, id_key="chunk_id")`** — merges any number of
  independently-ranked result lists into one, scoring each item by
  `sum(1 / (k + rank))` across every list it appears in — deliberately ignoring each list's raw
  score (cosine similarity and `ts_rank` aren't on comparable scales) and using only rank
  position. Returns items sorted by fused score, each tagged with `rrf_score` and
  `source_lists` (which input lists contributed to it).

### `retrieval/keyword_retriever.py`
- **`keyword_search(query, top_k=20)`** — runs `websearch_to_tsquery('english', query)` against
  `contract_chunks.search_vector`, ranked by `ts_rank`. Chosen over `plainto_tsquery`/
  `to_tsquery` specifically because it tolerates raw natural-language input (punctuation,
  quoted phrases, `-exclude` terms) without raising a syntax error.

### `retrieval/dense_retriever.py`
- **`dense_search(query, embedder, client, top_k=20)`** — embeds the query via
  `embedder.embed_query()` (not `embed()` — see the embedder.py update below) and searches the
  `contract_chunks` Qdrant collection via `client.query_points()`, returning the same
  dict shape as `keyword_search` (`chunk_id`, `contract_id`, `categories`, `text`, `score`) so
  both are drop-in interchangeable inputs to fusion.

### `retrieval/reranker.py`
- **`CrossEncoderReranker.rerank(query, candidates)`** — scores every `(query, candidate_text)`
  pair with a cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2` by default), which sees
  query and document together (unlike the bi-encoder used for dense search) and is meaningfully
  more accurate — but too slow to run against the whole corpus, hence only applied to the
  already-fused shortlist.
- **`FakeReranker.rerank(...)`** — same interface, deterministic hash-based scoring, no model
  download. Used to prove sort/slice logic is correct before a real model is available.

### `retrieval/hybrid_retriever.py`
- **`hybrid_retrieve(query, qdrant_client, embedder, reranker, dense_k=20, keyword_k=20, final_k=8)`**
  — the full pipeline: dense search + keyword search (run independently) → `reciprocal_rank_fusion`
  → `reranker.rerank()` → slice to `final_k`. This is the single function every downstream
  consumer (baseline RAG now, the agentic pipeline later) calls to get context chunks.

### `retrieval/generator.py`
One interface (`generate(prompt) -> str`), four backends, selected via `get_generator(provider)`:
- **`OpenRouterGenerator`** — OpenAI-compatible client pointed at `openrouter.ai`, so any model
  OpenRouter hosts (including free-tier open models) is just a `model=` string change. Reads
  `OPENROUTER_API_KEY`.
- **`HuggingFaceGenerator`** — `huggingface_hub.InferenceClient.chat_completion` against an
  open-weight model. Reads `HF_TOKEN`.
- **`GeminiGenerator`** — Google's `google-genai` client. Reads `GEMINI_API_KEY`.
- **`FakeGenerator`** — no network, no key: regex-extracts the `[contract_id]` tags from the
  prompt's excerpts block and echoes them in a canned response, proving the citation plumbing
  end-to-end without spending any API credits.
> None of the three real backends could be exercised in this sandbox (OpenRouter, HF Inference,
> and Gemini are all outside the network allowlist here) — only `FakeGenerator` has actually
> been run. Test the real providers on your machine.

### `retrieval/baseline_rag.py`
- **`build_prompt(question, chunks)`** — formats retrieved chunks into a `[contract_id]`-tagged
  context block plus a strict instruction ("use ONLY these excerpts, cite every claim, say so
  if the answer isn't here") — this exact prompt is what Phase 4's evaluation will score.
- **`answer_question(question, qdrant_client, embedder, reranker, generator, top_k=8)`** — calls
  `hybrid_retrieve`, builds the prompt, calls the generator, and returns a dict with the answer,
  the prompt (kept for eval logging), and a `sources` list for citation checking.

### Updated `ingestion/embedder.py`
Added **`embed_query(text)`** to both embedder classes (`SentenceTransformerEmbedder` and
`FakeEmbedder`) alongside the existing `embed(texts)`. BGE-family models are trained
asymmetrically — passages are indexed as-is, but queries need an instruction prefix
(`"Represent this sentence for searching relevant passages: "`) at search time for good
retrieval quality. Using `embed()` for both indexing and querying (the Phase 2 state) would
have been a silent quality bug once Phase 3 started searching.

### Updated `requirements.txt`, `.env.example`
Added `openai` (also serves OpenRouter, which is OpenAI-API-compatible), `huggingface_hub`,
`google-genai`. `.env.example` gained `OPENROUTER_API_KEY`, `HF_TOKEN`, `GEMINI_API_KEY` —
only the one(s) you actually use need a real value.

---

## 8. Phase 5 — Agentic Pipeline (LangGraph)

> Phase 4 (gold question set + RAGAS/DeepEval scoring of the Phase 3 baseline) was
> deliberately skipped for now, at your direction, in favor of moving straight to the
> agentic pipeline. It remains a good next step whenever you want a rigorous, numeric
> baseline-vs-agentic comparison rather than the spot-checks used so far.

New folder: `agent/`. This phase wraps Phase 3's linear baseline (retrieve → prompt →
generate) in a LangGraph state machine with a genuine feedback loop — the first actually
*agentic* behavior in the project (as opposed to a single-pass pipeline).

### `agent/state.py`
`AgentState` (a `TypedDict`) — the shared state every node reads from and writes back to.
Key fields beyond the obvious (`question`, `retrieved_chunks`, `answer`): `search_query`
(separate from `question` because the planner rewrites this on retry, while the original
question is preserved for the final prompt), `route` (router's decision — always `"rag"` in
Phase 5, but the field exists so Phase 6-7 can extend it without restructuring the graph),
`verdict`/`critic_feedback`/`retry_count` (the loop's control state), and `trace` (a running
human-readable log of every node's decision, used for debugging and for demonstrating the
retry behavior).

### `agent/nodes.py`
Every node is a **factory function** (`make_x_node`) that closes over external dependencies
(generator, Qdrant client, embedder, reranker) and returns the actual `(state) -> dict`
function LangGraph calls — chosen over LangGraph's own config/context-passing conventions for
explicitness and testability.
- **`make_planner_node(generator)`** — on the first pass, `search_query = question` verbatim.
  On retry, prompts the generator to suggest a different search query given the question, the
  previous query, and the critic's feedback on why it failed.
- **`make_router_node()`** — always returns `route="rag"` in Phase 5 (only one tool exists
  yet); the node and its conditional edge already exist so Phase 6-7's SQL tool addition is
  an extension, not a restructure.
- **`make_rag_tool_node(qdrant_client, embedder, reranker)`** — thin wrapper around Phase 3's
  `hybrid_retrieve()`.
- **`make_synthesizer_node(generator)`** — reuses Phase 3's `build_prompt()` from
  `baseline_rag.py` (no duplicated prompt logic) and calls the generator.
- **`make_critic_node()`** — the key new piece: a **deterministic** grounding check, no extra
  LLM call. Extracts every `[contract_id]`-shaped citation from the answer (regex matches the
  exact `\d{4}_\w+` format `extract_cuad_contracts.py` always produces) and checks each one
  against the set of contract_ids actually retrieved. Any citation not in that set, or zero
  citations at all, or an explicit "couldn't find this" signal in the answer text → `"retry"`
  (with `critic_feedback` explaining why). Once `retry_count` hits `max_retries`, downgrades
  to `"give_up"` (stop looping, return the best-effort answer) instead of retrying forever.

### `agent/graph.py`
- **`build_agent_graph(qdrant_client, embedder, reranker, generator)`** — wires the five nodes
  into a `StateGraph(AgentState)`: `START → planner → router →`(conditional, currently only
  `"rag"`)`→ rag_tool → synthesizer → critic →`(conditional: `"retry"` loops back to
  `planner`, `"accept"`/`"give_up"` go to `END`). Compiles and returns the runnable graph.
- **`run_agent(question, qdrant_client, embedder, reranker, generator, max_retries=2)`** —
  builds the graph, seeds the initial `AgentState`, and calls `graph.invoke()`. This is the
  single entry point later phases (and eventually FastAPI serving, Phase 10) call.
- CLI entry point mirrors `baseline_rag.py`'s flags (`--provider`, `--url`/`--local-path`,
  `--fake-embedder`/`--fake-reranker`) plus `--max-retries`, and prints the full `trace` so a
  retry loop is visible when it happens.

### A real bug this phase's testing caught
CUAD's actual contract text contains bracketed redaction placeholders — `[Redacted]`, `[***]`
— visible in the Phase 3 Gemini test output too. An early version of the critic's citation
regex (`\[([\w\-]+)\]`) matched *any* bracketed word-text, so a real LLM quoting a redacted
clause verbatim (entirely plausible, and something Gemini already demonstrated doing) would
have a genuinely well-grounded answer wrongly rejected as a hallucinated citation, wasting a
retry on a false positive. Fixed by tightening the regex to the actual contract_id shape
(`\[(\d{4}_\w+)\]`) in both `agent/nodes.py`'s critic and `retrieval/generator.py`'s
`FakeGenerator`. Verified with a purpose-built test generator that hallucinates a fake
citation on its first attempt (correctly triggers a retry) and produces a valid, `[Redacted]`-
containing answer on its second attempt (correctly accepted, not falsely rejected).

### Updated `requirements.txt`
Added `langgraph==1.2.12`.

---

## 9. What was tested vs. what still needs a real run (all phases)

| Claim | Verified how |
|---|---|
| CUAD extraction is correct | Ran on real data: 510 contracts, 13,823 clause spans, 26.8M chars total |
| Synthetic ERP data loads without FK errors | Ran end-to-end against live local Postgres |
| The vendor↔contract↔spend join actually works | Ran the real query from the PRD; returned real joined rows |
| Clause-aware chunking produces coherent, non-truncated clauses | Spot-checked a "Cap On Liability" chunk — complete sentence, correct category |
| Qdrant plumbing (schema, batching, payload, metadata filtering) is correct | Loaded all 42,350 chunks with `FakeEmbedder`, confirmed category filter returned 482 correct matches |
| Postgres full-text search returns correctly ranked results | Ran `ts_rank`/`websearch_to_tsquery` queries — top hits were genuine, correctly-categorized clauses |
| RRF fusion logic is correct | Pure-logic smoke test — item appearing in both input lists ranked first, as expected |
| Keyword vs. dense search have genuinely different blind spots | Confirmed directly: a long conversational query returned 0 keyword hits while a short targeted one returned correct results — the exact gap dense search is meant to cover |
| Dense retrieval plumbing (`query_points`, payload parsing) | Ran against a real 42,350-point local Qdrant collection with `FakeEmbedder` — structurally correct, not semantically meaningful yet |
| Full hybrid pipeline wiring (dense + keyword → fusion → rerank → slice) | Ran end-to-end with fake embedder + fake reranker; every stage's output fed the next correctly |
| Baseline RAG end-to-end (retrieve → prompt → generate → cite) | Ran with `FakeGenerator`; caught and fixed a regex bug that matched the prompt template's own `[contract_id]` placeholder text |
| **Real semantic search quality** (real embeddings) | ✅ **Verified on your machine** — real `bge-small-en-v1.5` embeddings + real Qdrant search via `baseline_rag.py --provider gemini`, returned correctly relevant liability clauses |
| **Real reranking quality** (real cross-encoder) | ✅ **Verified on your machine** — same run used the real `CrossEncoderReranker` (no `--fake-reranker` flag passed) |
| **Real generation quality** (Gemini) | ✅ **Verified on your machine** — produced a correctly grounded, correctly cited answer, including correctly noting the redacted `[***]` dollar figure in the source contract rather than inventing one. OpenRouter and Hugging Face backends are still unverified. |
| LangGraph state machine wiring, incl. the retry loop | Ran end-to-end with `FakeEmbedder`/`FakeReranker`/`FakeGenerator`: happy path accepted on the first pass; a purpose-built hallucinating test generator correctly triggered a retry, and correctly succeeded on the second attempt after the (simulated) planner rewrite |
| Critic's citation-grounding check doesn't false-positive on redacted text | Confirmed directly — see the bug writeup above |
| **The agentic pipeline (`agent/graph.py`) with real models** | **Not yet verified** — only tested with fake components so far; real-model validation is the next thing to do on your machine |

## 10. Before Phase 6-7

On your machine, with real embeddings already loaded and a provider key set:
```bash
python agent/graph.py "what is the liability cap for our vendors" --provider gemini
```
Check the printed `TRACE` — on a well-grounded first answer it should show `verdict=accept`
with 0 retries; if you want to see the loop actually fire, try a deliberately vague or
out-of-corpus question and watch the planner rewrite the query.

Once that's confirmed working end-to-end with real models, Phase 6-7 adds a SQL tool (MCP,
read-only, schema-constrained) over the ERP tables, and the router in `agent/nodes.py`
becomes a real multi-tool decision instead of always returning `"rag"`.
