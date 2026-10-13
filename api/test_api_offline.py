"""
Phase 8 offline API tests — no Postgres, no Qdrant, no models, no LLM, no network.

The REAL compiled agent graph runs behind the REAL FastAPI app; only its dependencies (LLM, SQL
executor, retrieval) are scripted stand-ins. So these tests cover HTTP validation, auth, caching,
concurrency limits, streaming and error handling together with the actual pipeline and RiskReport.

Run:  python api/test_api_offline.py        (needs: pip install fastapi httpx)

Isolation: every dependency of the agent is injected, INCLUDING the exact clause-category lookup
(make_client(category_fn=False) disables it). Results must not depend on what is in your database.
"""
import json
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "agent"))
sys.path.insert(0, str(HERE.parent / "retrieval"))
sys.path.insert(0, str(HERE.parent / "ingestion"))

from fastapi.testclient import TestClient  # noqa: E402

from api.app import GraphRunner, create_app  # noqa: E402
from api.cache import TTLCache, make_key  # noqa: E402
from graph import build_agent_graph  # noqa: E402
from schemas import RiskReport  # noqa: E402
from test_phase6_offline import (  # noqa: E402
    ROWS_BOTH, VALID_SQL_BOTH, Recorder, ScriptedGenerator, _chunks, _executor_counting,
)

N = 0
Q_RAG = "What is the liability cap in Acme's contract?"
Q_SQL = "Who are the top 5 vendors by total spend?"


def check(label, cond):
    global N
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label
    N += 1


class CountingGenerator(ScriptedGenerator):
    """Scripted LLM that answers any number of SQL prompts and counts every call."""

    def __init__(self, **kw):
        super().__init__(sql_replies=["SELECT vendor_name FROM vendors"] * 50, **kw)
        self.calls = 0

    def generate(self, prompt):
        self.calls += 1
        return super().generate(prompt)


def make_client(*, api_key=None, cache=None, max_concurrent=2, queue_timeout_s=0.2, generator=None,
                checks=None, chunks=None, category_fn=False):
    # category_fn=False DISABLES the exact clause lookup. It must never default to the real one: that
    # queries whatever Postgres happens to be reachable, making the tests depend on live data.
    gen = generator or CountingGenerator()
    graph = build_agent_graph(
        None, None, None, gen, sql_executor=_executor_counting(ROWS_BOTH),
        retrieve_fn=Recorder(chunks if chunks is not None else _chunks("0001_ACME_AGREEMENT")),
        category_fn=category_fn,
    )
    app = create_app(
        lambda: GraphRunner(graph, readiness_checks=checks if checks is not None else {"postgres": lambda: None}),
        api_key=api_key, cache=cache if cache is not None else TTLCache(),
        max_concurrent=max_concurrent, queue_timeout_s=queue_timeout_s,
    )
    return TestClient(app), gen, app


# ---------------------------------------------------------------------------------------------
def test_cache_unit():
    now = [0.0]
    c = TTLCache(max_entries=2, ttl_s=10, clock=lambda: now[0])
    c.set("a", 1)
    check("cache hit within TTL", c.get("a") == 1)
    now[0] = 11
    check("entry expires after TTL", c.get("a") is None)
    c.set("a", 1); c.set("b", 2); c.get("a"); c.set("c", 3)
    check("LRU evicts the least recently used (b), keeps a and c", c.get("b") is None and c.get("a") == 1 and c.get("c") == 3)
    check("ttl=0 disables caching", (lambda x: (x.set("k", 1), x.get("k"))[1])(TTLCache(ttl_s=0)) is None)
    check("key normalises case and whitespace", make_key("  Top 5   VENDORS? ", 2) == make_key("top 5 vendors?", 2))
    check("key separates different max_retries", make_key("q", 1) != make_key("q", 2))


def test_probes():
    client, _, _ = make_client()
    with client:
        check("/health is ok", client.get("/health").json() == {"status": "ok"})
        r = client.get("/ready")
        check("/ready 200 when all checks pass", r.status_code == 200 and r.json()["checks"] == {"postgres": "ok"})

    def boom():
        raise ConnectionError("db down at 10.0.0.5 password=hunter2")

    client, _, _ = make_client(checks={"postgres": boom, "qdrant": lambda: None})
    with client:
        r = client.get("/ready")
        check("/ready 503 when a dependency is down", r.status_code == 503 and r.json()["status"] == "not_ready")
        check("/ready names the failed dependency without leaking details", r.json()["checks"]["postgres"] == "FAILED: ConnectionError" and "hunter2" not in r.text)
        check("/health still 200 while a dependency is down (liveness != readiness)", client.get("/health").status_code == 200)


