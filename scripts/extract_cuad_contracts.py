"""
Phase 1 — CUAD extraction.

Reads the raw CUADv1.json (SQuAD-style: one 'paragraph' per contract, with a full-text
'context' and a list of clause-category QA spans) and writes:

  data/raw/cuad/contracts/<contract_id>.txt        -- full contract text, one file per contract
  data/raw/cuad/contracts_metadata.csv             -- contract_id, title, char_len, num_clauses
  data/raw/cuad/clauses.jsonl                       -- one row per labeled clause span

We deliberately do NOT chunk or embed here — that's Phase 2's job (clause-aware chunking is a
distinct, more careful step). This phase's only job is: get CUAD into a clean, inspectable,
per-contract form on disk.

Usage:
    python scripts/extract_cuad_contracts.py --source data_source_cuad/data/CUADv1.json
"""
import argparse
import csv
import json
import re
from pathlib import Path


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title).strip("_")
    return slug[:120]


def main(source_path: str, out_dir: str):
    out_dir = Path(out_dir)
    contracts_dir = out_dir / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)

    with open(source_path, "r", encoding="utf-8") as f:
        cuad = json.load(f)

    metadata_rows = []
    clause_rows = []

    for i, entry in enumerate(cuad["data"]):
        title = entry["title"]
        contract_id = f"{i:04d}_{slugify(title)}"
        paragraph = entry["paragraphs"][0]
        context = paragraph["context"]

        # Write full contract text
        txt_path = contracts_dir / f"{contract_id}.txt"
        txt_path.write_text(context, encoding="utf-8")

        num_clauses_found = 0
        for qa in paragraph["qas"]:
            if qa.get("is_impossible", True):
                continue  # no answer for this clause category in this contract
            category = qa["question"].split('related to "')[1].split('"')[0]
            for ans in qa["answers"]:
                clause_rows.append(
                    {
                        "contract_id": contract_id,
                        "category": category,
                        "clause_text": ans["text"],
                        "char_start": ans["answer_start"],
                    }
                )
                num_clauses_found += 1

        metadata_rows.append(
            {
                "contract_id": contract_id,
                "title": title,
                "source_file": f"contracts/{contract_id}.txt",
                "char_len": len(context),
                "num_labeled_clauses": num_clauses_found,
            }
        )

    # Write metadata CSV
    meta_path = out_dir / "contracts_metadata.csv"
    with open(meta_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(metadata_rows[0].keys()))
        writer.writeheader()
        writer.writerows(metadata_rows)

    # Write clause spans as JSONL (easy to stream later for chunking metadata)
    clauses_path = out_dir / "clauses.jsonl"
    with open(clauses_path, "w", encoding="utf-8") as f:
        for row in clause_rows:
            f.write(json.dumps(row) + "\n")

    print(f"Extracted {len(metadata_rows)} contracts -> {contracts_dir}")
    print(f"Metadata written -> {meta_path}")
    print(f"Wrote {len(clause_rows)} labeled clause spans -> {clauses_path}")
    print(f"Total corpus size: {sum(r['char_len'] for r in metadata_rows):,} characters")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data_source_cuad/data/CUADv1.json")
    parser.add_argument("--out", default="data/raw/cuad")
    args = parser.parse_args()
    main(args.source, args.out)
