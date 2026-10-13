"""
Phase 2 — load chunks.jsonl into Qdrant.

Each point stores the embedding plus a payload with contract_id, categories, and char offsets
— enough for the agent to (a) filter by clause category before searching, and (b) trace any
retrieved chunk back to its source contract file and, via Postgres, its vendor.

Local/offline usage (no Qdrant server needed — file-based storage):
    python ingestion/build_qdrant_index.py --local-path data/processed/qdrant_local --fake-embedder

Real usage, against a running Qdrant server (see infra/docker-compose.yml), with real embeddings:
    python ingestion/build_qdrant_index.py --url http://localhost:6333
"""
import argparse
import json

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

COLLECTION = "contract_chunks"
BATCH_SIZE = 128


def get_client(url: str | None, local_path: str | None) -> QdrantClient:
    if url:
        return QdrantClient(url=url)
    return QdrantClient(path=local_path)


def load_chunks(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def main(chunks_path: str, url: str | None, local_path: str | None, use_fake_embedder: bool, model_name: str):
    if use_fake_embedder:
        from embedder import FakeEmbedder
        embedder = FakeEmbedder()
        print("WARNING: using FakeEmbedder — vectors are NOT semantically meaningful. "
              "This run only validates the Qdrant plumbing, not retrieval quality.")
    else:
        from embedder import SentenceTransformerEmbedder
        embedder = SentenceTransformerEmbedder(model_name)

    client = get_client(url, local_path)

    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=embedder.dim, distance=Distance.COSINE),
    )

    chunks = load_chunks(chunks_path)
    print(f"Embedding + upserting {len(chunks)} chunks (batch size {BATCH_SIZE})...")

    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        vectors = embedder.embed([c["text"] for c in batch])
        points = [
            PointStruct(
                id=i + j,
                vector=vec,
                payload={
                    "contract_id": c["contract_id"],
                    "chunk_id": c["chunk_id"],
                    "categories": c["categories"],
                    "char_start": c["char_start"],
                    "char_end": c["char_end"],
                    "text": c["text"],
                },
            )
            for j, (c, vec) in enumerate(zip(batch, vectors))
        ]
        client.upsert(collection_name=COLLECTION, points=points)
        if (i // BATCH_SIZE) % 20 == 0:
            print(f"  {min(i + BATCH_SIZE, len(chunks))}/{len(chunks)}")

    count = client.count(COLLECTION).count
    print(f"Done. Collection '{COLLECTION}' now has {count} points.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", default="data/processed/chunks.jsonl")
    parser.add_argument("--url", default=None, help="Qdrant server URL, e.g. http://localhost:6333")
    parser.add_argument("--local-path", default="data/processed/qdrant_local",
                         help="Used if --url is not given: file-based local Qdrant, no server needed")
    parser.add_argument("--fake-embedder", action="store_true",
                         help="Use the offline deterministic stand-in instead of a real model")
    parser.add_argument("--model-name", default="BAAI/bge-small-en-v1.5")
    args = parser.parse_args()
    main(args.chunks, args.url, args.local_path, args.fake_embedder, args.model_name)
