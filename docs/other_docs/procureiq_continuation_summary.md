# ProcureIQ — Continuation Summary (for a fresh chat session)

Paste this whole document as the first message in a new chat to resume exactly where this
one left off.

---

## 1. Core Objective

Building **ProcureIQ**, a portfolio-grade agentic RAG system that answers multi-hop
procurement questions by joining unstructured contract text with structured spend/vendor data
(e.g. "which vendors with >$500K spend have contracts auto-renewing next quarter with uncapped
liability?"). This is a flagship AI/LLM Engineer portfolio project, built phase-by-phase
(Phase 0 through Phase 13), with Claude acting as build partner: writing code, testing it
against real data before handing it off, and documenting everything so the human user (a
4th-year AI/ML B.Tech student, Sree Chaitanya Institute of Technological Sciences, JNTUH)
can run it locally and understand every piece for interviews.

**Origin context (earlier in this conversation, not part of the active build thread):** the
user's resume was reviewed in depth (strong RAG/agentic project portfolio, but gaps flagged:
no team/OSS experience, unquantified bullets, no self-hosted LLM serving, no alignment/DPO
work beyond SFT). ProcureIQ was recommended as the single highest-leverage project to close
most of those gaps at once. A separate AI/ML internship tracker (Excel file, 26 programs
across Big Tech/Other/Startups) was also built earlier and is a finished, unrelated deliverable
— not part of the ProcureIQ thread.

## 2. Key Decisions & Rules Established

- **Scope was deliberately reduced** from an original "flagship" blueprint: Kafka/Debezium CDC,
  Kubernetes, Terraform, and human-in-the-loop approval workflows were explicitly cut. This is
  documented in `docs/PRD.md` as a conscious v1 scope decision, not an oversight.
- **Data sources**: CUAD (Contract Understanding Atticus Dataset — 510 real commercial
  contracts, 41 expert-labeled clause categories, 13,823 labeled clause spans, from
  `github.com/TheAtticusProject/cuad`) + Faker-generated synthetic ERP data (vendors, POs,
  invoices), with 120 vendors linked 1:1 to real CUAD contracts. Synthetic data is always
  clearly labeled as such, never presented as real.
- **Every offline-untestable component gets a "Fake" stand-in** behind the same interface as
  the real one (`FakeEmbedder`/`SentenceTransformerEmbedder`, `FakeReranker`/
  `CrossEncoderReranker`, `FakeGenerator`/real LLM generators) — this pattern is established
  and should be followed for any new component that needs a model/API Claude's sandbox can't
  reach.
- **Claude's sandbox network is restricted** (pypi, github, npm, api.anthropic.com only — no
  Hugging Face, no OpenRouter, no Gemini, no Google APIs). Anything needing those must be
  written correctly by Claude, tested only via the Fake stand-ins in-sandbox, and validated for
  real by the user on their own machine (which has full internet access).
- **Loaders must be idempotent.** All Postgres loader scripts use `ON CONFLICT DO NOTHING` —
  this was a real bug found and fixed after a rerun crashed with a duplicate-key error.
- **`.env` files require an explicit `load_dotenv()` call** — this was a real bug (present in
  three scripts) found and fixed; don't assume `python-dotenv` being installed is sufficient.
- **`erp/schema.sql` is destructive** (`DROP TABLE ... CASCADE` on everything) — clearly
  documented in the README with the exact recovery sequence, because the user hit this for
  real (re-ran schema.sql mid-session, wiping `cuad_documents`, causing a downstream FK
  violation).
- **User's environment**: Windows, PowerShell, VS Code, Python venv, Docker Desktop for
  Postgres + Qdrant (not native installs). Key gotcha already solved: no local `psql` client
  exists on Windows when using Docker for Postgres — use
  `docker exec -it procureiq_postgres psql -U postgres -d procureiq ...` instead. This is
  documented as a callout in `README.md`.
- **BGE embedding models need an instruction prefix at query time only**, not at index time —
  implemented as `embed_query()` vs `embed()` in `ingestion/embedder.py`. This was caught and
  fixed before it became a silent quality bug.
- **Generation providers**: user chose **OpenRouter, Hugging Face (open models), and Gemini**
  — all three implemented behind one `generate(prompt) -> str` interface in
  `retrieval/generator.py`, selectable via `--provider`. (Anthropic/Claude was offered but not
  chosen.)
- **Qdrant client/server version skew** (client auto-installed newer than the pinned Docker
  server image) is handled with `check_compatibility=False` on every remote `QdrantClient(...)`
  call — cosmetic warning, not a functional issue, confirmed still returns correct results.