def test_ask_basic():
    client, gen, _ = make_client()
    with client:
        r = client.post("/ask", json={"question": Q_RAG})
        body = r.json()
        check("/ask 200 with a re-validatable RiskReport", r.status_code == 200 and RiskReport(**body["report"]).route == "rag")
        check("request id in header matches body", r.headers["X-Request-ID"] == body["request_id"] and len(body["request_id"]) == 12)
        check("first call is a cache MISS", r.headers["X-Cache"] == "MISS" and body["cached"] is False)
        check("trace omitted unless requested", "trace" not in body)
        r2 = client.post("/ask", json={"question": Q_SQL, "include_trace": True})
        check("include_trace returns the node trace", any(t.startswith("router:") for t in r2.json()["trace"]) and r2.json()["report"]["route"] == "sql")
        check("sql route report carries database_row findings", r2.json()["report"]["findings"][0]["basis"] == "database_row")


def test_cache_behaviour():
    client, gen, _ = make_client()
    with client:
        client.post("/ask", json={"question": Q_RAG})
        calls_after_first = gen.calls
        r = client.post("/ask", json={"question": "  what is the LIABILITY cap in acme's contract?  "})
        check("repeat (case/space-insensitive) is a cache HIT", r.headers["X-Cache"] == "HIT" and r.json()["cached"] is True and r.json()["latency_s"] == 0.0)
        check("cache hit made ZERO LLM calls", gen.calls == calls_after_first)
        r = client.post("/ask", json={"question": Q_RAG, "refresh": True})
        check("refresh=true bypasses the cache", r.headers["X-Cache"] == "MISS" and gen.calls > calls_after_first)
        r = client.post("/ask", json={"question": Q_RAG, "max_retries": 0})
        check("different max_retries is a separate cache entry", r.headers["X-Cache"] == "MISS")

    # untrustworthy results must never be cached
    cache = TTLCache()
    bad_gen = CountingGenerator()
    bad_gen.sql_replies = ["DROP TABLE vendors"] * 10  # SQL always rejected -> give_up
    client, _, _ = make_client(cache=cache, generator=bad_gen)
    with client:
        r = client.post("/ask", json={"question": Q_SQL, "max_retries": 1})
        check("give_up still returns 200 with a low-confidence report", r.status_code == 200 and r.json()["report"]["verdict"] == "give_up" and r.json()["report"]["confidence"] == "low")
        check("give_up result is NOT cached", len(cache) == 0)


def test_validation():
    client, _, _ = make_client()
    with client:
        check("too-short question -> 422", client.post("/ask", json={"question": "hi"}).status_code == 422)
        check("too-long question -> 422", client.post("/ask", json={"question": "x" * 1001}).status_code == 422)
        check("unknown field -> 422 (extra=forbid)", client.post("/ask", json={"question": Q_RAG, "admin": True}).status_code == 422)
        check("max_retries > 3 -> 422", client.post("/ask", json={"question": Q_RAG, "max_retries": 9}).status_code == 422)
        check("missing body -> 422", client.post("/ask").status_code == 422)
        sneaky = "What is the liab\u200bility cap in Acme's contract?\u202e"
        r = client.post("/ask", json={"question": sneaky, "include_trace": True})
        check("invisible characters stripped from the question before use", r.status_code == 200 and "\u200b" not in r.json()["report"]["question"] and "\u202e" not in r.json()["report"]["question"])


def test_auth():
    client, _, _ = make_client(api_key="s3cret")
    with client:
        check("no key -> 401", client.post("/ask", json={"question": Q_RAG}).status_code == 401)
        check("wrong key -> 401", client.post("/ask", json={"question": Q_RAG}, headers={"X-API-Key": "nope"}).status_code == 401)
        check("correct key -> 200", client.post("/ask", json={"question": Q_RAG}, headers={"X-API-Key": "s3cret"}).status_code == 200)
        check("stream also requires the key", client.post("/ask/stream", json={"question": Q_RAG}).status_code == 401)
        check("/health and /ready stay open", client.get("/health").status_code == 200 and client.get("/ready").status_code == 200)


def test_errors_do_not_leak():
    class ExplodingRunner:
        def run(self, q, r):
            raise RuntimeError("secret internal detail: postgres://admin:pw@10.1.1.1/db")

        def stream(self, q, r):
            raise RuntimeError("secret internal detail")
            yield  # pragma: no cover

        def ready(self):
            return {}

    app = create_app(lambda: ExplodingRunner())
    with TestClient(app) as client:
        r = client.post("/ask", json={"question": Q_RAG})
        check("runner crash -> 500", r.status_code == 500)
        check("500 body carries a request_id, NOT the exception text", "request_id" in r.json()["detail"] and "secret" not in r.text and "postgres://" not in r.text)
        check("semaphore released after a crash (next call is not blocked)", client.post("/ask", json={"question": Q_RAG}).status_code == 500)
        s = client.post("/ask/stream", json={"question": Q_RAG})
        check("stream crash -> an 'error' event, no leak", "event: error" in s.text and "secret" not in s.text)


