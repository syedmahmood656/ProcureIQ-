"""
Phase 2 — Clause-aware chunking.

Naive fixed-window chunking (e.g. "every 500 tokens") will happily cut a liability-cap clause
in half. Instead, we use CUAD's own expert-labeled clause spans (from Phase 1's clauses.jsonl)
as anchors:

  1. Group each contract's labeled clause spans, merge ones that touch/overlap.
  2. Expand each merged span outward to the nearest paragraph break (up to a cap) so the chunk
     has enough surrounding context to be useful on its own, not just a bare sentence fragment.
  3. Fill the *uncovered* gaps between clause chunks with fallback chunks (paragraph-packed,
     capped at ~800 chars) tagged category="GENERAL", so no part of the contract is silently
     dropped from the index.

Output: one JSONL file, one row per chunk, with contract_id/vendor linkage preserved so a
retrieved chunk can always be traced back to (a) the source contract file and (b) the vendor
row in Postgres for the SQL tool to join against later.

Usage:
    python ingestion/clause_chunker.py \
        --contracts-dir data/raw/cuad/contracts \
        --clauses-jsonl data/raw/cuad/clauses.jsonl \
        --out data/processed/chunks.jsonl
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

MAX_EXPAND = 400          # max chars to grow a clause span outward, each direction
FALLBACK_CHUNK_SIZE = 800  # target size for "GENERAL" filler chunks
PARA_BREAK = re.compile(r"\n\s*\n")


def load_clauses(clauses_path: str) -> dict:
    by_contract = defaultdict(list)
    with open(clauses_path, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            start = row["char_start"]
            end = start + len(row["clause_text"])
            by_contract[row["contract_id"]].append(
                {"category": row["category"], "start": start, "end": end}
            )
    return by_contract


def merge_spans(spans: list[dict], gap_tolerance: int = 50) -> list[dict]:
    """Merge overlapping/near-adjacent clause spans, keeping the union of categories."""
    spans = sorted(spans, key=lambda s: s["start"])
    merged = []
    for s in spans:
        if merged and s["start"] <= merged[-1]["end"] + gap_tolerance:
            merged[-1]["end"] = max(merged[-1]["end"], s["end"])
            merged[-1]["categories"].add(s["category"])
        else:
            merged.append({"start": s["start"], "end": s["end"], "categories": {s["category"]}})
    return merged


def expand_to_paragraph(text: str, start: int, end: int) -> tuple[int, int]:
    """Grow [start, end) outward to the nearest paragraph break, capped at MAX_EXPAND."""
    window_start = max(0, start - MAX_EXPAND)
    window_end = min(len(text), end + MAX_EXPAND)

    left_context = text[window_start:start]
    breaks = list(PARA_BREAK.finditer(left_context))
    new_start = window_start + breaks[-1].end() if breaks else window_start

    right_context = text[end:window_end]
    breaks = list(PARA_BREAK.finditer(right_context))
    new_end = end + breaks[0].start() if breaks else window_end

    return new_start, new_end


def pack_fallback_chunks(text: str, gap_start: int, gap_end: int) -> list[tuple[int, int]]:
    """Split an uncovered gap into ~FALLBACK_CHUNK_SIZE chunks on paragraph boundaries."""
    gap_text = text[gap_start:gap_end]
    if not gap_text.strip():
        return []

    paragraphs = [(m.start(), m.end()) for m in PARA_BREAK.finditer(gap_text)]
    bounds = [0] + [p[1] for p in paragraphs] + [len(gap_text)]

    chunks = []
    cur_start = 0
    cur_end = 0
    for i in range(1, len(bounds)):
        candidate_end = bounds[i]
        if candidate_end - cur_start > FALLBACK_CHUNK_SIZE and cur_end > cur_start:
            chunks.append((gap_start + cur_start, gap_start + cur_end))
            cur_start = cur_end
        cur_end = candidate_end
    if cur_end > cur_start:
        chunks.append((gap_start + cur_start, gap_start + cur_end))

    # Fixed-size fallback if a single "paragraph" is itself huge (some CUAD contracts have none)
    final = []
    for s, e in chunks:
        if e - s <= FALLBACK_CHUNK_SIZE * 1.5:
            final.append((s, e))
        else:
            pos = s
            while pos < e:
                final.append((pos, min(pos + FALLBACK_CHUNK_SIZE, e)))
                pos += FALLBACK_CHUNK_SIZE
    return final


def chunk_contract(contract_id: str, text: str, clause_spans: list[dict]) -> list[dict]:
    merged = merge_spans(clause_spans) if clause_spans else []

    clause_chunks = []
    for m in merged:
        s, e = expand_to_paragraph(text, m["start"], m["end"])
        clause_chunks.append({"start": s, "end": e, "categories": sorted(m["categories"])})

    # Re-merge in case expansion caused new overlaps
    clause_chunks.sort(key=lambda c: c["start"])
    dedup = []
    for c in clause_chunks:
        if dedup and c["start"] <= dedup[-1]["end"]:
            dedup[-1]["end"] = max(dedup[-1]["end"], c["end"])
            dedup[-1]["categories"] = sorted(set(dedup[-1]["categories"]) | set(c["categories"]))
        else:
            dedup.append(c)
    clause_chunks = dedup

    # Fill gaps (before first, between, after last) with GENERAL fallback chunks
    all_chunks = []
    cursor = 0
    for c in clause_chunks:
        if c["start"] > cursor:
            for gs, ge in pack_fallback_chunks(text, cursor, c["start"]):
                all_chunks.append({"start": gs, "end": ge, "categories": ["GENERAL"]})
        all_chunks.append(c)
        cursor = c["end"]
    if cursor < len(text):
        for gs, ge in pack_fallback_chunks(text, cursor, len(text)):
            all_chunks.append({"start": gs, "end": ge, "categories": ["GENERAL"]})

    results = []
    for i, c in enumerate(all_chunks):
        chunk_text = text[c["start"]:c["end"]].strip()
        if not chunk_text:
            continue
        results.append(
            {
                "contract_id": contract_id,
                "chunk_id": f"{contract_id}::chunk_{i:03d}",
                "categories": c["categories"],
                "char_start": c["start"],
                "char_end": c["end"],
                "text": chunk_text,
            }
        )
    return results


def main(contracts_dir: str, clauses_jsonl: str, out_path: str):
    clauses_by_contract = load_clauses(clauses_jsonl)
    contracts_dir = Path(contracts_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_chunks = 0
    general_chunks = 0
    lengths = []

    with open(out_path, "w", encoding="utf-8") as out_f:
        for txt_file in sorted(contracts_dir.glob("*.txt")):
            contract_id = txt_file.stem
            text = txt_file.read_text(encoding="utf-8")
            spans = clauses_by_contract.get(contract_id, [])
            chunks = chunk_contract(contract_id, text, spans)
            for ch in chunks:
                out_f.write(json.dumps(ch) + "\n")
                total_chunks += 1
                lengths.append(len(ch["text"]))
                if ch["categories"] == ["GENERAL"]:
                    general_chunks += 1

    n_contracts = len(list(contracts_dir.glob("*.txt")))
    print(f"Chunked {n_contracts} contracts -> {total_chunks} chunks")
    print(f"  clause-anchored chunks: {total_chunks - general_chunks}")
    print(f"  GENERAL fallback chunks: {general_chunks}")
    print(f"  avg chunk length: {sum(lengths) / len(lengths):.0f} chars")
    print(f"  avg chunks/contract: {total_chunks / n_contracts:.1f}")
    print(f"Output -> {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--contracts-dir", default="data/raw/cuad/contracts")
    parser.add_argument("--clauses-jsonl", default="data/raw/cuad/clauses.jsonl")
    parser.add_argument("--out", default="data/processed/chunks.jsonl")
    args = parser.parse_args()
    main(args.contracts_dir, args.clauses_jsonl, args.out)
