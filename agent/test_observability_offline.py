"""
Phase 11 offline tests — no Langfuse server, no network, no database, no LLM.

These use the REAL Langfuse SDK; only its exporter is swapped for an in-memory one, so what is
asserted here is exactly what would be sent to Langfuse: span tree, types, names, inputs/outputs,
error levels, redaction, and scores.

Run:   python agent/test_observability_offline.py
"""
import logging
import os
import sys
import threading
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (ROOT, HERE, ROOT / "retrieval", ROOT / "ingestion", ROOT / "api"):
    sys.path.insert(0, str(p))

for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_TRACING_ENABLED", "LANGFUSE_CAPTURE_CONTENT", "LANGFUSE_HOST"):
    os.environ.pop(k, None)
# Hermetic: init_tracing() also reads your real .env (which may hold real Langfuse keys). Tests must not
# depend on, or send anything using, your actual configuration -- so .env loading is disabled here.
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False
# The SDK tries to ship *scores* over HTTP to a host that does not exist here; that error is expected noise.
logging.getLogger("langfuse").setLevel(logging.CRITICAL)

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

import observability as obs  # noqa: E402
from graph import build_agent_graph, run_agent  # noqa: E402
from test_api_offline import Q_SQL, make_client  # noqa: E402
from test_phase6_offline import ROWS_BOTH, Recorder, ScriptedGenerator, _executor_counting  # noqa: E402

N = 0
_pk = 0


def check(label, cond):
    global N
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label
    N += 1


def start(**env):
    """Fresh tracing session with its own in-memory exporter (unique key => fresh SDK instance)."""
    global _pk
    _pk += 1
    obs.shutdown_tracing()
    for k in ("LANGFUSE_TRACING_ENABLED", "LANGFUSE_CAPTURE_CONTENT"):
        os.environ.pop(k, None)
    os.environ.update(env)
    exp = InMemorySpanExporter()
    ok = obs.init_tracing(public_key=f"pk-test-{_pk}", secret_key="sk-test", host="http://127.0.0.1:1", span_exporter=exp)
    assert ok, "tracing failed to start"
    return exp


def spans_of(exp):
    obs.flush()
    return exp.get_finished_spans()


def attr(s, key):
    return (s.attributes or {}).get(key)


def sid(s):
    return s.context.span_id


def run(question="Who are the top 5 vendors by total spend?", sql=("SELECT vendor_name FROM vendors ORDER BY 1",), executor=None):
    return run_agent(
        question, None, None, None, ScriptedGenerator(sql_replies=list(sql)),
        sql_executor=executor or _executor_counting(ROWS_BOTH), retrieve_fn=Recorder([]), category_fn=False,
    )


# ---------------------------------------------------------------- 1. off by default
check("no keys -> tracing off", obs.init_tracing() is False and not obs.enabled())
out_off = run()
check("agent works untraced (no-op path)", out_off["verdict"] == "accept")
os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"], os.environ["LANGFUSE_TRACING_ENABLED"] = "pk", "sk", "false"
check("LANGFUSE_TRACING_ENABLED=false wins over present keys", obs.init_tracing() is False)
for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_TRACING_ENABLED"):
    os.environ.pop(k, None)