def test_concurrency_limit():
    client, _, app = make_client(max_concurrent=1, queue_timeout_s=0.05)
    with client:
        app.state.semaphore.acquire()  # simulate a run already in flight
        try:
            r = client.post("/ask", json={"question": Q_RAG})
            check("server busy -> 503 with Retry-After", r.status_code == 503 and r.headers.get("Retry-After") == "10")
            check("busy stream request also gets a clean 503 (before streaming starts)", client.post("/ask/stream", json={"question": Q_RAG}).status_code == 503)
        finally:
            app.state.semaphore.release()
        check("after the slot frees, requests succeed", client.post("/ask", json={"question": Q_RAG}).status_code == 200)
        app.state.semaphore.acquire()
        try:
            r = client.post("/ask", json={"question": Q_RAG})
            check("a CACHED answer is served even when every slot is busy", r.status_code == 200 and r.headers["X-Cache"] == "HIT")
        finally:
            app.state.semaphore.release()


def _parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(l.split(": ", 1) for l in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_streaming():
    client, _, app = make_client()
    with client:
        r = client.post("/ask/stream", json={"question": Q_RAG, "include_trace": True})
        check("stream content-type is text/event-stream", r.headers["content-type"].startswith("text/event-stream"))
        ev = _parse_sse(r.text)
        names = [e for e, _ in ev]
        check("stream order: start, steps..., result", names[0] == "start" and names[-1] == "result" and set(names[1:-1]) == {"step"})
        nodes = [d["node"] for e, d in ev if e == "step"]
        check("steps follow the graph: planner -> router -> rag_tool -> synthesizer -> critic -> report", nodes == ["planner", "router", "rag_tool", "synthesizer", "critic", "report"])
        check("each step reports only NEW trace lines (no repeats)", sum(len(d["lines"]) for e, d in ev if e == "step") == len(ev[-1][1]["trace"]))
        check("final event carries a re-validatable RiskReport", RiskReport(**ev[-1][1]["report"]).verdict == "accept")
        check("semaphore released after the stream finished", app.state.semaphore.acquire(blocking=False) and (app.state.semaphore.release() or True))
        r2 = client.post("/ask/stream", json={"question": Q_RAG})
        ev2 = _parse_sse(r2.text)
        check("repeat stream is served from cache: just start + result", [e for e, _ in ev2] == ["start", "result"] and ev2[0][1]["cached"] is True)


def test_both_route_end_to_end_over_http():
    gen = CountingGenerator()
    gen.sql_replies = [VALID_SQL_BOTH]

    def category_fn(category, ids):  # stub for the exact clause lookup: only Acme carries the clause
        return [{"chunk_id": f"{c}::1", "contract_id": c, "categories": [category], "text": "clause text"}
                for c in ids if c == "0001_ACME_AGREEMENT"]

    client, _, _ = make_client(generator=gen, category_fn=category_fn)
    with client:
        q = "Which vendors with total purchase-order spend above $1,000,000 have contracts that include an uncapped liability clause?"
        r = client.post("/ask", json={"question": q, "include_trace": True})
        rep = r.json()["report"]
        check("both route over HTTP: sql_query recorded in the report", r.status_code == 200 and rep["route"] == "both" and rep["sql_query"].startswith("SELECT"))
        check("both route over HTTP: ONLY the vendor with the clause is a finding (Globex filtered out)",
              [f["vendor_name"] for f in rep["findings"]] == ["Acme"] and rep["findings"][0]["basis"] == "exact_label")
        check("both route over HTTP: finding carries the ERP figure", rep["findings"][0]["figures"]["total_spend"] == 2.0e6)


def test_max_retries_three_does_not_crash():
    """Regression for the LangGraph default recursion limit (25 steps) being hit at max_retries=3."""
    class AlwaysHallucinates(CountingGenerator):
        def generate(self, prompt):
            if "careful PostgreSQL analyst" in prompt or "A previous attempt" in prompt:
                return super().generate(prompt)
            return "Per [9999_FABRICATED] it is unlimited."

    gen = AlwaysHallucinates()
    gen.sql_replies = [VALID_SQL_BOTH]
    client, _, _ = make_client(generator=gen, chunks=_chunks("0001_ACME_AGREEMENT"))
    with client:
        q = "Which vendors with spend above $1,000,000 have an uncapped liability clause?"
        r = client.post("/ask", json={"question": q, "max_retries": 3})
        check("max_retries=3 worst case returns a give_up report, not a 500", r.status_code == 200 and r.json()["report"]["verdict"] in ("give_up", "accept"))


if __name__ == "__main__":
    test_cache_unit()
    test_probes()
    test_ask_basic()
    test_cache_behaviour()
    test_validation()
    test_auth()
    test_errors_do_not_leak()
    test_concurrency_limit()
    test_streaming()
    test_both_route_end_to_end_over_http()
    test_max_retries_three_does_not_crash()
    print(f"\nAll {N} API checks passed.")