- Claude's working style established in this thread: **build → test in sandbox with real data
  wherever possible → report exactly what was verified vs. what still needs a real run on the
  user's machine → package as a zip via `present_files`** after every phase or fix. The user
  has responded very well to Claude finding and fixing its own bugs transparently (e.g. the
  `FakeEmbedder` NaN bug, the invoice-ID-never-incremented bug, the `[contract_id]` regex
  false-positive bug) — keep doing this rather than glossing over issues.
- Documentation is treated as a first-class, continuously-updated deliverable:
  `docs/PRD.md`, `docs/ARCHITECTURE.md`, `docs/PROJECT_OVERVIEW.md` (file-by-file,
  function-by-function map of the whole repo, updated after every phase), and `README.md`
  (phase status table + exact run instructions, Local track and Docker track for every phase).

## 3. Important Context / Code / Repo State

**Repo root:** `procureiq/` (delivered to the user as a zip after every phase; local repo also
maintained in Claude's sandbox at `/home/claude/procureiq`). User's local path:
`C:\Projects\Procure-IQ`.

**Current repo tree (through Phase 3, all delivered and working):**
```
procureiq/
├── README.md                      # phase status table, Local+Docker run instructions, gotcha callouts
├── requirements.txt                # Faker, psycopg2-binary, python-dotenv, qdrant-client==1.12.1,
│                                    # sentence-transformers==3.3.1, openai, huggingface_hub, google-genai
├── .env.example                    # POSTGRES_*, OPENROUTER_API_KEY, HF_TOKEN, GEMINI_API_KEY
├── .gitignore
├── docs/
│   ├── PRD.md                      # problem statement, scope (incl. what was cut), success criteria
│   ├── ARCHITECTURE.md             # Mermaid diagram, 3 planes (Data/Reasoning/Serving), phase mapping
│   └── PROJECT_OVERVIEW.md         # full file-by-file, function-by-function map (Phases 0-3), row counts,
│                                    # a "what was tested vs. not" table, "before next phase" instructions
├── scripts/
│   └── extract_cuad_contracts.py   # slugify(), main() — CUADv1.json -> per-contract .txt + metadata.csv + clauses.jsonl
├── erp/
│   ├── schema.sql                  # DESTRUCTIVE (drops everything). Tables: vendors, cuad_documents (all 510),
│   │                                # contracts (120, FK->cuad_documents), purchase_orders, invoices,
│   │                                # contract_chunks (FK->cuad_documents, generated tsvector + GIN index),
│   │                                # view vendor_annual_spend
│   ├── generate_synthetic_erp.py   # Faker-based; 120 vendors, 1063 POs, 1079 invoices; --n-vendors --seed
│   └── load_erp_to_postgres.py     # idempotent (ON CONFLICT DO NOTHING), loads .env via load_dotenv()
├── ingestion/                       # Phase 2
│   ├── clause_chunker.py           # merge_spans(), expand_to_paragraph(), pack_fallback_chunks(),
│   │                                # chunk_contract(), main() -> 42,350 chunks (6,235 clause-anchored)
│   ├── embedder.py                 # SentenceTransformerEmbedder (BAAI/bge-small-en-v1.5, has embed() AND
│   │                                # embed_query() w/ BGE instruction prefix) + FakeEmbedder (hash-based,
│   │                                # NaN bug already fixed)
│   ├── build_qdrant_index.py       # loads chunks.jsonl into Qdrant collection "contract_chunks";
│   │                                # --url / --local-path / --fake-embedder flags; check_compatibility=False
│   ├── load_cuad_documents.py      # idempotent; loads all 510 into cuad_documents (MUST run before erp loader)
│   └── load_fulltext_postgres.py   # idempotent; loads chunks.jsonl into Postgres contract_chunks
├── retrieval/                       # Phase 3
│   ├── keyword_retriever.py        # keyword_search() via websearch_to_tsquery + ts_rank
│   ├── dense_retriever.py          # dense_search() via Qdrant query_points() + embed_query()
│   ├── fusion.py                   # reciprocal_rank_fusion() — pure logic, k=60 default, tested standalone
│   ├── reranker.py                 # CrossEncoderReranker (cross-encoder/ms-marco-MiniLM-L-6-v2) + FakeReranker
│   ├── hybrid_retriever.py         # hybrid_retrieve() orchestrates dense+keyword->RRF->rerank->slice
│   ├── generator.py                # OpenRouterGenerator, HuggingFaceGenerator, GeminiGenerator, FakeGenerator,
│   │                                # get_generator(provider) factory; load_dotenv() present
│   └── baseline_rag.py             # build_prompt(), answer_question() — the non-agentic yardstick for Phase 4
├── infra/
│   └── docker-compose.yml          # postgres:16 (auto-runs schema.sql on fresh volume) + qdrant/qdrant:v1.12.4
└── data/                            # generated, gitignored: raw/cuad/ (510 .txt + metadata.csv + clauses.jsonl),
                                      # processed/ (chunks.jsonl, erp/*.csv)
```

**Current data state (last confirmed):** 120 vendors, 510 `cuad_documents`, 120 `contracts`,
1,063 purchase_orders, 1,079 invoices, 42,350 `contract_chunks` in Postgres AND in Qdrant (user
has now loaded **real** BGE embeddings on their own machine — confirmed working via a real,
correctly-cited Gemini-generated answer).

**Validated end-to-end on the user's real machine (not just Claude's sandbox):** ran
`retrieval/baseline_rag.py "what is the liability cap for our vendors" --provider gemini` and
got a correct, well-grounded, properly-cited answer pulling from 4 distinct real contracts,
correctly preserving `[***]` redactions rather than hallucinating numbers.

