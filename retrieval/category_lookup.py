"""
Phase 6 — exact clause-category lookup for the agent's "both" (multi-hop) route.

Every chunk in contract_chunks carries CUAD's expert labels in `categories` (e.g. 'Uncapped
Liability'). When the SQL step has narrowed the question to N candidate contracts and the
question asks about a known clause type, the most reliable way to answer "which of these have
clause X?" is NOT a similarity search that hopes the right chunk ranks in the top few -- it is an
exact lookup of which of the N contracts have a chunk labelled X. That checks ALL candidates,
not just the ones that happen to rank well.

Usage (standalone):
    python retrieval/category_lookup.py "Uncapped Liability" 0001_X 0002_Y
"""
import argparse
import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def chunks_with_category(category: str, contract_ids: list[str]) -> list[dict]:
    """
    For each contract in `contract_ids` that has at least one chunk labelled `category`
    (case-insensitive), return its first such chunk. At most one row per contract.
    """
    if not contract_ids:
        return []
    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "procureiq"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", "postgres"),
    )
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT ON (contract_id) chunk_id, contract_id, categories, chunk_text
            FROM contract_chunks
            WHERE contract_id = ANY(%s)
              AND EXISTS (SELECT 1 FROM unnest(categories) AS c WHERE lower(c) = lower(%s))
            ORDER BY contract_id, char_start
            """,
            (list(contract_ids), category),
        )
        return [
            {"chunk_id": r[0], "contract_id": r[1], "categories": r[2], "text": r[3]}
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("category")
    parser.add_argument("contract_ids", nargs="+")
    args = parser.parse_args()
    for c in chunks_with_category(args.category, args.contract_ids):
        print(c["contract_id"], c["categories"], "|", c["text"][:100].replace("\n", " "))
