"""
Phase 6 offline tests — no Postgres, no Qdrant server, no embedding/LLM model, no network.

Covers, in order:
  1. SQL validator       - accepts legit SELECTs, rejects every dangerous shape we could think of
  2. extract_sql         - cleans typical LLM output (fences, chatter, trailing semicolons)
  3. router              - rag / sql / both decisions, including the project's own gold questions
  4. clause_query_for    - short contract-search phrase for "both" questions
  5. best_per_contract   - diversification helper
  6. Qdrant contract_ids filter (in-memory Qdrant + FakeEmbedder)
  7. The whole LangGraph pipeline with scripted generators, proving:
        - each route visits exactly the nodes it should
        - "both" runs SQL first and hands its contract_ids to the contract search
        - a bad SQL attempt is repaired on retry using the error message
        - a RAG-side retry does NOT re-run a SQL query that already succeeded
        - persistent SQL failure ends in give_up with an honest message

Run:
    python agent/test_phase6_offline.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "retrieval"))
sys.path.insert(0, str(HERE.parent / "ingestion"))

from graph import run_agent  # noqa: E402
from nodes import best_per_contract  # noqa: E402
from router import clause_query_for, route_question  # noqa: E402
from sql_tool import SQLToolError, extract_sql, validate_sql  # noqa: E402

PASSED = 0

def check(label, condition):
    global PASSED
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    assert condition, label
    PASSED += 1


def rejects(sql):
    try:
        validate_sql(sql)
        return False
    except SQLToolError:
        return True


# ------------------------------------------------------------------------------------------
# 1. validator
# ------------------------------------------------------------------------------------------
def test_validator():
    good = [
        "SELECT vendor_name FROM vendors",
        "SELECT country, COUNT(*) FROM vendors GROUP BY country ORDER BY 2 DESC",
        "WITH s AS (SELECT vendor_id, SUM(amount) AS t FROM purchase_orders "
        "WHERE status <> 'CANCELLED' GROUP BY vendor_id) "
        "SELECT v.vendor_name, c.contract_id, s.t FROM vendors v "
        "JOIN contracts c ON c.vendor_id = v.vendor_id JOIN s ON s.vendor_id = v.vendor_id "
        "WHERE s.t > 1000000 AND c.expiration_date < CURRENT_DATE + INTERVAL '180 days' "
        "ORDER BY s.t DESC LIMIT 50",
        "SELECT * FROM vendor_annual_spend WHERE spend_year = EXTRACT(YEAR FROM CURRENT_DATE)",
        "SELECT vendor_id FROM invoices WHERE paid_date IS NULL;",
        "SELECT vendor_name FROM vendors UNION SELECT vendor_name FROM vendors",
    ]
    for q in good:
        check(f"accepts: {q[:55]}...", not rejects(q))

    bad = {
        "DROP TABLE vendors": "DDL",
        "DELETE FROM vendors": "DELETE",
        "UPDATE vendors SET risk_tier = 'X'": "UPDATE",
        "INSERT INTO vendors(vendor_name) VALUES ('x')": "INSERT",
        "TRUNCATE vendors": "TRUNCATE",
        "CREATE TABLE x(a int)": "CREATE",
        "SELECT 1; DROP TABLE vendors": "stacked statements",
        "WITH x AS (DELETE FROM vendors RETURNING *) SELECT * FROM x": "DML hidden in a CTE",
        "SELECT * INTO newtbl FROM vendors": "SELECT INTO",
        "SELECT * FROM vendors FOR UPDATE": "row locking",
        "COPY vendors TO '/tmp/x'": "COPY",
        "SET ROLE postgres": "SET",
        "SELECT * FROM pg_shadow": "system table",
        "SELECT * FROM pg_catalog.pg_user": "schema-qualified system table",
        "SELECT * FROM information_schema.tables": "information_schema",
        "SELECT * FROM public.vendors": "schema-qualified allowed table",
        "SELECT * FROM contract_chunks": "table outside the allow-list",
        "SELECT * FROM vendors v JOIN cuad_documents d ON true": "join to non-allowed table",
        "SELECT pg_sleep(10)": "pg_sleep",
        "SELECT pg_read_file('/etc/passwd')": "pg_read_file",
        "SELECT query_to_xml('select * from pg_shadow', true, true, '')": "query_to_xml",
        "SELECT current_setting('server_version')": "current_setting",
        "SELECT version()": "version()",
        "SELECT current_user": "current_user",
        "SELECT * FROM generate_series(1, 100000000)": "table-valued function",
        "": "empty string",
        "   ": "blank string",
    }
    for q, why in bad.items():
        check(f"rejects {why}: {q[:45]!r}", rejects(q))

    # What gets executed is the validator's re-rendering, not the raw text: a trailing comment or
    # semicolon must not survive into the executed string.
    out = validate_sql("SELECT vendor_name FROM vendors -- sneaky comment\n;")
    check("normalised output has no comment/semicolon", "--" not in out and ";" not in out)


# ------------------------------------------------------------------------------------------
# 2. extract_sql
# ------------------------------------------------------------------------------------------
def test_extract_sql():
    check("strips ```sql fences", extract_sql("```sql\nSELECT 1 FROM vendors;\n```") == "SELECT 1 FROM vendors")
    check(
        "strips chatty preamble",
        extract_sql("Sure! Here is the query:\nSELECT a FROM vendors;").startswith("SELECT a FROM vendors"),
    )
    check("handles bare SQL", extract_sql("  select a from vendors ;  ") == "select a from vendors")
    check("keeps WITH queries intact", extract_sql("WITH a AS (SELECT 1) SELECT * FROM a").startswith("WITH a"))


# ------------------------------------------------------------------------------------------
# 3 & 4. router + clause query
# ------------------------------------------------------------------------------------------
def test_router():
    cases = [
        # contract text only
        ("What is the liability cap in ACME Corp's contract?", "rag"),
        ("What happens if a vendor terminates for convenience?", "rag"),
        ("Does the agreement include an exclusivity clause?", "rag"),
        # structured data only
        ("Who are the top 5 vendors by total spend?", "sql"),
        ("How many invoices are overdue?", "sql"),
        ("Which vendors are in India?", "sql"),  # 'countr' signal absent -> falls to default? see below
        ("Which vendor contracts expire in the next 90 days?", "sql"),
        # multi-hop (the project's own gold-question shapes)
        ("Which vendors with total purchase-order spend above $1,537,622 have contracts that include "
         "an uncapped liability clause?", "both"),
        ("Which vendors have contracts that are set to auto-renew AND include a termination-for-convenience "
         "clause?", "both"),
        ("Which vendors with total purchase-order spend above $1,130,017 have contracts that include "
         "an exclusivity clause?", "both"),
        ("Which vendors have contracts expiring within 180 days of 2026-09-22 that also include an "
         "uncapped liability clause?", "both"),
        ("Which vendors with total purchase-order spend above $1,537,622 have contracts that include "
         "a non-compete restriction?", "both"),
    ]
    for question, expected in cases:
        got, reason = route_question(question)
        if question == "Which vendors are in India?":
            # No keyword signal here ("India" alone isn't a signal) -> safe default is rag.
            check(f"vague question defaults to rag: {question!r}", got == "rag")
            continue
        check(f"{expected:>4} <- {question[:70]!r}", got == expected)

    check("no-signal question defaults to rag", route_question("tell me something useful")[0] == "rag")

    class ScriptedClassifier:
        def __init__(self, reply):
            self.reply = reply

        def generate(self, prompt):
            return self.reply

    check("LLM tiebreaker used when no signals", route_question("vague thing", ScriptedClassifier("sql"))[0] == "sql")
    check("LLM tiebreaker tolerates chatter", route_question("vague thing", ScriptedClassifier("I'd say: BOTH."))[0] == "both")
    check("garbage LLM reply falls back to rag", route_question("vague thing", ScriptedClassifier("???"))[0] == "rag")

    class Exploding:
        def generate(self, prompt):
            raise RuntimeError("API down")

    check("LLM failure never crashes the router", route_question("vague thing", Exploding())[0] == "rag")


def test_clause_query():
    q1 = "Which vendors with total spend above $1M have contracts that include an uncapped liability clause?"
    check("uncapped subsumes liability cap", clause_query_for(q1) == "uncapped liability")
    q2 = "Which vendors that auto-renew include a termination-for-convenience clause?"
    check("termination topic extracted", clause_query_for(q2) == "termination for convenience")
    check("exclusivity topic extracted", clause_query_for("vendors above $1M with exclusivity") == "exclusivity")
    check("returns None when no clause topic", clause_query_for("top 5 vendors by spend") is None)


# ------------------------------------------------------------------------------------------
# 5. best_per_contract
# ------------------------------------------------------------------------------------------
def test_best_per_contract():
    chunks = [{"contract_id": c, "chunk_id": f"{c}::{i}"} for i, c in enumerate("AABACBD")]
    out = best_per_contract(chunks, 10)
    check("one chunk per contract, best-first order kept", [c["chunk_id"] for c in out] == ["A::0", "B::2", "C::4", "D::6"])
    check("respects max_n", len(best_per_contract(chunks, 2)) == 2)


# ------------------------------------------------------------------------------------------
# 6. contract_ids filter in the dense retriever (in-memory Qdrant)
# ------------------------------------------------------------------------------------------
def test_dense_filter():
    from dense_retriever import COLLECTION, dense_search
    from embedder import FakeEmbedder
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    emb = FakeEmbedder(dim=32)
    client = QdrantClient(":memory:")
    client.create_collection(COLLECTION, vectors_config=VectorParams(size=32, distance=Distance.COSINE))
    texts = [(f"c{i % 3}", f"chunk text number {i}") for i in range(12)]
    client.upsert(
        COLLECTION,
        points=[
            PointStruct(
                id=i,
                vector=emb.embed([t])[0],
                payload={"chunk_id": f"{cid}::{i}", "contract_id": cid, "categories": ["GENERAL"], "text": t},
            )
            for i, (cid, t) in enumerate(texts)
        ],
    )
    unfiltered = dense_search("anything", emb, client, top_k=12)
    check("unfiltered search sees all 3 contracts", {r["contract_id"] for r in unfiltered} == {"c0", "c1", "c2"})
    filtered = dense_search("anything", emb, client, top_k=12, contract_ids=["c1"])
    check("filtered search returns only c1", {r["contract_id"] for r in filtered} == {"c1"} and len(filtered) == 4)
    two = dense_search("anything", emb, client, top_k=12, contract_ids=["c0", "c2"])
    check("filter accepts multiple ids", {r["contract_id"] for r in two} == {"c0", "c2"})


# ------------------------------------------------------------------------------------------
# 7. full graph with scripted components
# ------------------------------------------------------------------------------------------
VALID_SQL_BOTH = (
    "SELECT v.vendor_name, c.contract_id, s.t AS total_spend FROM vendors v "
    "JOIN contracts c ON c.vendor_id = v.vendor_id "
    "JOIN (SELECT vendor_id, SUM(amount) AS t FROM purchase_orders GROUP BY vendor_id) s "
    "ON s.vendor_id = v.vendor_id"
)
ROWS_BOTH = {
    "columns": ["vendor_name", "contract_id", "total_spend"],
    "rows": [
        {"vendor_name": "Acme", "contract_id": "0001_ACME_AGREEMENT", "total_spend": 2.0e6},
        {"vendor_name": "Globex", "contract_id": "0002_GLOBEX_AGREEMENT", "total_spend": 1.6e6},
    ],
    "truncated": False,
}


class ScriptedGenerator:
    """
    Stands in for a real LLM. Dispatches on which prompt it was handed:
      - SQL prompts  -> pops the next scripted SQL string
      - synthesis    -> cites whichever contract ids appear in the excerpts (optionally a
                        fabricated one first, to exercise the critic's retry path)
      - rewrite      -> returns a fixed alternative search query
    """

    is_fake = False

    def __init__(self, sql_replies=None, hallucinate_first=False):
        self.sql_replies = list(sql_replies or [])
        self.hallucinate_first = hallucinate_first
        self.sql_prompts: list[str] = []
        self.synth_calls = 0

    def generate(self, prompt):
        if "careful PostgreSQL analyst" in prompt:
            self.sql_prompts.append(prompt)
            return self.sql_replies.pop(0)
        if "A previous attempt to answer" in prompt:
            return "rewritten search query"
        # synthesis (either baseline or Phase 6 prompt)
        self.synth_calls += 1
        block = re.search(r"--- CONTRACT EXCERPTS ---(.*)--- END EXCERPTS ---", prompt, re.DOTALL)
        ids = list(dict.fromkeys(re.findall(r"\[(\d{4}_\w+)\]", block.group(1) if block else "")))
        if self.hallucinate_first and self.synth_calls == 1:
            return "Per [9999_FABRICATED_CONTRACT] the cap is unlimited."
        if ids:
            return "Findings: " + " ".join(f"[{i}] has the clause." for i in ids)
        return "Per the database, the top vendor is Initech."


class Recorder:
    """Records every retrieval call so tests can assert what the RAG node was asked to do."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.calls: list[dict] = []

    def __call__(self, query, client, embedder, reranker, **kw):
        self.calls.append({"query": query, **kw})
        ids = kw.get("contract_ids")
        return [c for c in self.chunks if not ids or c["contract_id"] in ids]


def _chunks(*contract_ids):
    return [
        {"chunk_id": f"{c}::1", "contract_id": c, "categories": ["Uncapped Liability"], "text": "clause text", "rrf_score": 0.1, "source_lists": 1}
        for c in contract_ids
    ]


def _executor_counting(result):
    calls = []

    def ex(sql):
        calls.append(sql)
        return result

    ex.calls = calls
    return ex


def test_graph_rag_route():
    rec = Recorder(_chunks("0001_ACME_AGREEMENT"))
    ex = _executor_counting(ROWS_BOTH)
    gen = ScriptedGenerator()
    out = run_agent("What is the liability cap in Acme's contract?", None, None, None, gen, sql_executor=ex, retrieve_fn=rec)
    check("rag route: SQL never ran", ex.calls == [] and out["sql_query"] == "")
    check("rag route: retrieval ran, unfiltered", len(rec.calls) == 1 and "contract_ids" not in rec.calls[0])
    check("rag route: accepted with 0 retries", out["route"] == "rag" and out["verdict"] == "accept" and out["retry_count"] == 0)


def test_graph_sql_route():
    rec = Recorder(_chunks("0001_ACME_AGREEMENT"))
    ex = _executor_counting(ROWS_BOTH)
    gen = ScriptedGenerator(sql_replies=["SELECT vendor_name FROM vendors ORDER BY 1"])
    out = run_agent("Who are the top 5 vendors by total spend?", None, None, None, gen, sql_executor=ex, retrieve_fn=rec)
    check("sql route: RAG never ran", rec.calls == [] and out["retrieved_chunks"] == [])
    check("sql route: executed the validated SQL", len(ex.calls) == 1 and ex.calls[0].startswith("SELECT"))
    check("sql route: accepted", out["route"] == "sql" and out["verdict"] == "accept" and "Initech" in out["answer"])


def test_graph_both_route():
    rec = Recorder(_chunks("0001_ACME_AGREEMENT", "0002_GLOBEX_AGREEMENT", "0777_NOT_IN_SQL_RESULT"))
    ex = _executor_counting(ROWS_BOTH)
    gen = ScriptedGenerator(sql_replies=[VALID_SQL_BOTH])
    q = "Which vendors with total purchase-order spend above $1,000,000 have contracts that include an uncapped liability clause?"
    out = run_agent(q, None, None, None, gen, sql_executor=ex, retrieve_fn=rec, category_fn=False)

    check("both route chosen", out["route"] == "both")
    check("SQL prompt demanded contract_id column", "contracts.contract_id AS contract_id" in gen.sql_prompts[0])
    sql_i = next(i for i, t in enumerate(out["trace"]) if t.startswith("sql_tool"))
    rag_i = next(i for i, t in enumerate(out["trace"]) if t.startswith("rag_tool"))
    check("SQL ran BEFORE RAG", sql_i < rag_i)
    check(
        "RAG got exactly the SQL-found contract_ids as its filter",
        rec.calls[0].get("contract_ids") == ["0001_ACME_AGREEMENT", "0002_GLOBEX_AGREEMENT"],
    )
    check("RAG query narrowed to the clause topic (spend words dropped)", rec.calls[0]["query"] == "uncapped liability")
    check("contract NOT in SQL result never reached the answer", "0777" not in out["answer"])
    check("both route accepted, citations parsed", out["verdict"] == "accept" and len(out["citations"]) == 2)


def test_graph_both_route_empty_sql():
    rec = Recorder(_chunks("0001_ACME_AGREEMENT"))
    ex = _executor_counting({"columns": ["vendor_name", "contract_id", "total_spend"], "rows": [], "truncated": False})
    gen = ScriptedGenerator(sql_replies=[VALID_SQL_BOTH])
    out = run_agent(
        "Which vendors with spend above $999,000,000 have an exclusivity clause?", None, None, None, gen,
        sql_executor=ex, retrieve_fn=rec,
    )
    check("empty SQL result: contract search skipped", rec.calls == [])
    check("empty SQL result: accepted (legit 'nothing matches'), no pointless retry", out["verdict"] == "accept" and out["retry_count"] == 0)


def test_graph_sql_self_repair():
    ex = _executor_counting(ROWS_BOTH)
    # attempt 1: a destructive statement the validator must stop; attempt 2: valid
    gen = ScriptedGenerator(sql_replies=["DROP TABLE vendors", "SELECT vendor_name FROM vendors"])
    out = run_agent("Who are the top 5 vendors by total spend?", None, None, None, gen, sql_executor=ex, retrieve_fn=Recorder([]))
    check("dangerous SQL never reached the executor", all("DROP" not in s.upper() for s in ex.calls))
    check("repaired on retry", out["verdict"] == "accept" and out["retry_count"] == 1)
    check("retry prompt contained the previous SQL + error", "Your previous attempt failed" in gen.sql_prompts[1] and "DROP TABLE vendors" in gen.sql_prompts[1])


def test_graph_sql_persistent_failure():
    gen = ScriptedGenerator(sql_replies=["DROP TABLE vendors"] * 3)
    out = run_agent("Who are the top 5 vendors by total spend?", None, None, None, gen, sql_executor=_executor_counting(ROWS_BOTH), retrieve_fn=Recorder([]), max_retries=2)
    check("persistent SQL failure -> give_up", out["verdict"] == "give_up" and out["retry_count"] == 2)
    check("give_up answer is honest, not empty/invented", "couldn't retrieve" in out["answer"])


def test_graph_rag_retry_reuses_sql():
    rec = Recorder(_chunks("0001_ACME_AGREEMENT", "0002_GLOBEX_AGREEMENT"))
    ex = _executor_counting(ROWS_BOTH)
    gen = ScriptedGenerator(sql_replies=[VALID_SQL_BOTH], hallucinate_first=True)
    q = "Which vendors with spend above $1,000,000 have an uncapped liability clause?"
    out = run_agent(q, None, None, None, gen, sql_executor=ex, retrieve_fn=rec, category_fn=False)
    check("hallucinated citation triggered exactly one retry", out["retry_count"] == 1 and out["verdict"] == "accept")
    check("SQL ran only once despite the retry", len(ex.calls) == 1 and len(gen.sql_prompts) == 1)
    check("route stayed 'both' across the retry", out["route"] == "both" and any("keeping route" in t for t in out["trace"]))
    check("planner rewrote the search query for the RAG-side failure", rec.calls[1]["query"] == "rewritten search query")


def test_graph_fake_provider_plumbing():
    """--provider fake path: FakeGenerator gets the canned SQL, so offline runs still exercise SQL."""
    sys.path.insert(0, str(HERE.parent / "retrieval"))
    from generator import FakeGenerator

    ex = _executor_counting(ROWS_BOTH)
    out = run_agent(
        "Which vendors with spend above $1,000,000 have an exclusivity clause?", None, None, None,
        FakeGenerator(), sql_executor=ex, retrieve_fn=Recorder(_chunks("0001_ACME_AGREEMENT", "0002_GLOBEX_AGREEMENT")), category_fn=False,
    )
    check("fake provider: canned SQL validated + executed", len(ex.calls) == 1 and out["sql_error"] == "")
    check("fake provider: pipeline completes", out["verdict"] in ("accept", "give_up") and out["route"] == "both")


# ------------------------------------------------------------------------------------------
# 8. exact clause-category lookup on the "both" route + SQL prompt fixes
# ------------------------------------------------------------------------------------------
def test_clause_category_mapping():
    from router import clause_category_for

    check("uncapped -> 'Uncapped Liability' (not 'Cap On Liability')", clause_category_for("spend above $1M with an uncapped liability clause") == "Uncapped Liability")
    check("plain liability cap -> 'Cap On Liability'", clause_category_for("vendors with a liability cap") == "Cap On Liability")
    check("termination -> 'Termination For Convenience'", clause_category_for("auto-renew AND termination-for-convenience clause") == "Termination For Convenience")
    check("non-compete -> 'Non-Compete'", clause_category_for("spend above $1M with a non-compete restriction") == "Non-Compete")
    check("no topic -> None", clause_category_for("top 5 vendors by spend") is None)


def test_sql_prompt_fixes():
    from prompts import build_sql_prompt

    both = build_sql_prompt("q", "2026-10-04", want_contract_ids=True)
    check("both-route prompt keeps filter measures (e.g. total_spend) in SELECT", "every measure the question filters on" in both)
    check("both-route prompt forbids a small LIMIT", "do NOT add a LIMIT smaller than 200" in both)
    check("prompt tells model to honour an explicit date in the question", "use THAT date as a literal" in both)
    plain = build_sql_prompt("q", "2026-10-04", want_contract_ids=False)
    check("plain prompt keeps LIMIT 50 guidance", "LIMIT 50" in plain and "smaller than 200" not in plain)


def test_graph_both_exact_lookup():
    sql_rows = {
        "columns": ["vendor_name", "contract_id", "total_spend"],
        "rows": [
            {"vendor_name": "Acme", "contract_id": "0001_ACME_AGREEMENT", "total_spend": 2.0e6},
            {"vendor_name": "Globex", "contract_id": "0002_GLOBEX_AGREEMENT", "total_spend": 1.6e6},
            {"vendor_name": "Initech", "contract_id": "0003_INITECH_AGREEMENT", "total_spend": 1.2e6},
        ],
        "truncated": False,
    }
    seen = {}

    def category_fn(category, ids):
        seen["category"], seen["ids"] = category, list(ids)
        # only Acme and Initech actually have the labelled clause
        return [
            {"chunk_id": f"{c}::7", "contract_id": c, "categories": [category], "text": "x" * 900}
            for c in ids if c in ("0001_ACME_AGREEMENT", "0003_INITECH_AGREEMENT")
        ]

    rec = Recorder([])
    gen = ScriptedGenerator(sql_replies=[VALID_SQL_BOTH])
    q = "Which vendors with total purchase-order spend above $1,000,000 have contracts that include an uncapped liability clause?"
    out = run_agent(q, None, None, None, gen, sql_executor=_executor_counting(sql_rows), retrieve_fn=rec, category_fn=category_fn)

    check("exact lookup got the category + ALL SQL candidate ids", seen["category"] == "Uncapped Liability" and len(seen["ids"]) == 3)
    check("semantic search NOT used when exact lookup succeeds", rec.calls == [])
    check("only the 2 matching contracts reach the answer", {c["contract_id"] for c in out["retrieved_chunks"]} == {"0001_ACME_AGREEMENT", "0003_INITECH_AGREEMENT"})
    check("excerpt text capped for prompt size", all(len(c["text"]) <= 600 for c in out["retrieved_chunks"]))
    check("clause note is deterministic and carries SQL figures", "2 of the 3" in out["clause_note"] and "total_spend=2000000.0" in out["clause_note"] and "Globex" not in out["clause_note"])
    check("accepted without retry", out["verdict"] == "accept" and out["retry_count"] == 0)


def test_graph_both_exact_lookup_none_match():
    sql_rows = {"columns": ["vendor_name", "contract_id"], "rows": [{"vendor_name": "Acme", "contract_id": "0001_ACME_AGREEMENT"}], "truncated": False}
    gen = ScriptedGenerator(sql_replies=[VALID_SQL_BOTH])
    out = run_agent(
        "Which vendors with spend above $1,000,000 have an exclusivity clause?", None, None, None, gen,
        sql_executor=_executor_counting(sql_rows), retrieve_fn=Recorder([]), category_fn=lambda cat, ids: [],
    )
    check("zero matches is a complete answer: accepted, no pointless retry", out["verdict"] == "accept" and out["retry_count"] == 0)
    check("clause note states none matched", "NONE" in out["clause_note"])


def test_graph_category_lookup_failure_falls_back():
    def boom(category, ids):
        raise RuntimeError("db down")

    sql_rows = {"columns": ["vendor_name", "contract_id"], "rows": [{"vendor_name": "Acme", "contract_id": "0001_ACME_AGREEMENT"}], "truncated": False}
    rec = Recorder(_chunks("0001_ACME_AGREEMENT"))
    out = run_agent(
        "Which vendors with spend above $1,000,000 have an exclusivity clause?", None, None, None,
        ScriptedGenerator(sql_replies=[VALID_SQL_BOTH]), sql_executor=_executor_counting(sql_rows), retrieve_fn=rec, category_fn=boom,
    )
    check("lookup failure degrades to semantic search", len(rec.calls) == 1 and out["verdict"] == "accept")
    check("failure is visible in the trace", any("lookup failed" in t for t in out["trace"]))


def test_append_missing_matches():
    from nodes import append_missing_matches

    note = "2 of 3 ...:\n- [0001_ACME_AGREEMENT] vendor_name=Acme, total_spend=1\n- [0003_INITECH_AGREEMENT] vendor_name=Smith, Vaughan and Miller, total_spend=2"
    rows = [
        {"contract_id": "0001_ACME_AGREEMENT", "vendor_name": "Acme"},
        {"contract_id": "0003_INITECH_AGREEMENT", "vendor_name": "Smith, Vaughan and Miller"},
    ]
    full = "Acme and Smith, Vaughan and Miller both match."
    check("complete answer is left untouched", append_missing_matches(full, note, rows) == full)
    part = append_missing_matches("Acme matches.", note, rows)
    check("omitted vendor appended, named vendor not duplicated", "Smith, Vaughan and Miller [0003_INITECH_AGREEMENT]" in part and part.count("Acme") == 1)
    none = append_missing_matches("I could not determine this.", note, rows)
    check("answer naming nobody gets every match appended", "Acme [0001_ACME_AGREEMENT]" in none and "Smith, Vaughan and Miller [0003" in none)
    cited = append_missing_matches("The match is [0001_ACME_AGREEMENT] (vendor Akme).", note, rows)
    check("citing the contract id does NOT count as naming the vendor", "- Acme [0001_ACME_AGREEMENT]" in cited.split("not listed above:")[-1])
    check("lookalike-hyphen name is re-appended in exact form", "- Smith-Myers [0001" in append_missing_matches(
    "Smith\u2011Myers matches.", "1 of 1:\n- [0001_X] vendor_name=Smith-Myers", [{"contract_id": "0001_X", "vendor_name": "Smith-Myers"}]))
    check("empty answer is repaired", "Acme [0001" in append_missing_matches("", note, rows))


if __name__ == "__main__":
    test_validator()
    test_extract_sql()
    test_router()
    test_clause_query()
    test_best_per_contract()
    test_dense_filter()
    test_graph_rag_route()
    test_graph_sql_route()
    test_graph_both_route()
    test_graph_both_route_empty_sql()
    test_graph_sql_self_repair()
    test_graph_sql_persistent_failure()
    test_graph_rag_retry_reuses_sql()
    test_graph_fake_provider_plumbing()
    test_clause_category_mapping()
    test_sql_prompt_fixes()
    test_graph_both_exact_lookup()
    test_graph_both_exact_lookup_none_match()
    test_graph_category_lookup_failure_falls_back()
    test_append_missing_matches()
    print(f"\nAll {PASSED} checks passed.")
