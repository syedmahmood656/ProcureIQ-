"""
Phase 2 — load ALL 510 CUAD contracts into cuad_documents.

Ordering matters: this must run BEFORE erp/load_erp_to_postgres.py, since `contracts`
now has a foreign key to `cuad_documents`. See README.md's updated Phase 2 run order.

Usage:
    python ingestion/load_cuad_documents.py
"""
import csv
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "procureiq")
DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")


def main(metadata_csv: str):
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    cur = conn.cursor()

    with open(metadata_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    values = [
        (r["contract_id"], r["title"], r["source_file"], int(r["char_len"]), int(r["num_labeled_clauses"]))
        for r in rows
    ]
    cur.executemany(
        "INSERT INTO cuad_documents (contract_id, cuad_title, source_file, char_len, num_labeled_clauses) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (contract_id) DO NOTHING",
        values,
    )
    conn.commit()
    cur.execute("SELECT count(*) FROM cuad_documents")
    print(f"Processed {len(values)} rows (existing contract_ids skipped) -> cuad_documents now has {cur.fetchone()[0]} rows")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main(str(Path("data/raw/cuad/contracts_metadata.csv")))
