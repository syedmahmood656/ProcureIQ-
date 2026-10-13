"""
Offline unit tests for the pure scoring functions in run_baseline_eval.py -- no DB, no Qdrant,
no embedding/reranking/generation model, no network. These are exactly the functions that turn
raw retrieval/generation output into the numbers in summary.json, so getting them right matters
independently of whether you can run the full pipeline in a given environment.

Run:
    python eval/test_offline.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from metrics import (  # noqa: E402
    aggregate_retrieval_metrics,
    compute_mention_recall,
    compute_single_hop_hit,
)


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    assert condition, label


def test_single_hop_hit():
    item = {"expected_source_chunk_ids": ["c1::chunk_003", "c1::chunk_004"]}
    check("hit when one expected chunk retrieved", compute_single_hop_hit(item, ["c1::chunk_004", "x"]))
    check("miss when no expected chunk retrieved", not compute_single_hop_hit(item, ["c1::chunk_099"]))
    check("miss on empty retrieval", not compute_single_hop_hit(item, []))


def test_mention_recall():
    check("full recall, exact case", compute_mention_recall(["Acme Corp"], "Acme Corp has a cap.") == 1.0)
    check(
        "full recall, case-insensitive",
        compute_mention_recall(["Acme Corp"], "acme corp's contract says...") == 1.0,
    )
    check(
        "partial recall",
        abs(compute_mention_recall(["Acme Corp", "Globex"], "Only Acme Corp appears here.") - 0.5) < 1e-9,
    )
    check("zero recall", compute_mention_recall(["Acme Corp"], "No relevant vendors mentioned.") == 0.0)
    check("vacuous true on empty expected list", compute_mention_recall([], "anything") == 1.0)


def test_aggregate():
    results = [
        {"type": "single_hop", "clause_category": "Cap On Liability", "chunk_hit": True},
        {"type": "single_hop", "clause_category": "Cap On Liability", "chunk_hit": False},
        {"type": "single_hop", "clause_category": "Governing Law", "chunk_hit": True},
        {"type": "multi_hop", "vendor_mention_recall": 0.5},
        {"type": "multi_hop", "vendor_mention_recall": 0.0},
    ]
    summary = aggregate_retrieval_metrics(results)
    check("single_hop_count == 3", summary["single_hop_count"] == 3)
    check("overall hit rate == 2/3", abs(summary["single_hop_chunk_hit_rate"] - (2 / 3)) < 1e-9)
    check(
        "per-category breakdown correct",
        abs(summary["single_hop_hit_rate_by_category"]["Cap On Liability"] - 0.5) < 1e-9
        and summary["single_hop_hit_rate_by_category"]["Governing Law"] == 1.0,
    )
    check("multi_hop_count == 2", summary["multi_hop_count"] == 2)
    check("multi-hop avg recall == 0.25", abs(summary["multi_hop_avg_vendor_mention_recall"] - 0.25) < 1e-9)


def test_aggregate_handles_empty_groups():
    # No multi-hop items at all -- must not crash, should report None rather than divide by zero.
    results = [{"type": "single_hop", "clause_category": "Cap On Liability", "chunk_hit": True}]
    summary = aggregate_retrieval_metrics(results)
    check("multi_hop_avg is None when no multi-hop items", summary["multi_hop_avg_vendor_mention_recall"] is None)


if __name__ == "__main__":
    test_single_hop_hit()
    test_mention_recall()
    test_aggregate()
    test_aggregate_handles_empty_groups()
    print("\nAll offline scoring tests passed.")
