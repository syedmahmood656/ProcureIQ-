"""
Phase 3 — baseline (non-agentic) RAG endpoint: retrieve -> build a grounded prompt -> generate.

Deliberately simple: single retrieval pass, no tool use, no query rewriting, no self-check or
retry loop. This is the yardstick Phase 4's evaluation harness compares the eventual agentic
pipeline (Phase 5+) against — if the agentic system can't beat this, the added complexity
isn't earning its keep.

Usage:
    python retrieval/baseline_rag.py "what is the liability cap for vendor X" --provider fake
    python retrieval/baseline_rag.py "what is the liability cap for vendor X" --provider gemini
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestion"))

from embedder import FakeEmbedder, SentenceTransformerEmbedder  # noqa: E402
from generator import get_generator  # noqa: E402
from hybrid_retriever import hybrid_retrieve  # noqa: E402
from reranker import CrossEncoderReranker, FakeReranker  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

PROMPT_TEMPLATE = """You are a procurement contract assistant. Answer the question using ONLY \
the contract excerpts below. Every claim must cite its source contract_id in square brackets, \
e.g. [0107_...]. If the excerpts don't contain the answer, say so explicitly — never guess or \
use outside knowledge.

--- CONTRACT EXCERPTS ---
{context}
--- END EXCERPTS ---

Question: {question}

Answer (with [contract_id] citations):"""


def build_prompt(question: str, chunks: list[dict]) -> str:
    context = "\n\n".join(
        f"[{c['contract_id']}] (categories: {', '.join(c['categories'])})\n{c['text']}"
        for c in chunks
    )
    return PROMPT_TEMPLATE.format(context=context, question=question)


def answer_question(
    question: str, qdrant_client: QdrantClient, embedder, reranker, generator, top_k: int = 8
) -> dict:
    chunks = hybrid_retrieve(question, qdrant_client, embedder, reranker, final_k=top_k)
    prompt = build_prompt(question, chunks)
    answer = generator.generate(prompt)
    return {
        "question": question,
        "answer": answer,
        "sources": [{"contract_id": c["contract_id"], "chunk_id": c["chunk_id"]} for c in chunks],
        "prompt": prompt,  # kept for debugging and for Phase 4's eval logging
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument(
        "--provider", choices=["openrouter", "huggingface", "gemini", "fake"], default="fake"
    )
    parser.add_argument("--url", default="http://localhost:6333")
    parser.add_argument("--local-path", default=None, help="Use file-based local Qdrant instead of --url")
    parser.add_argument("--fake-embedder", action="store_true", help="Offline plumbing test only")
    parser.add_argument("--fake-reranker", action="store_true", help="Offline plumbing test only")
    parser.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()

    embedder = FakeEmbedder() if args.fake_embedder else SentenceTransformerEmbedder()
    reranker = FakeReranker() if args.fake_reranker else CrossEncoderReranker()
    client = QdrantClient(path=args.local_path) if args.local_path else QdrantClient(url=args.url)
    generator = get_generator(args.provider)

    result = answer_question(args.question, client, embedder, reranker, generator, top_k=args.top_k)
    print("QUESTION:", result["question"])
    print("\nANSWER:\n", result["answer"])
    print("\nSOURCES:")
    for s in result["sources"]:
        print("  -", s["contract_id"], f"({s['chunk_id']})")
