"""
Quality gate: compare an agent eval run against a saved baseline; exit non-zero on a regression.

WORKFLOW (needs your data + LLM key, so this runs on your machine, not in CI):
    # 1. produce a result (writes data/eval/results_agent/summary.json)
    python eval/run_agent_eval.py --provider openrouter --types all --sleep 2 --resume
    # 2. first time only: bless it as the baseline (committed to git: eval/baseline/agent_summary.json)
    python eval/check_regression.py --save-baseline
    # 3. after ANY change to prompts / retrieval / router / models: re-run step 1, then
    python eval/check_regression.py            # exit 0 = no regression, 1 = regression, 2 = can't compare

WHAT COUNTS AS A REGRESSION
  * higher-is-better metrics (route accuracy, vendor precision/recall, chunk hit-rate ...) dropped by
    more than --tolerance (absolute; default 0.05)
  * any errored item or SQL-error item appeared, or give-ups increased
  * a metric that had a value in the baseline is now missing
  Latency and retry counts only WARN: free-tier LLM speed is too noisy to gate on.
  Runs are only comparable if they scored the same number of questions (otherwise exit 2).
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CURRENT = REPO_ROOT / "data" / "eval" / "results_agent" / "summary.json"
DEFAULT_BASELINE = REPO_ROOT / "eval" / "baseline" / "agent_summary.json"

HIGHER_BETTER = [
    "route_accuracy_overall", "route_accuracy_single_hop", "route_accuracy_multi_hop",
    "multi_hop_tool_precision", "multi_hop_tool_recall", "multi_hop_tool_f1", "multi_hop_tool_exact_match_rate",
    "multi_hop_answer_precision", "multi_hop_answer_recall", "multi_hop_answer_f1",
    "multi_hop_avg_vendor_mention_recall", "single_hop_chunk_hit_rate",
]
MUST_NOT_INCREASE = ["items_errored", "sql_error_items"]  # counts: any increase fails
WARN_ONLY = {"avg_retries": 0.25, "avg_latency_s": None}  # abs slack / None = warn if >1.5x


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def compare(base: dict, cur: dict, tol: float):
    """-> (exit_code, rows). rows = (metric, baseline, current, status, note); status PASS|WARN|FAIL."""
    if base.get("items_total") != cur.get("items_total"):
        return 2, [("items_total", base.get("items_total"), cur.get("items_total"), "FAIL",
                    "different number of questions scored: runs are not comparable")]
    rows = []
    for m in HIGHER_BETTER:
        b, c = _num(base.get(m)), _num(cur.get(m))
        if b is None and c is None:
            continue
        if b is None:
            rows.append((m, b, c, "PASS", "new metric"))
        elif c is None:
            rows.append((m, b, c, "FAIL", "metric disappeared"))
        elif c < b - tol - 1e-9:
            rows.append((m, b, c, "FAIL", f"dropped {b - c:.3f} (tolerance {tol})"))
        else:
            rows.append((m, b, c, "PASS", "" if c <= b else "improved"))
    for m in MUST_NOT_INCREASE:
        b, c = _num(base.get(m)) or 0, _num(cur.get(m)) or 0
        rows.append((m, b, c, "FAIL" if c > b else "PASS", f"increased by {c - b}" if c > b else ""))
    bg, cg = (base.get("verdicts") or {}).get("give_up", 0), (cur.get("verdicts") or {}).get("give_up", 0)
    rows.append(("verdicts.give_up", bg, cg, "FAIL" if cg > bg else "PASS", f"{cg - bg} more give-up(s)" if cg > bg else ""))
    for m, slack in WARN_ONLY.items():
        b, c = _num(base.get(m)), _num(cur.get(m))
        if b is None or c is None:
            continue
        worse = (c > b + slack) if slack is not None else (c > b * 1.5)
        rows.append((m, b, c, "WARN" if worse else "PASS", "worse than baseline (informational)" if worse else ""))
    return (1 if any(r[3] == "FAIL" for r in rows) else 0), rows


def _fmt(v):
    return "-" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))


def print_table(rows):
    w = max(len(r[0]) for r in rows)
    print(f"{'metric'.ljust(w)}  {'baseline':>9}  {'current':>9}  status")
    for m, b, c, s, note in rows:
        print(f"{m.ljust(w)}  {_fmt(b):>9}  {_fmt(c):>9}  {s}{('  ' + note) if note else ''}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--current", type=Path, default=DEFAULT_CURRENT)
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--tolerance", type=float, default=0.05, help="allowed absolute drop for rate metrics")
    ap.add_argument("--save-baseline", action="store_true", help="copy --current to --baseline and exit")
    ap.add_argument("--force", action="store_true", help="with --save-baseline: allow a run that had errored items")
    args = ap.parse_args(argv)

    if not args.current.exists():
        print(f"no current results at {args.current}\nrun: python eval/run_agent_eval.py --provider openrouter --types all")
        return 2
    try:
        cur = json.loads(args.current.read_text(encoding="utf-8"))
    except ValueError:
        print(f"{args.current} is not valid JSON")
        return 2

    if args.save_baseline:
        if (cur.get("items_errored") or 0) > 0 and not args.force:
            print(f"refusing to bless a run with {cur['items_errored']} errored item(s) as the baseline (use --force to override)")
            return 2
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.current, args.baseline)
        print(f"baseline saved: {args.baseline}\ncommit it:  git add {args.baseline.relative_to(REPO_ROOT) if args.baseline.is_relative_to(REPO_ROOT) else args.baseline}")
        return 0

    if not args.baseline.exists():
        print(f"no baseline at {args.baseline}\nbless a good run first:  python eval/check_regression.py --save-baseline")
        return 2
    try:
        base = json.loads(args.baseline.read_text(encoding="utf-8"))
    except ValueError:
        print(f"{args.baseline} is not valid JSON")
        return 2

    code, rows = compare(base, cur, args.tolerance)
    print_table(rows)
    print({0: "\nNO REGRESSION", 1: "\nREGRESSION DETECTED", 2: "\nCANNOT COMPARE"}[code])
    return code


if __name__ == "__main__":
    sys.exit(main())