# ---------------------------------------------------------------- 2. one traced run: tree, types, I/O
exp = start()
out = run()
sp = spans_of(exp)
roots = [s for s in sp if s.parent is None]
check("exactly one root span per question", len(roots) == 1 and roots[0].name == "procureiq.run_agent")
root = roots[0]
check("root is an 'agent' observation carrying the question", attr(root, "langfuse.observation.type") == "agent" and "top 5 vendors" in attr(root, "langfuse.observation.input"))
check("all spans share the root's trace id", len({s.context.trace_id for s in sp}) == 1)
names = Counter(s.name for s in sp)
print(f"   (diagnostic) span counts={dict(names)}  verdict={out['verdict']}  retries={out['retry_count']}  route={out['route']}")
check("every node that ran has a span", all(names[n] == 1 for n in ("planner", "router", "sql_tool", "synthesizer", "critic", "report")))
by_name = {s.name: s for s in sp if s.name != "llm.generate"}
check("node spans hang directly under the root", all(by_name[n].parent.span_id == sid(root) for n in ("planner", "router", "sql_tool", "synthesizer", "critic", "report")))
check("node types: tool / guardrail / chain", (attr(by_name["sql_tool"], "langfuse.observation.type"), attr(by_name["critic"], "langfuse.observation.type"), attr(by_name["router"], "langfuse.observation.type")) == ("tool", "guardrail", "chain"))
gens = [s for s in sp if s.name == "llm.generate"]
check("2 LLM calls recorded as 'generation' (text-to-SQL + synthesis)", len(gens) == 2 and all(attr(g, "langfuse.observation.type") == "generation" for g in gens))
check("each generation nests under the node that made the call", {g.parent.span_id for g in gens} == {sid(by_name["sql_tool"]), sid(by_name["synthesizer"])})
check("generation records prompt, reply and model", any("PostgreSQL" in attr(g, "langfuse.observation.input") for g in gens) and any(attr(g, "langfuse.observation.output") == "SELECT vendor_name FROM vendors ORDER BY 1" for g in gens) and all(attr(g, "langfuse.observation.model.name") == "ScriptedGenerator" for g in gens))
check("trace name and tag set on every span", all(attr(s, "langfuse.trace.name") == "procureiq.run_agent" and "cli" in attr(s, "langfuse.trace.tags") for s in sp))
check("sql_tool span output shows what ran", "SELECT vendor_name" in attr(by_name["sql_tool"], "langfuse.observation.output"))
check("root output carries route + verdict", '"route": "sql"' in attr(root, "langfuse.observation.output") and '"verdict": "accept"' in attr(root, "langfuse.observation.output"))
check("tracing did not change the agent's result", out["verdict"] == out_off["verdict"] and out["route"] == out_off["route"] and out["sql_rows"] == out_off["sql_rows"])

# ---------------------------------------------------------------- 3. retries show up as repeated nodes
exp = start()
out = run(sql=("DROP TABLE vendors", "SELECT vendor_name FROM vendors ORDER BY 1"))
sp = spans_of(exp)
names = Counter(s.name for s in sp)
check("a retried run is visible: planner x2, critic x2", out["retry_count"] == 1 and names["planner"] == 2 and names["critic"] == 2)
critics = sorted((s for s in sp if s.name == "critic"), key=lambda s: s.start_time)
check("first critic span says retry, last says accept", '"verdict": "retry"' in attr(critics[0], "langfuse.observation.output") and '"verdict": "accept"' in attr(critics[-1], "langfuse.observation.output"))

# ---------------------------------------------------------------- 4. errors: span marked ERROR, exception still propagates
exp = start()


def boom(state):
    raise RuntimeError("synthetic failure")


raised = False
try:
    with obs.trace_request("t", "q"):
        obs.traced_node("sql_tool", boom)({"question": "q"})
except RuntimeError:
    raised = True
sp = spans_of(exp)
bad = [s for s in sp if s.name == "sql_tool"][0]
check("exception propagates unchanged through the tracing layer", raised)
check("failing span is marked ERROR with the message", attr(bad, "langfuse.observation.level") == "ERROR" and "synthetic failure" in str(attr(bad, "langfuse.observation.status_message")))
check("root span still closed and exported", any(s.parent is None for s in sp))

# ---------------------------------------------------------------- 5. privacy switch
exp = start(LANGFUSE_CAPTURE_CONTENT="false")
secret_q = "How much total spend did we have with ACME-SECRET-PROJECT vendors?"
out = run(question=secret_q, sql=("SELECT vendor_name FROM vendors ORDER BY 1",))
sp = spans_of(exp)
blob = " ".join(str(v) for s in sp for v in (s.attributes or {}).values())
check("capture off: the question text never appears in any span", "ACME-SECRET-PROJECT" not in blob)
check("capture off: SQL text and LLM prompts never appear", "SELECT vendor_name" not in blob and "PostgreSQL analyst" not in blob and "Initech" not in blob)
check("capture off: structure survives as '<N chars>' markers", "chars>" in blob)
check("capture off: span tree is still complete", {s.name for s in sp} >= {"procureiq.run_agent", "planner", "router", "sql_tool", "synthesizer", "critic", "report", "llm.generate"})

