"""
Phase 10 — screen every vendor in the ERP against the loaded OFAC list.

    python risk/screen_vendors.py                       # screen all vendors, write results
    python risk/screen_vendors.py --dry-run             # show what would change, write nothing
    python risk/screen_vendors.py --name "Some Company Ltd"   # ad-hoc check of ONE name, writes nothing
    python risk/screen_vendors.py --include-individuals # also compare against people on the list

Writes (one transaction):
    vendor_risk_screening   latest result per vendor (status, matched name, score, program, list version)
    vendors.risk_tier       'SANCTIONS_REVIEW' (status REVIEW) | 'CLEAR' (WEAK_MATCH and CLEAR)
                            -> after this, the existing SQL route can answer risk questions.

A REVIEW is a NAME-SIMILARITY flag for a human to check, NOT a finding that the vendor is sanctioned.
Re-run after every ofac_loader.py refresh and after the vendor list changes.
"""
import argparse
import sys
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from matcher import SanctionsIndex  # noqa: E402
from ofac_loader import connect  # noqa: E402

TIER_FOR_STATUS = {"REVIEW": "SANCTIONS_REVIEW", "WEAK_MATCH": "CLEAR", "CLEAR": "CLEAR"}


def load_index(conn, include_individuals: bool):
    with conn.cursor() as cur:
        cur.execute("SELECT loaded_at FROM sanctions_list_info WHERE id = 1")
        info = cur.fetchone()
        if info is None:
            sys.exit("No sanctions list loaded yet. Run: python risk/ofac_loader.py")
        cur.execute("SELECT ent_num, name, entry_type, program, source FROM sanctions_entries")
        entries = [dict(zip(("ent_num", "name", "entry_type", "program", "source"), r)) for r in cur.fetchall()]
    return SanctionsIndex(entries, include_individuals=include_individuals), info[0]


def screen_all(conn, index: SanctionsIndex, loaded_at):
    with conn.cursor() as cur:
        cur.execute("SELECT vendor_id, vendor_name FROM vendors ORDER BY vendor_id")
        vendors = cur.fetchall()
    results = []
    for vendor_id, vendor_name in vendors:
        r = index.screen(vendor_name)
        results.append({"vendor_id": vendor_id, "vendor_name": vendor_name, "loaded_at": loaded_at, **r})
    return results


def write_results(conn, results) -> None:
    rows = []
    for r in results:
        m = r["match"] or {}
        # CLEAR rows keep no match fields; WEAK/REVIEW keep the best candidate for transparency.
        rows.append((r["vendor_id"], r["status"], m.get("matched_name"), m.get("matched_on"), m.get("score"),
                     m.get("ent_num"), m.get("entry_type"), m.get("program"), r["loaded_at"]))
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO vendor_risk_screening
                   (vendor_id, status, best_match_name, matched_on, match_score, ofac_ent_num, ofac_entry_type, ofac_program, list_loaded_at)
                   VALUES %s
                   ON CONFLICT (vendor_id) DO UPDATE SET status = EXCLUDED.status, best_match_name = EXCLUDED.best_match_name,
                     matched_on = EXCLUDED.matched_on, match_score = EXCLUDED.match_score, ofac_ent_num = EXCLUDED.ofac_ent_num,
                     ofac_entry_type = EXCLUDED.ofac_entry_type, ofac_program = EXCLUDED.ofac_program,
                     list_loaded_at = EXCLUDED.list_loaded_at, screened_at = now()""",
                rows,
            )
            psycopg2.extras.execute_batch(
                cur, "UPDATE vendors SET risk_tier = %s WHERE vendor_id = %s",
                [(TIER_FOR_STATUS[r["status"]], r["vendor_id"]) for r in results],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="screen one name; writes nothing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-individuals", action="store_true")
    args = ap.parse_args()

    conn = connect()
    try:
        index, loaded_at = load_index(conn, args.include_individuals)
        print(f"Index: {len(index):,} searchable names (list loaded {loaded_at:%Y-%m-%d %H:%M})")

        if args.name:
            matches = index.search(args.name, limit=5)
            print(f"\nCandidates for {args.name!r}:" if matches else f"\nNo candidates for {args.name!r} (CLEAR).")
            for m in matches:
                print(f"  {m['score']:.3f}  {m['matched_name']}  [{m['matched_on']}, ent {m['ent_num']}, {m['program']}]")
            sys.exit(0)

        results = screen_all(conn, index, loaded_at)
        counts = {s: sum(1 for r in results if r["status"] == s) for s in ("REVIEW", "WEAK_MATCH", "CLEAR")}
        print(f"Screened {len(results)} vendors: {counts}")
        for r in sorted(results, key=lambda r: -(r["match"] or {}).get("score", 0)):
            if r["match"]:
                m = r["match"]
                print(f"  {r['status']:<10} {m['score']:.3f}  {r['vendor_name']!r}  ~  {m['matched_name']!r} [{m['matched_on']}, {m['program']}]")
        if args.dry_run:
            print("\n--dry-run: nothing written.")
        else:
            write_results(conn, results)
            print("\nWrote vendor_risk_screening and updated vendors.risk_tier.")
    finally:
        conn.close()
