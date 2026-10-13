"""
Phase 1 — Synthetic ERP generator.

Creates realistic-looking (but clearly synthetic) vendor/PO/invoice data and links a subset
of vendors to real CUAD contracts by contract_id. This linkage is the whole point: it's what
lets a later query like "vendors with >$500K spend AND uncapped liability" require joining
structured spend data with unstructured contract clauses.

Writes CSVs to data/processed/erp/ — a separate load_erp_to_postgres.py reads these and
inserts them, keeping "generate" and "load" as separate, individually-testable steps.

Usage:
    python erp/generate_synthetic_erp.py --n-vendors 120 --seed 42
"""
import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

from faker import Faker

CATEGORIES = [
    "Cloud Infrastructure", "Logistics & Freight", "Raw Materials", "Professional Services",
    "Software & SaaS", "Facilities & Maintenance", "Marketing & Advertising", "IT Hardware",
    "Manufacturing Components", "Legal Services",
]
COUNTRIES = ["United States", "India", "United Kingdom", "Germany", "Singapore", "Canada"]


def load_cuad_contract_ids(metadata_csv: str) -> list[dict]:
    rows = []
    with open(metadata_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def main(n_vendors: int, seed: int, cuad_metadata: str, out_dir: str):
    random.seed(seed)
    fake = Faker()
    Faker.seed(seed)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cuad_contracts = load_cuad_contract_ids(cuad_metadata)
    random.shuffle(cuad_contracts)

    # Not every vendor needs a contract for the ERP data to make sense, but every vendor we
    # DO attach a contract to must map 1:1 to a real CUAD contract_id (never fabricate contract text).
    n_with_contracts = min(n_vendors, len(cuad_contracts))

    vendors = []
    contracts = []
    purchase_orders = []
    invoices = []

    po_id_counter = 1
    invoice_id_counter = 1

    for vendor_id in range(1, n_vendors + 1):
        vendor_name = fake.company()
        category = random.choice(CATEGORIES)
        country = random.choice(COUNTRIES)
        vendors.append(
            {
                "vendor_id": vendor_id,
                "vendor_name": vendor_name,
                "category": category,
                "country": country,
                "risk_tier": "UNKNOWN",
            }
        )

        # Attach a real CUAD contract to the first n_with_contracts vendors
        if vendor_id <= n_with_contracts:
            cuad_row = cuad_contracts[vendor_id - 1]
            effective = fake.date_between(start_date="-3y", end_date="-6m")
            # Expiration spread across past-to-near-future so some queries about
            # "expiring next quarter" have real hits once you run this data.
            expiration = effective + timedelta(days=random.choice([365, 730, 1095]))
            contracts.append(
                {
                    "contract_id": cuad_row["contract_id"],
                    "vendor_id": vendor_id,
                    "cuad_title": cuad_row["title"],
                    "source_file": cuad_row["source_file"],
                    "effective_date": effective.isoformat(),
                    "expiration_date": expiration.isoformat(),
                    "auto_renewal": random.choice([True, False]),
                }
            )

        # 3-15 POs per vendor across the last 2 years
        n_pos = random.randint(3, 15)
        for _ in range(n_pos):
            po_date = fake.date_between(start_date="-2y", end_date="today")
            amount = round(random.uniform(5_000, 250_000), 2)
            po_id = po_id_counter
            po_id_counter += 1
            purchase_orders.append(
                {
                    "po_id": po_id,
                    "vendor_id": vendor_id,
                    "po_date": po_date.isoformat(),
                    "amount": amount,
                    "currency": "USD",
                    "status": random.choice(["OPEN", "CLOSED", "CLOSED", "CLOSED"]),
                }
            )

            # 0-2 invoices per PO
            for _ in range(random.randint(0, 2)):
                invoice_date = po_date + timedelta(days=random.randint(1, 45))
                paid = random.random() < 0.75
                invoices.append(
                    {
                        "invoice_id": invoice_id_counter,
                        "po_id": po_id,
                        "vendor_id": vendor_id,
                        "invoice_date": invoice_date.isoformat(),
                        "amount": round(amount * random.uniform(0.9, 1.0), 2),
                        "paid_date": (invoice_date + timedelta(days=random.randint(5, 60))).isoformat()
                        if paid
                        else "",
                        "status": "PAID" if paid else random.choice(["UNPAID", "OVERDUE"]),
                    }
                )
                invoice_id_counter += 1

    _write_csv(out_dir / "vendors.csv", vendors)
    _write_csv(out_dir / "contracts.csv", contracts)
    _write_csv(out_dir / "purchase_orders.csv", purchase_orders)
    _write_csv(out_dir / "invoices.csv", invoices)

    print(f"Generated {len(vendors)} vendors ({n_with_contracts} linked to real CUAD contracts)")
    print(f"Generated {len(purchase_orders)} purchase orders, {len(invoices)} invoices")
    print(f"CSVs written to {out_dir}/")


def _write_csv(path: Path, rows: list[dict]):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-vendors", type=int, default=120)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cuad-metadata", default="data/raw/cuad/contracts_metadata.csv")
    parser.add_argument("--out", default="data/processed/erp")
    args = parser.parse_args()
    main(args.n_vendors, args.seed, args.cuad_metadata, args.out)
