"""
Phase 6 LIVE tests — these talk to your real Postgres (docker compose up -d must be running and
the ERP data loaded). They prove the parts the offline tests can't: that the executor really is
read-only at the DATABASE level, independent of the validator.

Run:
    python agent/test_sql_live.py

If you've applied erp/create_readonly_role.sql and set POSTGRES_RO_USER / POSTGRES_RO_PASSWORD in
.env, the extra role-level checks at the bottom run too; otherwise they are skipped.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "retrieval"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sql_tool import SQLToolError, execute_readonly, validate_sql  # noqa: E402

PASSED = 0


def check(label, condition):
    global PASSED
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label
    PASSED += 1


def raises(sql):
    """True if executing `sql` DIRECTLY (validator bypassed on purpose) is refused by the DB."""
    try:
        execute_readonly(sql)
        return False
    except SQLToolError:
        return True


def main():
    # 1. happy path through validator + executor
    safe = validate_sql("SELECT vendor_name, country FROM vendors ORDER BY vendor_id LIMIT 3")
    out = execute_readonly(safe)
    check("real query returns rows from your ERP data", len(out["rows"]) == 3 and out["columns"] == ["vendor_name", "country"])

    # 2. the canonical multi-hop building block from the README sanity check
    safe = validate_sql(
        "WITH s AS (SELECT vendor_id, SUM(amount) AS total_spend FROM purchase_orders "
        "WHERE status <> 'CANCELLED' GROUP BY vendor_id) "
        "SELECT v.vendor_name, c.contract_id, s.total_spend FROM vendors v "
        "JOIN contracts c ON c.vendor_id = v.vendor_id JOIN s ON s.vendor_id = v.vendor_id "
        "ORDER BY s.total_spend DESC LIMIT 5"
    )
    out = execute_readonly(safe)
    check("vendor -> contract_id -> spend join works", len(out["rows"]) > 0 and "contract_id" in out["columns"])

    # 3. row cap
    out = execute_readonly("SELECT po_id FROM purchase_orders", max_rows=5)
    check("row cap enforced + truncated flag set", len(out["rows"]) == 5 and out["truncated"] is True)

    # 4. DB-level read-only: these BYPASS the validator, to prove the second layer holds alone.
    check("DB refuses DELETE even if validator were bypassed", raises("DELETE FROM vendors"))
    check("DB refuses UPDATE even if validator were bypassed", raises("UPDATE vendors SET risk_tier = 'X'"))
    check("DB refuses DROP even if validator were bypassed", raises("DROP TABLE vendors"))
    check("DB refuses INSERT even if validator were bypassed", raises("INSERT INTO vendors(vendor_name, category, country) VALUES ('x','x','x')"))

    # 5. timeout
    check("statement_timeout kills a runaway query", raises("SELECT pg_sleep(30)"))

    # 6. data really unchanged after all of the above
    out = execute_readonly("SELECT COUNT(*) AS n FROM vendors")
    check("vendors table intact after the attacks", out["rows"][0]["n"] > 0)

    # 7. keyword search contract filter (needs Phase 2's contract_chunks to be loaded)
    from keyword_retriever import keyword_search

    one = execute_readonly("SELECT contract_id FROM contracts LIMIT 1")["rows"][0]["contract_id"]
    hits = keyword_search("agreement", top_k=50, contract_ids=[one])
    check("keyword_search(contract_ids=...) only returns that contract", all(h["contract_id"] == one for h in hits))

    # 8. optional: dedicated read-only role
    if os.getenv("POSTGRES_RO_USER"):
        out = execute_readonly("SELECT current_user AS u")  # direct call, validator bypassed on purpose
        check(f"connected as the read-only role ({out['rows'][0]['u']})", out["rows"][0]["u"] == os.getenv("POSTGRES_RO_USER"))
        check("RO role cannot read contract_chunks (not granted)", raises("SELECT * FROM contract_chunks LIMIT 1"))
        check("RO role cannot read cuad_documents (not granted)", raises("SELECT * FROM cuad_documents LIMIT 1"))
    else:
        print("[SKIP] POSTGRES_RO_USER not set -> role-level checks skipped (optional)")

    print(f"\nAll {PASSED} live checks passed.")


if __name__ == "__main__":
    main()
