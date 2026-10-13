"""
Phase 4 — pure, dependency-free scoring functions used by run_baseline_eval.py.

Deliberately has ZERO imports from retrieval/ or ingestion/ (no qdrant-client, no
sentence-transformers, no network) so these functions -- the actual arithmetic that turns raw
pipeline output into the numbers in summary.json -- can be unit-tested in any environment,
including one where those heavier packages aren't installed. See test_offline.py.
"""
import statistics
from collections import defaultdict


def compute_single_hop_hit(item: dict, retrieved_chunk_ids: list[str]) -> bool:
    """Did retrieval surface at least one chunk CUAD's own labels say contains the answer?"""
    expected = set(item["expected_source_chunk_ids"])
    return bool(expected.intersection(retrieved_chunk_ids))


def compute_mention_recall(names: list[str], answer_text: str) -> float:
    """Fraction of `names` that appear (case-insensitive substring) somewhere in `answer_text`."""
    if not names:
        return 1.0  # vacuously true: nothing was expected to be mentioned
    answer_lower = answer_text.lower()
    hits = sum(1 for name in names if name.lower() in answer_lower)
    return hits / len(names)


def aggregate_retrieval_metrics(per_item_results: list[dict]) -> dict:
    single_hop = [r for r in per_item_results if r["type"] == "single_hop"]
    multi_hop = [r for r in per_item_results if r["type"] == "multi_hop"]

    by_category = defaultdict(list)
    for r in single_hop:
        by_category[r["clause_category"]].append(r["chunk_hit"])

    return {
        "single_hop_count": len(single_hop),
        "single_hop_chunk_hit_rate": (
            statistics.mean(r["chunk_hit"] for r in single_hop) if single_hop else None
        ),
        "single_hop_hit_rate_by_category": {
            cat: statistics.mean(hits) for cat, hits in sorted(by_category.items())
        },
        "multi_hop_count": len(multi_hop),
        "multi_hop_avg_vendor_mention_recall": (
            statistics.mean(r["vendor_mention_recall"] for r in multi_hop) if multi_hop else None
        ),
    }
