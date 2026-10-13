"""
Phase 1 — load the generated CSVs into Postgres, in FK-safe order:
vendors -> contracts -> purchase_orders -> invoices.

Reads connection settings from environment variables (see .env.example), so this script
works unchanged whether Postgres is running locally, in Docker, or in the cloud later.

Usage:
    python erp/load_erp_to_postgres.py
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

DATA_DIR = Path("data/processed/erp")


def get_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )


def load_csv(cur, table: str, csv_path: Path, columns: list[str]):
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    placeholders = ", ".join(["%s"] * len(columns))
    col_list = ", ".join(columns)
    # ON CONFLICT DO NOTHING (no target needed) makes this safe to re-run against data that's
    # already loaded -- e.g. after re-running load_cuad_documents.py -- instead of throwing a
    # duplicate-key error on the primary key.
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

    values = []
    for row in rows:
        record = []
        for col in columns:
            v = row.get(col, "")
            record.append(None if v == "" else v)
        values.append(tuple(record))

    cur.executemany(sql, values)
    cur.execute(f"SELECT count(*) FROM {table}")
    print(f"  processed {len(values)} rows from CSV (existing rows skipped) -> {table} now has {cur.fetchone()[0]} rows")


def main():
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("Loading vendors...")
        load_csv(cur, "vendors", DATA_DIR / "vendors.csv",
                  ["vendor_id", "vendor_name", "category", "country", "risk_tier"])
        # Reset the SERIAL sequence since we're inserting explicit IDs
        cur.execute("SELECT setval('vendors_vendor_id_seq', (SELECT MAX(vendor_id) FROM vendors))")

        print("Loading contracts...")
        load_csv(cur, "contracts", DATA_DIR / "contracts.csv",
                  ["contract_id", "vendor_id", "cuad_title", "source_file",
                   "effective_date", "expiration_date", "auto_renewal"])

        print("Loading purchase_orders...")
        load_csv(cur, "purchase_orders", DATA_DIR / "purchase_orders.csv",
                  ["po_id", "vendor_id", "po_date", "amount", "currency", "status"])
        cur.execute("SELECT setval('purchase_orders_po_id_seq', (SELECT MAX(po_id) FROM purchase_orders))")

        print("Loading invoices...")
        load_csv(cur, "invoices", DATA_DIR / "invoices.csv",
                  ["invoice_id", "po_id", "vendor_id", "invoice_date", "amount", "paid_date", "status"])
        cur.execute("SELECT setval('invoices_invoice_id_seq', (SELECT MAX(invoice_id) FROM invoices))")

        conn.commit()
        print("All tables loaded successfully.")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
