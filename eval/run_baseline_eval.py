"""
Phase 4 — score the Phase 3 baseline RAG endpoint against eval/gold_questions.jsonl (built by
build_gold_questions.py). This is the number every later, more complex (agentic, Phase 5+)
version of the pipeline has to beat to justify its added cost/latency/complexity.

Two independent layers of scoring, deliberately kept separate:

1. RETRIEVAL METRICS (free, offline, no LLM judge needed) -- computed directly from IDs:
   - single-hop "chunk hit-rate": did the retrieved top-K contain at least one of the chunks
     CUAD's own clause labels say actually contains the answer?
   - multi-hop "mention recall": crude but useful and free -- for questions like "which vendors
     with spend > $X have an uncapped-liability clause", does the baseline's generated answer
     text actually *mention* the vendor names that a correct join would return? The baseline
     only ever sees whatever ~8 chunks retrieval found for the raw question text, with no
     structured join over spend/renewal data at all -- so a LOW score here for multi-hop
     questions is the expected, useful result: it's the concrete evidence for the exact gap
     Phase 5 (agentic, tool-using) is meant to close, not a bug in this script.

2. RAGAS METRICS (needs a real LLM judge + real embeddings -- NOT runnable in this sandbox,
   run on your machine): context_precision, context_recall, faithfulness, answer_relevancy,
   computed by RAGAS over (question, answer, retrieved contexts, ground_truth) for every item.

Usage (on your machine, with `docker compose up` running and a provider configured in `.env`):
    # Full run: retrieval metrics + RAGAS, using Gemini as both the SUT generator and RAGAS judge
    python eval/run_baseline_eval.py --provider gemini --ragas-judge gemini

    # Retrieval metrics only, no LLM calls at all beyond the SUT generator itself
    python eval/run_baseline_eval.py --provider openrouter --skip-ragas

    # Fast offline smoke test of the whole harness' wiring (no real model/API/DB needed)
    python eval/run_baseline_eval.py --provider fake --fake-embedder --fake-reranker \\
        --local-path data/processed/qdrant_local --skip-ragas --limit 5

RAGAS judge support: OpenRouter and Gemini only (both have simple LangChain chat-model
wrappers). Hugging Face Inference API is not wired as a RAGAS judge to keep scope contained --
you can still use `--provider huggingface` for the generator being *evaluated*, just pick
`--ragas-judge openrouter` or `--ragas-judge gemini` for the judge itself.
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "retrieval"))
sys.path.insert(0, str(REPO_ROOT / "ingestion"))

from metrics import (  # noqa: E402
    aggregate_retrieval_metrics,
    compute_mention_recall,
    compute_single_hop_hit,
)

# NOTE: everything below this point (baseline_rag, hybrid_retriever, embedder, generator,
# reranker, qdrant_client) is imported lazily, inside the functions that need it, rather than
# at module level. Those modules pull in qdrant-client / sentence-transformers, which aren't
# installed in every environment (e.g. this sandbox) -- keeping them out of the top-level
# import means eval/test_offline.py can still exercise the pure scoring logic above anywhere.


# --------------------------------------------------------------------------------------------
# Pipeline orchestration (needs Qdrant + Postgres + a real embedder/reranker/generator).
# --------------------------------------------------------------------------------------------

def load_gold(path: Path, limit: int | None = None) -> list[dict]:
    items = [json.loads(line) for line in open(path, encoding="utf-8")]
    return items[:limit] if limit else items


def run_item(item: dict, qdrant_client, embedder, reranker, generator, top_k: int) -> dict:
    from baseline_rag import build_prompt
    from hybrid_retriever import hybrid_retrieve

    chunks = hybrid_retrieve(item["question"], qdrant_client, embedder, reranker, final_k=top_k)
    prompt = build_prompt(item["question"], chunks)
    answer = generator.generate(prompt)

    retrieved_chunk_ids = [c["chunk_id"] for c in chunks]
    contexts = [c["text"] for c in chunks]

    result = {
        "id": item["id"],
        "type": item["type"],
        "question": item["question"],
        "answer": answer,
        "ground_truth": item["ground_truth"],
        "contexts": contexts,
        "retrieved_chunk_ids": retrieved_chunk_ids,
    }

    if item["type"] == "single_hop":
        result["clause_category"] = item["clause_category"]
        result["chunk_hit"] = compute_single_hop_hit(item, retrieved_chunk_ids)
    else:
        vendor_names = [m["vendor_name"] for m in item["expected_matches"]]
        result["clause_category"] = item["clause_category"]
        result["expected_vendor_names"] = vendor_names
        result["vendor_mention_recall"] = compute_mention_recall(vendor_names, answer)

    return result


def build_ragas_judge(provider: str):
    """LangChain chat-model wrapper for RAGAS's judge LLM. Only openrouter/gemini supported."""
    import os

    if provider == "openrouter":
        from langchain_openai import ChatOpenAI

        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("Set OPENROUTER_API_KEY in your .env file")
        return ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model="meta-llama/llama-3.1-8b-instruct:free",
            temperature=0.0,
            request_timeout=120,
            max_retries=5,
        )
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("Set GEMINI_API_KEY in your .env file")
        return ChatGoogleGenerativeAI(
            model="gemini-3.6-flash", 
            google_api_key=api_key,
            request_timeout=120,
            max_retries=5,
        )
    raise ValueError(f"Unsupported RAGAS judge provider: {provider!r} (use openrouter or gemini)")


