"""
Phase 7 — audit your real contract corpus with the guardrail patterns.

Why: guardrails.py is tuned for HIGH PRECISION (don't delete genuine clauses). Whether it
actually is, on YOUR 42K CUAD chunks, is an empirical question. This script scans every row of
contract_chunks (read-only) and reports, per pattern, how many chunks fire plus example snippets,
so you can eyeball whether each hit is a real attack or a false positive.

Interpretation:
  * CUAD is a corpus of real, public SEC-filed contracts, so GENUINE injection attempts should be
    ~zero. Every hit is therefore most likely a FALSE POSITIVE worth looking at -- if a pattern
    fires often on legitimate legal language, tell me which one and its snippets and I'll tighten it.
  * Zero hits is the expected, healthy result.

Usage:
    python agent/scan_corpus_for_injection.py              # summary + up to 5 examples per pattern
    python agent/scan_corpus_for_injection.py --examples 15
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guardrails import iter_matches  # noqa: E402

load_dotenv()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--examples", type=int, default=5, help="Max example snippets per pattern")
    args = parser.parse_args()

    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"), port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "procureiq"), user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", "postgres"),
    )
    conn.set_session(readonly=True)
    cur = conn.cursor(name="scan_cursor")  # server-side cursor: streams, never loads 42K rows at once
    cur.itersize = 2000
    cur.execute("SELECT chunk_id, chunk_text FROM contract_chunks")

    total, flagged_chunks = 0, 0
    per_pattern = Counter()
    examples = defaultdict(list)
    for chunk_id, text in cur:
        total += 1
        hit = False
        for name, snippet in iter_matches(text or ""):
            hit = True
            per_pattern[name] += 1
            if len(examples[name]) < args.examples:
                examples[name].append((chunk_id, snippet))
        flagged_chunks += hit
    conn.close()

    print(f"Scanned {total} chunk(s); {flagged_chunks} contain at least one pattern hit "
          f"({(100 * flagged_chunks / total):.3f}% of the corpus)." if total else "No chunks found.")
    if not per_pattern:
        print("No hits. This is the expected result for a corpus of real SEC-filed contracts.")
        return
    print("\nHits per pattern:")
    for name, n in per_pattern.most_common():
        print(f"  {name:<24}{n}")
        for chunk_id, snippet in examples[name]:
            print(f"      [{chunk_id}] ...{snippet}...")
    print("\nReview each snippet: a genuine attack, or ordinary legal language caught by mistake?")


if __name__ == "__main__":
    main()
