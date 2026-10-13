"""
Phase 3 — the full hybrid retrieval pipeline:
    dense search (Qdrant) + keyword search (Postgres) -> RRF fusion -> cross-encoder rerank
    -> final top-N chunks, ready to hand to a generator (see generator.py / baseline_rag.py).

Usage:
    python retrieval/hybrid_retriever.py "what happens if a vendor terminates for convenience"
    python retrieval/hybrid_retriever.py "liability cap" --fake-embedder --fake-reranker --local-path data/processed/qdrant_local
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestion"))

from dense_retriever import dense_search  # noqa: E402
from embedder import FakeEmbedder, SentenceTransformerEmbedder  # noqa: E402
from fusion import reciprocal_rank_fusion  # noqa: E402
from keyword_retriever import keyword_search  # noqa: E402
from reranker import CrossEncoderReranker, FakeReranker  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402


def hybrid_retrieve(
    query: str,
    qdrant_client: QdrantClient,
    embedder,
    reranker,
    dense_k: int = 20,
    keyword_k: int = 20,
    final_k: int = 8,
    contract_ids: list[str] | None = None,
) -> list[dict]:
    """
    `contract_ids` (Phase 6): restrict BOTH the dense and keyword search to these contracts
    (used by the agent's "both" route after the SQL tool has narrowed the candidate set).

    Note on recall: dense and keyword search deliberately fail on *different* kinds of queries
    (keyword search misses natural-language phrasing it can't literally match; dense search can
    miss exact defined terms if the embedding model doesn't weight them heavily) — that's the
    whole reason to fuse both rather than pick one.
    """
    dense_results = dense_search(query, embedder, qdrant_client, top_k=dense_k, contract_ids=contract_ids)
    keyword_results = keyword_search(query, top_k=keyword_k, contract_ids=contract_ids)

    fused = reciprocal_rank_fusion([dense_results, keyword_results])

    reranked = reranker.rerank(query, fused)
    return reranked[:final_k]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--url", default="http://localhost:6333")
    parser.add_argument("--local-path", default=None, help="Use file-based local Qdrant instead of --url")
    parser.add_argument("--fake-embedder", action="store_true", help="Offline plumbing test only")
    parser.add_argument("--fake-reranker", action="store_true", help="Offline plumbing test only")
    parser.add_argument("--final-k", type=int, default=8)
    args = parser.parse_args()

    embedder = FakeEmbedder() if args.fake_embedder else SentenceTransformerEmbedder()
    reranker = FakeReranker() if args.fake_reranker else CrossEncoderReranker()
    client = QdrantClient(path=args.local_path) if args.local_path else QdrantClient(url=args.url)

    results = hybrid_retrieve(args.query, client, embedder, reranker, final_k=args.final_k)
    print(f"\nTop {len(results)} fused+reranked results for: {args.query!r}\n")
    for i, r in enumerate(results, 1):
        print(
            f"{i}. rerank={r.get('rerank_score', 0):.4f}  rrf={r['rrf_score']:.5f}  "
            f"from_lists={r['source_lists']}  {r['chunk_id']}  {r['categories']}"
        )
        print("   ", r["text"][:150].replace("\n", " "))
        print()
