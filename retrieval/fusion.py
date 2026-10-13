"""
Phase 3 — Reciprocal Rank Fusion (RRF).

Dense (cosine similarity) and keyword (ts_rank) scores live on completely different, unrelated
scales — a 0.82 cosine score and a 0.03 ts_rank score can't be compared directly. RRF sidesteps
this by ignoring raw scores entirely and using only each result's *rank position* within its own
list:

    score(doc) = sum over every list containing doc of  1 / (k + rank(doc))

A doc that appears near the top of both lists scores highest; a doc that appears in only one
list still contributes, just less. `k` (default 60, the value from the original RRF paper)
dampens the difference between rank 1 and rank 2 so the fusion isn't overly sensitive to noise
at the very top of either list.

Usage (standalone smoke test, no DB/model needed):
    python retrieval/fusion.py
"""
from collections import defaultdict


def reciprocal_rank_fusion(
    ranked_lists: list[list[dict]], k: int = 60, id_key: str = "chunk_id"
) -> list[dict]:
    """
    ranked_lists: one or more ranked result lists (each already sorted best-first), each item
        a dict containing at least `id_key`.

    Returns a single fused list sorted by RRF score descending. Each returned dict is the
    original item's metadata (taken from wherever it first appeared) plus two added fields:
    `rrf_score` and `source_lists` (the indices of ranked_lists that contained this item —
    useful later for debugging why something ranked where it did).
    """
    scores = defaultdict(float)
    metadata = {}
    sources = defaultdict(list)

    for list_idx, ranked_list in enumerate(ranked_lists):
        for rank, item in enumerate(ranked_list, start=1):
            key = item[id_key]
            scores[key] += 1.0 / (k + rank)
            sources[key].append(list_idx)
            if key not in metadata:
                metadata[key] = item

    fused = []
    for key, score in scores.items():
        row = dict(metadata[key])
        row["rrf_score"] = score
        row["source_lists"] = sources[key]
        fused.append(row)

    fused.sort(key=lambda r: r["rrf_score"], reverse=True)
    return fused


if __name__ == "__main__":
    dense = [{"chunk_id": "A"}, {"chunk_id": "B"}, {"chunk_id": "C"}]
    keyword = [{"chunk_id": "C"}, {"chunk_id": "D"}, {"chunk_id": "A"}]

    fused = reciprocal_rank_fusion([dense, keyword])
    print("Fused ranking (A and C appear in both lists, so should rank above B/D):")
    for r in fused:
        print(f"  {r['chunk_id']}: rrf_score={r['rrf_score']:.5f}  from_lists={r['source_lists']}")

    assert fused[0]["chunk_id"] in ("A", "C"), "item appearing in both lists should rank first"
    print("\nSmoke test passed.")
