"""
Phase 3 — keyword (full-text) retrieval against Postgres's `contract_chunks.search_vector`
(built in Phase 2).

Uses `websearch_to_tsquery` rather than `plainto_tsquery` or a hand-built `to_tsquery` —
it's the variant designed for raw natural-language input: it tolerates stray punctuation,
supports quoted phrases and `-exclude` terms, and never raises a syntax error on messy input
the way `to_tsquery` can.

Usage (standalone test):
    python retrieval/keyword_retriever.py "what is the cap on liability"
"""
import argparse
import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "procureiq")
DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")


def keyword_search(query: str, top_k: int = 20, contract_ids: list[str] | None = None) -> list[dict]:
    """`contract_ids` (Phase 6): if given, only chunks from those contracts can match."""
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    cur = conn.cursor()
    contract_filter_sql = "AND contract_id = ANY(%s)" if contract_ids else ""
    params = [query, query] + ([list(contract_ids)] if contract_ids else []) + [top_k]
    cur.execute(
        f"""
        SELECT chunk_id, contract_id, categories, chunk_text,
               ts_rank(search_vector, websearch_to_tsquery('english', %s)) AS score
        FROM contract_chunks
        WHERE search_vector @@ websearch_to_tsquery('english', %s)
          {contract_filter_sql}
        ORDER BY score DESC
        LIMIT %s
        """,
        params,
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"chunk_id": r[0], "contract_id": r[1], "categories": r[2], "text": r[3], "score": float(r[4])}
        for r in rows
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    results = keyword_search(args.query, top_k=args.top_k)
    print(f"{len(results)} keyword matches for: {args.query!r}\n")
    for i, r in enumerate(results, 1):
        print(f"{i}. [score={r['score']:.4f}] {r['chunk_id']} {r['categories']}")
        print("   ", r["text"][:120].replace("\n", " "))
