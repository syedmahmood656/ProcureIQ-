"""
Phase 6 — score the AGENT (agent/graph.py: router + SQL tool + exact clause lookup + RAG) on the
same gold questions the Phase 4 baseline was scored on, and print a side-by-side comparison.

What it measures (no LLM judge, no RAGAS -> cheap on free tiers; the only LLM calls are the
agent's own):
  * multi-hop (5 questions): vendor-set precision / recall / F1 for the tools AND for the answer
    text, plus the baseline's own "mention recall" metric for a like-for-like before/after.
  * routing: did the router pick `both` for multi-hop and `rag` for single-hop?
  * single-hop (104 questions, only with --types all or single_hop): chunk hit-rate, same metric
    as the baseline.
  * operational: retries, give-ups, SQL errors, latency.

Baseline results in data/eval/results/ are NEVER overwritten; agent results go to
data/eval/results_agent/.

Usage (repo root, docker compose up, provider configured in .env):
    # The Phase 6 target: the 5 multi-hop questions (default; ~15-30 LLM calls)
    python eval/run_agent_eval.py --provider openrouter

    # Everything (109 questions; slow on free tiers -- use --sleep and --resume)
    python eval/run_agent_eval.py --provider openrouter --types all --sleep 2 --resume

    # Quick wiring check
    python eval/run_agent_eval.py --provider openrouter --types multi_hop --limit 1
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for sub in ("eval", "agent", "retrieval", "ingestion"):
    sys.path.insert(0, str(REPO_ROOT / sub))

from agent_metrics import (  # noqa: E402
    aggregate_agent_metrics,
    expected_route,
    names_in_text,
    predicted_contract_ids,
    set_prf,
)
from metrics import compute_mention_recall, compute_single_hop_hit  # noqa: E402


def load_gold(path: Path, types: str, limit: int | None) -> list[dict]:
    items = [json.loads(line) for line in open(path, encoding="utf-8")]
    if types != "all":
        items = [i for i in items if i["type"] == types]
    return items[:limit] if limit else items


def score_item(item: dict, state: dict, latency_s: float) -> dict:
    """Turn one agent run (final graph state) into a scored result row."""
    row = {
        "id": item["id"],
        "type": item["type"],
        "question": item["question"],
        "clause_category": item["clause_category"],
        "route": state["route"],
        "expected_route": expected_route(item["type"]),
        "route_ok": state["route"] == expected_route(item["type"]),
        "verdict": state["verdict"],
        "retries": state["retry_count"],
        "sql_query": state["sql_query"],
        "sql_error": state["sql_error"],
        "n_sql_rows": len(state["sql_rows"]),
        "answer": state["answer"],
        "latency_s": round(latency_s, 2),
        "trace": state["trace"],
        "error": "",
    }
    if item["type"] == "single_hop":
        row["chunk_hit"] = compute_single_hop_hit(item, [c["chunk_id"] for c in state["retrieved_chunks"]])
    else:
        expected_contracts = {m["contract_id"] for m in item["expected_matches"]}
        expected_names = [m["vendor_name"] for m in item["expected_matches"]]
        sql_vendor_names = {r["vendor_name"] for r in state["sql_rows"] if r.get("vendor_name")}
        predicted_contracts = predicted_contract_ids(state)
        predicted_names = names_in_text(sql_vendor_names, state["answer"])

        row["expected_vendor_names"] = expected_names
        row["tool"] = set_prf(predicted_contracts, expected_contracts)
        row["answer_score"] = set_prf(predicted_names, set(expected_names))
        row["vendor_mention_recall"] = compute_mention_recall(expected_names, state["answer"])
        row["missed_contracts"] = sorted(expected_contracts - predicted_contracts)
        row["extra_contracts"] = sorted(predicted_contracts - expected_contracts)
    return row


def evaluate_items(items, run_fn, sleep_s=0.0, done: dict | None = None, on_row=None) -> list[dict]:
    """
    run_fn(question) -> final agent state. Errors on one question never abort the run (free-tier
    APIs do fail mid-run); the item is recorded with `error` set and excluded from the averages.
    `done` maps id -> previously scored row (for --resume).
    """
    results = []
    for i, item in enumerate(items, 1):
        if done and item["id"] in done:
            print(f"  [{i}/{len(items)}] {item['id']}: (resumed from previous run)")
            results.append(done[item["id"]])
            continue
        print(f"  [{i}/{len(items)}] {item['id']}: {item['question'][:90]}")
        t0 = time.perf_counter()
        try:
            state = run_fn(item["question"])
            row = score_item(item, state, time.perf_counter() - t0)
        except Exception as e:  # noqa: BLE001
            print(f"      !! failed: {type(e).__name__}: {str(e)[:150]}")
            row = {"id": item["id"], "type": item["type"], "question": item["question"],
                   "error": f"{type(e).__name__}: {e}"}
        results.append(row)
        if on_row:
            on_row(row)
        if sleep_s:
            time.sleep(sleep_s)
    return results


def print_multi_hop_table(results: list[dict]) -> None:
    rows = [r for r in results if r["type"] == "multi_hop" and not r.get("error")]
    if not rows:
        return
    print("\n=== MULTI-HOP, PER QUESTION ===")
    print(f"{'id':<28}{'route':<6}{'tool P/R/F1':<18}{'answer P/R/F1':<18}{'mention':<9}{'retry':<6}verdict")
    for r in rows:
        t, a = r["tool"], r["answer_score"]
        tool_col = f"{t['precision']:.2f}/{t['recall']:.2f}/{t['f1']:.2f}"
        ans_col = f"{a['precision']:.2f}/{a['recall']:.2f}/{a['f1']:.2f}"
        print(
            f"{r['id']:<28}{r['route']:<6}{tool_col:<18}{ans_col:<18}"
            f"{r['vendor_mention_recall']:<9.2f}{r['retries']:<6}{r['verdict']}"
        )
        if r["missed_contracts"] or r["extra_contracts"]:
            print(f"    missed {len(r['missed_contracts'])} expected contract(s), "
                  f"{len(r['extra_contracts'])} extra")


def print_baseline_comparison(summary: dict, baseline_dir: Path) -> None:
    path = baseline_dir / "summary.json"
    if not path.exists():
        print("\n(no baseline summary found at data/eval/results/summary.json -- skipping comparison)")
        return
    base = json.load(open(path, encoding="utf-8"))["retrieval_metrics"]
    per_item = baseline_dir / "per_item_results.jsonl"
    looks_fake = per_item.exists() and "[FAKE ANSWER" in per_item.read_text(encoding="utf-8")

    def fmt(x):
        return "n/a" if x is None else f"{x:.3f}"

    print("\n=== BASELINE (Phase 4) vs AGENT (Phase 6) ===")
    if looks_fake:
        print("!! The baseline results file was produced with --provider fake (answers say '[FAKE ANSWER').")
        print("!! Re-run `python eval/run_baseline_eval.py --provider <real> --skip-ragas` for a real baseline.")
    print(f"{'metric':<42}{'baseline':>10}{'agent':>10}")
    if summary["multi_hop_count"]:
        print(f"{'multi-hop vendor mention recall':<42}"
              f"{fmt(base.get('multi_hop_avg_vendor_mention_recall')):>10}"
              f"{fmt(summary['multi_hop_avg_vendor_mention_recall']):>10}")
    if summary["single_hop_count"]:
        print(f"{'single-hop chunk hit-rate':<42}"
              f"{fmt(base.get('single_hop_chunk_hit_rate')):>10}"
              f"{fmt(summary['single_hop_chunk_hit_rate']):>10}")
    print("(baseline cannot be scored on precision/F1/routing -- it only produces one answer text)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default=str(REPO_ROOT / "data/eval/gold_questions.jsonl"))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "data/eval/results_agent"))
    parser.add_argument("--baseline-dir", default=str(REPO_ROOT / "data/eval/results"))
    parser.add_argument("--types", choices=["multi_hop", "single_hop", "all"], default="multi_hop")
    parser.add_argument("--provider", choices=["openrouter", "huggingface", "gemini", "fake"], default="fake")
    parser.add_argument("--url", default="http://localhost:6333")
    parser.add_argument("--local-path", default=None, help="Use file-based local Qdrant instead of --url")
    parser.add_argument("--fake-embedder", action="store_true")
    parser.add_argument("--fake-reranker", action="store_true")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--limit", type=int, default=None, help="Only the first N selected items")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to pause between questions (free-tier rate limits)")
    parser.add_argument("--resume", action="store_true", help="Skip ids already scored in --out-dir")
    args = parser.parse_args()

    from embedder import FakeEmbedder, SentenceTransformerEmbedder
    from generator import get_generator
    from graph import run_agent
    from qdrant_client import QdrantClient
    from reranker import CrossEncoderReranker, FakeReranker

    embedder = FakeEmbedder() if args.fake_embedder else SentenceTransformerEmbedder()
    reranker = FakeReranker() if args.fake_reranker else CrossEncoderReranker()
    client = QdrantClient(path=args.local_path) if args.local_path else QdrantClient(url=args.url)
    generator = get_generator(args.provider)

    def run_fn(question):
        return run_agent(question, client, embedder, reranker, generator, max_retries=args.max_retries)

    items = load_gold(Path(args.gold), args.types, args.limit)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    per_item_path = out_dir / "per_item_results.jsonl"

    done = {}
    if args.resume and per_item_path.exists():
        for line in open(per_item_path, encoding="utf-8"):
            r = json.loads(line)
            if not r.get("error"):
                done[r["id"]] = r

    print(f"Scoring the AGENT on {len(items)} gold question(s) (types={args.types}, provider={args.provider})...")
    results = evaluate_items(items, run_fn, sleep_s=args.sleep, done=done)

    with open(per_item_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    summary = aggregate_agent_metrics(results)
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print_multi_hop_table(results)
    print("\n=== AGENT SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print_baseline_comparison(summary, Path(args.baseline_dir))
    print(f"\nWrote {per_item_path} and {out_dir / 'summary.json'}")
    errored = [r["id"] for r in results if r.get("error")]
    if errored:
        print(f"!! {len(errored)} item(s) errored and are excluded from averages: {errored}")
        print("!! Re-run with --resume to retry only those.")


if __name__ == "__main__":
    main()
