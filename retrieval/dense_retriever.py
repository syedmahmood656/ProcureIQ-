"""
Phase 3 — dense (semantic) retrieval against Qdrant.

Uses `embed_query()` (not `embed()`) since BGE models need the query-side instruction prefix
for good retrieval quality — see ingestion/embedder.py.

Usage (standalone test):
    python retrieval/dense_retriever.py "what happens if a vendor terminates for convenience" --url http://localhost:6333
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestion"))
from embedder import FakeEmbedder, SentenceTransformerEmbedder  # noqa: E402

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny

COLLECTION = "contract_chunks"


def dense_search(
    query: str,
    embedder,
    client: QdrantClient,
    top_k: int = 20,
    contract_ids: list[str] | None = None,
) -> list[dict]:
    """
    `contract_ids` (Phase 6): if given, only chunks belonging to those contracts are searched.
    This is how the SQL tool's output (the vendors/contracts that matched the spend/date
    conditions) narrows the contract-text search in a multi-hop question.
    """
    vector = embedder.embed_query(query)
    query_filter = (
        Filter(must=[FieldCondition(key="contract_id", match=MatchAny(any=list(contract_ids)))])
        if contract_ids
        else None
    )
    result = client.query_points(
        collection_name=COLLECTION,
        query=vector,
        limit=top_k,
        with_payload=True,
        query_filter=query_filter,
    )
    return [
        {
            "chunk_id": p.payload["chunk_id"],
            "contract_id": p.payload["contract_id"],
            "categories": p.payload["categories"],
            "text": p.payload["text"],
            "score": p.score,
        }
        for p in result.points
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--url", default="http://localhost:6333")
    parser.add_argument("--local-path", default=None, help="Use file-based local Qdrant instead of --url")
    parser.add_argument("--fake-embedder", action="store_true", help="Offline plumbing test only")
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    embedder = FakeEmbedder() if args.fake_embedder else SentenceTransformerEmbedder()
    client = QdrantClient(path=args.local_path) if args.local_path else QdrantClient(url=args.url)

    results = dense_search(args.query, embedder, client, top_k=args.top_k)
    print(f"{len(results)} dense matches for: {args.query!r}\n")
    for i, r in enumerate(results, 1):
        print(f"{i}. [score={r['score']:.4f}] {r['chunk_id']} {r['categories']}")
        print("   ", r["text"][:120].replace("\n", " "))