def run_ragas(per_item_results: list[dict], judge_provider: str):
    from datasets import Dataset
    from ragas import evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import answer_relevancy, context_precision, context_recall, faithfulness
    from ragas.run_config import RunConfig
    from langchain_huggingface import HuggingFaceEmbeddings

    dataset = Dataset.from_list(
        [
            {
                "question": r["question"],
                "answer": r["answer"],
                "contexts": r["contexts"],
                "ground_truth": r["ground_truth"],
            }
            for r in per_item_results
        ]
    )

    llm = LangchainLLMWrapper(build_ragas_judge(judge_provider))
    # Same embedding model used for indexing (see ingestion/embedder.py) -- keeps the judge's
    # notion of semantic similarity consistent with what retrieval was actually scored on.
    embeddings = LangchainEmbeddingsWrapper(
        HuggingFaceEmbeddings(model_name="BAAI/bge-small-en-v1.5")
    )

    # Configure robust execution parameters to prevent timeouts and handle rate-limits
    run_config = RunConfig(
        timeout=180,
        max_retries=10,
        max_wait=60,
        max_workers=4,
    )

    return evaluate(
        dataset,
        metrics=[context_precision, context_recall, faithfulness, answer_relevancy],
        llm=llm,
        embeddings=embeddings,
        run_config=run_config,
        raise_exceptions=False,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default=str(REPO_ROOT / "data/eval/gold_questions.jsonl"))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "data/eval/results"))
    parser.add_argument(
        "--provider", choices=["openrouter", "huggingface", "gemini", "fake"], default="fake"
    )
    parser.add_argument("--url", default="http://localhost:6333")
    parser.add_argument("--local-path", default=None, help="Use file-based local Qdrant instead of --url")
    parser.add_argument("--fake-embedder", action="store_true", help="Offline plumbing test only")
    parser.add_argument("--fake-reranker", action="store_true", help="Offline plumbing test only")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument(
    "--sample-size", 
    "--limit", 
    type=int, 
    default=None, 
    dest="limit",
    help="Only score the first N gold items for quick testing")
    parser.add_argument("--skip-ragas", action="store_true", help="Skip RAGAS (retrieval metrics only)")
    parser.add_argument("--ragas-judge", choices=["openrouter", "gemini"], default="gemini")
    args = parser.parse_args()

    from embedder import FakeEmbedder, SentenceTransformerEmbedder
    from generator import get_generator
    from reranker import CrossEncoderReranker, FakeReranker
    from qdrant_client import QdrantClient

    embedder = FakeEmbedder() if args.fake_embedder else SentenceTransformerEmbedder()
    reranker = FakeReranker() if args.fake_reranker else CrossEncoderReranker()
    client = QdrantClient(path=args.local_path) if args.local_path else QdrantClient(url=args.url)
    generator = get_generator(args.provider)

    gold_items = load_gold(Path(args.gold), limit=args.limit)

    if args.limit:
        print(f"⚠️  Running in SAMPLE MODE: evaluation limited to {len(gold_items)} items.")

    print(f"Scoring {len(gold_items)} gold questions (provider={args.provider}, top_k={args.top_k})...")
    per_item_results = []
    for i, item in enumerate(gold_items, 1):
        print(f"  [{i}/{len(gold_items)}] {item['id']}: {item['question'][:80]}")
        per_item_results.append(run_item(item, client, embedder, reranker, generator, args.top_k))

    retrieval_summary = aggregate_retrieval_metrics(per_item_results)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "per_item_results.jsonl", "w", encoding="utf-8") as f:
        for r in per_item_results:
            f.write(json.dumps(r) + "\n")

    print("\n=== RETRIEVAL METRICS (offline, no LLM judge) ===")
    print(json.dumps(retrieval_summary, indent=2))

    ragas_summary = None
    if not args.skip_ragas:
        print(f"\nRunning RAGAS (judge={args.ragas_judge})... this calls a real LLM API for every item.")
        ragas_result = run_ragas(per_item_results, args.ragas_judge)
        
        # Safely extract scores to prevent KeyError if isolated items time out
        ragas_df = ragas_result.to_pandas()
        ragas_summary = ragas_df.mean(numeric_only=True).to_dict()

        print("\n=== RAGAS METRICS ===")
        print(json.dumps(ragas_summary, indent=2))
    else:
        print("\n(--skip-ragas set: not running context_precision/recall/faithfulness/answer_relevancy)")

    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump({"retrieval_metrics": retrieval_summary, "ragas_metrics": ragas_summary}, f, indent=2)

    print(f"\nWrote per-item results and summary to {out_dir}/")


if __name__ == "__main__":
    main()