**Known bugs found and fixed during this build (all already patched in the delivered zip):**
1. `FakeEmbedder` reinterpreted raw hash bytes as IEEE-754 floats → could produce NaN vectors →
   fixed to build floats from safe integer ranges instead.
2. `generate_synthetic_erp.py`'s `invoice_id_counter` was never incremented → every invoice got
   `invoice_id=1` → FK/PK violation on load → fixed.
3. Three Postgres loader scripts read `os.getenv()` for `.env` values but never called
   `load_dotenv()` → `.env` file was silently ignored → fixed in all three.
4. All three Postgres loaders crashed on a second run (plain `INSERT`, no conflict handling) →
   added `ON CONFLICT DO NOTHING` to all three.
5. `FakeGenerator`'s citation-extraction regex matched the prompt template's own literal
   `[contract_id]` placeholder text, not just real contract IDs → fixed to only scan within the
   `--- CONTRACT EXCERPTS ---` block.
6. `ingestion/embedder.py` lacked a query-time-only instruction prefix for BGE models (would
   have silently hurt retrieval quality) → added `embed_query()`.
7. Qdrant client/server version-skew warning (client 1.19.1 vs. pinned server 1.12.4) → added
   `check_compatibility=False` to all remote `QdrantClient(...)` constructions.

**Open, non-blocking observation flagged to the user (not yet fixed, deliberately deferred):**
the retrieval pipeline has no diversity constraint (no MMR / no cap on chunks-per-contract), so
a plural question ("our vendor**s**") can retrieve 4 of 8 final chunks from a single contract.
Agreed approach: measure this with Phase 4's RAGAS evaluation before fixing it, rather than
tuning against a hypothesis.

**Deliverable so far:** zip `procureiq_phase0_to_3.zip`, delivered via `present_files` multiple
times (latest version has all 7 bug fixes above applied). The user has this unzipped locally at
`C:\Projects\Procure-IQ` and has been running commands from it directly, reporting real
terminal output back into the chat for Claude to debug.

## 4. Exact Next Action

**Phase 4 — Baseline Evaluation**, per `docs/ARCHITECTURE.md` and `README.md`'s "What's next"
section:

1. Build a **gold question set** — semi-automatic, using CUAD's own 41 clause-category labels
   (already extracted into `data/raw/cuad/clauses.jsonl` in Phase 1) as a near-automatic source
   of ground truth: for known contract/clause pairs, generate questions like "what is the
   liability cap in vendor X's contract?" with known correct source chunks.
2. Score the Phase 3 **baseline RAG endpoint** (`retrieval/baseline_rag.py`) against this gold
   set using **RAGAS and/or DeepEval** — specifically context precision, context recall,
   faithfulness, and answer relevancy, per the success criteria already defined in
   `docs/PRD.md`.
3. This baseline score becomes the number every later, more complex (agentic, Phase 5+) version
   of the pipeline must beat to justify its added cost/latency/complexity — and it's also the
   natural place to empirically test the chunk-diversity observation flagged above.

**Before starting**, the assistant in the new session should:
- Ask the user to confirm current phase status (they may have run more commands since this
  summary was written).
- Re-view `docs/PROJECT_OVERVIEW.md` and `README.md` from the zip if the user re-uploads it, to
  get exact current file contents rather than relying solely on this summary.
- Continue the established pattern: build in the sandbox, test everything testable (gold
  question generation logic is pure Python — fully testable in-sandbox; actual RAGAS/DeepEval
  scoring against the baseline requires the user's real Qdrant/Postgres data and a real LLM
  provider, so as with Phases 2-3, expect a "validated in sandbox with fake data" +
  "user validates for real on their machine" split).
