"""
Offline tests for the agent eval harness: scoring math + the run loop (stubbed agent, no
services). Run:  python eval/test_agent_eval_offline.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_metrics import (  # noqa: E402
    aggregate_agent_metrics, expected_route, names_in_text, predicted_contract_ids, set_prf,
)
from run_agent_eval import evaluate_items, score_item  # noqa: E402

N = 0


def check(label, cond):
    global N
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label
    N += 1


# ---- set_prf
check("perfect match", set_prf({"a", "b"}, {"a", "b"}) == {"precision": 1.0, "recall": 1.0, "f1": 1.0, "exact": True})
r = set_prf({"a", "b", "c", "d"}, {"a", "b"})
check("over-prediction: recall 1, precision .5", r["recall"] == 1.0 and r["precision"] == 0.5 and not r["exact"])
r = set_prf({"a"}, {"a", "b", "c", "d"})
check("under-prediction: precision 1, recall .25", r["precision"] == 1.0 and r["recall"] == 0.25)
check("disjoint -> f1 0", set_prf({"x"}, {"a"})["f1"] == 0.0)
check("empty prediction vs non-empty gold -> 0/0/0", set_prf(set(), {"a"}) == {"precision": 0.0, "recall": 0.0, "f1": 0.0, "exact": False})
check("both empty -> perfect", set_prf(set(), set())["exact"] is True)

# ---- names_in_text
check("whole-name, case-insensitive match", names_in_text({"Cole LLC", "Hill Group"}, "cole llc and others") == {"Cole LLC"})
check("name inside a longer word does NOT match", names_in_text({"Cole"}, "Coleman Inc") == set())
check("names with commas/hyphens work", names_in_text({"Smith, Vaughan and Miller", "Garcia-Smith"}, "Garcia-Smith; Smith, Vaughan and Miller.") == {"Smith, Vaughan and Miller", "Garcia-Smith"})

# ---- predicted_contract_ids
note = "2 of 5 contract(s) ...:\n- [0001_ACME_AGREEMENT] vendor_name=Acme\n- [0003_INITECH_AGREEMENT] vendor_name=Init"
st = {"clause_note": note, "retrieved_chunks": [{"contract_id": "0001_ACME_AGREEMENT"}]}
check("clause note beats (capped) retrieved chunks", predicted_contract_ids(st) == {"0001_ACME_AGREEMENT", "0003_INITECH_AGREEMENT"})
check("falls back to retrieved chunks", predicted_contract_ids({"clause_note": "", "retrieved_chunks": [{"contract_id": "0009_X"}]}) == {"0009_X"})
check("expected routes", expected_route("multi_hop") == "both" and expected_route("single_hop") == "rag")

# ---- score_item (multi-hop)
item = {
    "id": "mh_x", "type": "multi_hop", "question": "q", "clause_category": "Uncapped Liability",
    "expected_matches": [
        {"vendor_name": "Acme", "contract_id": "0001_ACME_AGREEMENT"},
        {"vendor_name": "Initech", "contract_id": "0003_INITECH_AGREEMENT"},
    ],
}
state = {
    "route": "both", "verdict": "accept", "retry_count": 1, "sql_query": "SELECT 1", "sql_error": "",
    "sql_rows": [{"vendor_name": "Acme"}, {"vendor_name": "Initech"}, {"vendor_name": "Globex"}],
    "answer": "Acme and Globex have it.", "trace": [], "clause_note": note, "retrieved_chunks": [],
}
row = score_item(item, state, 1.234)
check("tool set perfect (both contracts found)", row["tool"]["f1"] == 1.0)
check("answer set: Acme right, Globex wrong, Initech missed", row["answer_score"]["precision"] == 0.5 and row["answer_score"]["recall"] == 0.5)
check("mention recall matches baseline definition (1 of 2)", row["vendor_mention_recall"] == 0.5)
check("route_ok + latency rounded + extra/missed lists", row["route_ok"] and row["latency_s"] == 1.23 and row["missed_contracts"] == [] and row["extra_contracts"] == [])
check("answer text preserved as a string", isinstance(row["answer"], str))

# ---- score_item (single-hop)
sh = {"id": "sh_1", "type": "single_hop", "question": "q", "clause_category": "Governing Law", "expected_source_chunk_ids": ["c1"]}
sst = dict(state, route="rag", retrieved_chunks=[{"chunk_id": "c1", "contract_id": "x"}], sql_rows=[], sql_query="")
check("single-hop hit scored", score_item(sh, sst, 0.5)["chunk_hit"] is True)

# ---- evaluate_items: error isolation + resume
calls = []


def run_fn(q):
    calls.append(q)
    if q == "boom":
        raise RuntimeError("api down")
    return state


items = [dict(item, id="mh_a", question="ok1"), dict(item, id="mh_b", question="boom"), dict(item, id="mh_c", question="ok2")]
res = evaluate_items(items, run_fn)
check("one failing question doesn't abort the run", len(res) == 3 and res[1]["error"].startswith("RuntimeError") and not res[0]["error"])
agg = aggregate_agent_metrics(res)
check("errored item excluded from averages but counted", agg["items_errored"] == 1 and agg["multi_hop_count"] == 2)
check("aggregate numbers", agg["multi_hop_tool_f1"] == 1.0 and agg["multi_hop_answer_precision"] == 0.5 and agg["route_accuracy_multi_hop"] == 1.0 and agg["avg_retries"] == 1)
calls.clear()
res2 = evaluate_items(items, run_fn, done={"mh_a": res[0], "mh_c": res[2]})
check("--resume re-runs only the missing item", calls == ["boom"])

print(f"\nAll {N} checks passed.")
