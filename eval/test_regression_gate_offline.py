"""Offline tests for eval/check_regression.py. Run: python eval/test_regression_gate_offline.py"""
import copy
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_regression as cr  # noqa: E402

N = 0


def check(label, cond):
    global N
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label
    N += 1


BASE = {
    "items_total": 109, "items_errored": 0, "sql_error_items": 0, "avg_retries": 0.2, "avg_latency_s": 12.0,
    "verdicts": {"accept": 105, "give_up": 4},
    "route_accuracy_overall": 0.95, "route_accuracy_single_hop": 0.96, "route_accuracy_multi_hop": 0.8,
    "multi_hop_tool_precision": 0.9, "multi_hop_tool_recall": 0.85, "multi_hop_tool_f1": 0.87,
    "multi_hop_answer_recall": 0.7, "single_hop_chunk_hit_rate": 0.88,
}


def run(mod=None, **kw):
    cur = copy.deepcopy(BASE)
    for k, v in (mod or {}).items():
        cur[k] = v
    return cr.compare(BASE, cur, kw.get("tol", 0.05))


def status_of(rows, metric):
    return next(r[3] for r in rows if r[0] == metric)


code, rows = run()
check("identical run -> no regression (0)", code == 0 and all(r[3] == "PASS" for r in rows))
code, rows = run({"single_hop_chunk_hit_rate": 0.90, "multi_hop_tool_recall": 0.95})
check("improvements pass and are labelled", code == 0 and [r[4] for r in rows if r[0] == "single_hop_chunk_hit_rate"] == ["improved"])
code, rows = run({"single_hop_chunk_hit_rate": 0.84})
check("drop within tolerance (0.04 <= 0.05) passes", code == 0)
code, rows = run({"single_hop_chunk_hit_rate": 0.80})
check("drop beyond tolerance (0.08) fails and names the metric", code == 1 and status_of(rows, "single_hop_chunk_hit_rate") == "FAIL")
code, rows = run({"single_hop_chunk_hit_rate": 0.80}, tol=0.10)
check("tolerance is configurable", code == 0)
code, rows = run({"multi_hop_tool_f1": None})
check("a metric that existed but is now missing fails", code == 1 and status_of(rows, "multi_hop_tool_f1") == "FAIL")
b2 = dict(BASE, multi_hop_answer_precision=None)
code, rows = cr.compare(b2, dict(BASE, multi_hop_answer_precision=None), 0.05)
check("metric absent in both runs is ignored", code == 0 and all(r[0] != "multi_hop_answer_precision" for r in rows))
code, rows = run({"items_errored": 1})
check("a newly errored item fails", code == 1 and status_of(rows, "items_errored") == "FAIL")
code, rows = run({"sql_error_items": 2})
check("new SQL errors fail", code == 1)
code, rows = run({"verdicts": {"accept": 104, "give_up": 5}})
check("more give-ups fail", code == 1 and status_of(rows, "verdicts.give_up") == "FAIL")
code, rows = run({"verdicts": {"accept": 107, "give_up": 2}})
check("fewer give-ups pass", code == 0)
code, rows = run({"avg_latency_s": 40.0})
check("slow run only WARNS (exit still 0)", code == 0 and status_of(rows, "avg_latency_s") == "WARN")
code, rows = run({"avg_retries": 0.9})
check("more retries only WARN", code == 0 and status_of(rows, "avg_retries") == "WARN")
code, rows = run({"items_total": 5})
check("different question count -> cannot compare (2)", code == 2)
check("boolean values are not treated as numbers", cr._num(True) is None and cr._num(0.5) == 0.5 and cr._num("x") is None)

# drift guard: every metric the gate watches must really be produced by the eval harness
produced = (Path(__file__).resolve().parent / "agent_metrics.py").read_text(encoding="utf-8")
missing = [m for m in cr.HIGHER_BETTER + cr.MUST_NOT_INCREASE + list(cr.WARN_ONLY) if f'"{m}"' not in produced]
check("every gated metric name exists in eval/agent_metrics.py (no silent skips)", not missing)

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    cur_p, base_p = d / "cur.json", d / "sub" / "base.json"

    def main(*a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cr.main(["--current", str(cur_p), "--baseline", str(base_p), *a])
        return code, buf.getvalue()

    code, out = main()
    check("no current file -> exit 2 with a helpful message", code == 2 and "run_agent_eval.py" in out)
    cur_p.write_text(json.dumps(BASE))
    code, out = main()
    check("no baseline yet -> exit 2 pointing at --save-baseline", code == 2 and "--save-baseline" in out)
    code, out = main("--save-baseline")
    check("--save-baseline writes the baseline (creating the folder)", code == 0 and base_p.exists() and json.loads(base_p.read_text()) == BASE)
    code, out = main()
    check("CLI: same run vs its own baseline -> 0 and prints NO REGRESSION", code == 0 and "NO REGRESSION" in out)
    worse = dict(BASE, route_accuracy_overall=0.5)
    cur_p.write_text(json.dumps(worse))
    code, out = main()
    check("CLI: regression -> exit 1, table shows FAIL row", code == 1 and "REGRESSION DETECTED" in out and "route_accuracy_overall" in out)
    cur_p.write_text(json.dumps(dict(BASE, items_errored=3)))
    code, out = main("--save-baseline")
    check("refuses to bless a run with errored items", code == 2 and json.loads(base_p.read_text()) == BASE)
    code, out = main("--save-baseline", "--force")
    check("--force overrides that refusal", code == 0 and json.loads(base_p.read_text())["items_errored"] == 3)
    cur_p.write_text("{not json")
    code, out = main()
    check("corrupt current file -> exit 2, no traceback", code == 2 and "not valid JSON" in out)

print(f"\nAll {N} checks passed.")