# ---------------------------------------------------------------- 6. scores
exp = start()
scores = []
real = obs._client.score_current_trace
obs._client.score_current_trace = lambda **kw: (scores.append(kw), real(**kw))[1]
run(sql=("DROP TABLE vendors", "SELECT vendor_name FROM vendors ORDER BY 1"))
spans_of(exp)
got = {s["name"]: s["value"] for s in scores}
check("verdict / confidence / retries scored on the trace", set(got) == {"verdict", "confidence", "retries"})
check("score values are right (accept, retries=1)", got["verdict"] == "accept" and got["retries"] == 1)
check("categorical scores are strings, retries numeric", isinstance(got["verdict"], str) and isinstance(got["retries"], int))

# ---------------------------------------------------------------- 7. tracing failures never reach the request
class BrokenClient:
    def start_as_current_observation(self, **kw):
        raise ConnectionError("langfuse is down")

    def score_current_trace(self, **kw):
        raise ConnectionError("langfuse is down")

    def flush(self):
        raise ConnectionError("langfuse is down")


obs.shutdown_tracing()
obs._client = BrokenClient()
out_broken = run()
check("broken Langfuse client: request still succeeds with identical result", out_broken["verdict"] == out_off["verdict"] and out_broken["sql_rows"] == out_off["sql_rows"])
obs.flush()
check("broken Langfuse client: flush() swallows the error", True)
obs._client = None

# ---------------------------------------------------------------- 8. concurrency: traces never mix
exp = start()
results, errors = [], []


def worker(i):
    try:
        results.append(run(question=f"Who are the top {i + 3} vendors by total spend?"))
    except Exception as e:  # noqa: BLE001
        errors.append(e)


threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
[t.start() for t in threads]
[t.join(60) for t in threads]
sp = spans_of(exp)
roots = [s for s in sp if s.parent is None]
check("6 concurrent runs -> 6 roots, 6 trace ids, no errors", len(roots) == 6 and len({s.context.trace_id for s in sp}) == 6 and not errors)
trace_of = {sid(s): s.context.trace_id for s in sp}
check("no span has a parent from another trace", all(s.parent is None or trace_of.get(s.parent.span_id, s.context.trace_id) == s.context.trace_id for s in sp))
per_trace = Counter(s.context.trace_id for s in sp)
check("each trace is complete (>= 8 spans: root + 6 nodes + generations)", all(c >= 9 for c in per_trace.values()))
check("each root carries its own question", len({attr(r, "langfuse.observation.input") for r in roots}) == 6)

# ---------------------------------------------------------------- 9. the API
exp = start()
client, _gen, _app = make_client()
with client:
    r1 = client.post("/ask", json={"question": Q_SQL})
    r2 = client.post("/ask", json={"question": Q_SQL})  # cache hit
sp = spans_of(exp)
rid1 = r1.json()["request_id"]
# FastAPI wraps each request in its own (unexported) OTel span, so our span has a parent that is never
# shipped. Langfuse marks our span as the app root -- that flag, not parent-is-None, defines the root here.
api_roots = [s for s in sp if s.name == "procureiq.ask" and attr(s, "langfuse.internal.is_app_root")]
check("/ask: fresh answer then cache hit -> 2 traces", r1.status_code == 200 and r2.json()["cached"] is True and len(api_roots) == 2)
fresh = [s for s in api_roots if "cache_hit" not in attr(s, "langfuse.trace.tags")]
hit = [s for s in api_roots if "cache_hit" in attr(s, "langfuse.trace.tags")]
check("fresh /ask trace holds the full node tree", len(fresh) == 1 and {s.name for s in sp if s.context.trace_id == fresh[0].context.trace_id} >= {"planner", "router", "sql_tool", "synthesizer", "critic", "report"})
check("request_id is in the trace metadata (correlates with server logs)", attr(fresh[0], "langfuse.trace.metadata.request_id") == rid1)
check("cache hit is traced too (tagged, no node spans)", len(hit) == 1 and sum(1 for s in sp if s.context.trace_id == hit[0].context.trace_id) == 1)
check("API response unchanged by tracing", r1.json()["report"]["verdict"] == "accept")

obs.shutdown_tracing()
print(f"\nAll {N} checks passed.")
