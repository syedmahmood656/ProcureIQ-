"""
Phase 2 — load chunks.jsonl into Postgres `contract_chunks` (the keyword-search half of
hybrid retrieval; the same chunks also go into Qdrant via build_qdrant_index.py for the
dense-vector half).

Run after ingestion/load_cuad_documents.py (contract_chunks FKs to cuad_documents).

Usage:
    python ingestion/load_fulltext_postgres.py
"""
import json
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "procureiq")
DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")

BATCH_SIZE = 1000


def main(chunks_path: str):
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    cur = conn.cursor()

    with open(chunks_path, encoding="utf-8") as f:
        chunks = [json.loads(line) for line in f]

    rows = [
        (c["chunk_id"], c["contract_id"], c["categories"], c["char_start"], c["char_end"], c["text"])
        for c in chunks
    ]

    sql = (
        "INSERT INTO contract_chunks (chunk_id, contract_id, categories, char_start, char_end, chunk_text) "
        "VALUES %s ON CONFLICT (chunk_id) DO NOTHING"
    )
    psycopg2.extras.execute_values(cur, sql, rows, page_size=BATCH_SIZE)
    conn.commit()

    print(f"Processed {len(rows)} chunks (existing chunk_ids skipped) -> contract_chunks now has:")
    cur.execute("SELECT count(*) FROM contract_chunks")
    print(f"  {cur.fetchone()[0]} total rows")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main(str(Path("data/processed/chunks.jsonl")))
