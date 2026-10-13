"""
Phase 3 — cross-encoder reranking.

Dense retrieval uses a bi-encoder: query and document are embedded separately, then compared
by cosine similarity — fast enough to search the whole corpus, but the model never actually
sees the query and document together. A cross-encoder does see them together (as a single
input pair), which is meaningfully more accurate at judging true relevance, but too slow to
run against the whole corpus. So the pattern is: fast bi-encoder + keyword search narrow the
corpus down to ~20-40 candidates (Phase 3's RRF fusion step), then the cross-encoder does a
final, accurate reordering of just that shortlist.

Two interchangeable backends, same interface: `rerank(query, candidates) -> reranked candidates`.
"""


class CrossEncoderReranker:
    """Production reranker. Requires: pip install sentence-transformers (already a dependency)."""

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(model_name)

    def rerank(self, query: str, candidates: list[dict], text_key: str = "text") -> list[dict]:
        if not candidates:
            return []
        pairs = [(query, c[text_key]) for c in candidates]
        scores = self.model.predict(pairs)
        for c, s in zip(candidates, scores):
            c["rerank_score"] = float(s)
        return sorted(candidates, key=lambda c: c["rerank_score"], reverse=True)


class FakeReranker:
    """
    Offline, dependency-free stand-in — NOT semantically meaningful. Scores each candidate
    deterministically from a hash of (query, text), so ordering is stable across runs but
    carries no real relevance signal. Use only to test the reranking plumbing (scores attach
    correctly, sort direction is right, slicing to top-N works) without downloading a model.
    """

    def rerank(self, query: str, candidates: list[dict], text_key: str = "text") -> list[dict]:
        import hashlib

        if not candidates:
            return []
        for c in candidates:
            h = hashlib.sha256((query + "||" + c[text_key]).encode("utf-8")).hexdigest()
            c["rerank_score"] = int(h[:8], 16) / 0xFFFFFFFF
        return sorted(candidates, key=lambda c: c["rerank_score"], reverse=True)
