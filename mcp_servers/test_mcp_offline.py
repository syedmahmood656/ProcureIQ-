"""
Phase 9 offline tests — no Postgres, no Qdrant, no models, no network.

  1. Protocol (in-memory MCP session): tools are listed, read-only annotated, valid SQL runs,
     every dangerous shape is rejected BEFORE the executor is touched.
  2. Real stdio child process via MCPSQLExecutor: rejection messages cross the process boundary,
     a DB failure becomes SQLToolError (not a hang), concurrent threads share one session, and the
     client reconnects after close().
  3. The whole LangGraph agent with its SQL executed through MCP (sql route and both route).
  4. Config: PROCUREIQ_SQL_BACKEND handling.

Run:   python mcp_servers/test_mcp_offline.py
"""
import asyncio
import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (ROOT, ROOT / "agent", ROOT / "retrieval", ROOT / "ingestion"):
    sys.path.insert(0, str(p))

# Deterministic: a child that DOES reach its DB layer must fail to connect, even if you have
# Postgres running locally. (Only POSTGRES_* vars are forwarded to the child.)
os.environ["POSTGRES_HOST"] = "127.0.0.1"
os.environ["POSTGRES_PORT"] = "1"

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from graph import run_agent  # noqa: E402
from mcp_client import MCPSQLExecutor, get_sql_executor  # noqa: E402
from mcp_servers.sql_server import create_server  # noqa: E402
from sql_tool import SQLToolError  # noqa: E402
from test_phase6_offline import (  # noqa: E402
    ROWS_BOTH, VALID_SQL_BOTH, Recorder, ScriptedGenerator, _chunks,
)

N = 0


def check(label, cond):
    global N
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label
    N += 1


CANNED = {"columns": ["vendor_name"], "rows": [{"vendor_name": "Acme"}], "truncated": False}

DANGEROUS = [
    "DROP TABLE vendors",
    "DELETE FROM vendors",
    "SELECT 1; DROP TABLE vendors",
    "SELECT * FROM pg_shadow",
    "SELECT * FROM contract_chunks",
    "SELECT pg_sleep(30)",
    "WITH x AS (DELETE FROM vendors RETURNING *) SELECT * FROM x",
    "SELECT * FROM information_schema.tables",
    "",
]


# ---------------------------------------------------------------- 1. protocol, in memory
async def _protocol_tests():
    calls = []

    def fake_exec(sql):
        calls.append(sql)
        return CANNED

    server = create_server(executor=fake_exec)
    async with create_connected_server_and_client_session(server._mcp_server) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        check("server exposes exactly get_schema + run_readonly_sql", set(tools) == {"get_schema", "run_readonly_sql"})
        check("both tools annotated read-only", all(t.annotations and t.annotations.readOnlyHint for t in tools.values()))

        schema = await s.call_tool("get_schema", {})
        check("get_schema lists the ERP tables", "vendors(" in schema.content[0].text and "purchase_orders(" in schema.content[0].text)

        ok = await s.call_tool("run_readonly_sql", {"sql": "SELECT vendor_name FROM vendors;"})
        check("valid SQL succeeds", not ok.isError and ok.structuredContent["rows"] == CANNED["rows"])
        check("executor received the validator's normalised SQL", calls == ["SELECT vendor_name FROM vendors"])

        calls.clear()
        for q in DANGEROUS:
            r = await s.call_tool("run_readonly_sql", {"sql": q})
            check(f"rejected as isError: {q[:45]!r}", r.isError)
        check("executor NEVER ran for any rejected query", calls == [])

        big = await s.call_tool("run_readonly_sql", {"sql": "SELECT vendor_name FROM vendors WHERE vendor_name = '" + "a" * 5000 + "'"})
        check("oversized SQL rejected", big.isError and "too long" in big.content[0].text)

    def boom(sql):
        raise SQLToolError('database error: column "x" does not exist')

    async with create_connected_server_and_client_session(create_server(executor=boom)._mcp_server) as s:
        r = await s.call_tool("run_readonly_sql", {"sql": "SELECT vendor_name FROM vendors"})
        check("DB error text reaches the client (the model needs it to repair)", r.isError and 'column "x" does not exist' in r.content[0].text)

    big_rows = {"columns": ["n"], "rows": [{"n": i} for i in range(500)], "truncated": True}
    async with create_connected_server_and_client_session(create_server(executor=lambda s: big_rows)._mcp_server) as s:
        r = await s.call_tool("run_readonly_sql", {"sql": "SELECT vendor_id AS n FROM vendors"})
        check("server enforces the 200-row cap itself", len(r.structuredContent["rows"]) == 200 and r.structuredContent["truncated"])


# ---------------------------------------------------------------- 2. real child process
def _stdio_tests():
    ex = MCPSQLExecutor(timeout_s=30)
    try:
        for q in ("DROP TABLE vendors", "SELECT * FROM pg_shadow"):
            try:
                ex(q)
                check(f"child rejects {q!r}", False)
            except SQLToolError as e:
                msg = str(e)
                check(f"child rejects {q!r} with a clean message", len(msg) > 0 and "Error executing tool" not in msg)

        try:
            ex("SELECT vendor_name FROM vendors")
            check("unreachable DB -> SQLToolError", False)
        except SQLToolError as e:
            check("unreachable DB surfaces as SQLToolError('database error')", "database error" in str(e))

        check("schema fetched through the child", "vendors(" in ex.get_schema())
        ex.ping()
        check("ping succeeds", True)

        errors, done = [], []

        def worker(i):
            try:
                ex("DELETE FROM vendors")
            except SQLToolError:
                done.append(i)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        check("8 concurrent threads share one session without error", len(done) == 8 and not errors)

        ex.close()
        try:
            ex("DROP TABLE vendors")
        except SQLToolError as e:
            check("client reconnects transparently after close()", "SQL service unavailable" not in str(e))
    finally:
        ex.close()

    bad = MCPSQLExecutor(command=[sys.executable, "-c", "import sys; sys.exit(3)"], timeout_s=10)
    try:
        bad("SELECT 1")
        check("dead server -> SQLToolError", False)
    except SQLToolError as e:
        check("dead server -> SQLToolError('SQL service unavailable')", "unavailable" in str(e))
    finally:
        bad.close()


# ---------------------------------------------------------------- 3. whole graph through MCP
FAKE_SERVER_CODE = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from mcp_servers.sql_server import create_server
ROWS = {ROWS_BOTH!r}
create_server(executor=lambda sql: ROWS).run(transport="stdio")
"""


def _graph_tests():
    ex = MCPSQLExecutor(command=[sys.executable, "-c", FAKE_SERVER_CODE], timeout_s=30)
    try:
        rec = Recorder(_chunks("0001_ACME_AGREEMENT"))
        gen = ScriptedGenerator(sql_replies=["SELECT vendor_name FROM vendors ORDER BY 1"])
        out = run_agent("Who are the top 5 vendors by total spend?", None, None, None, gen, sql_executor=ex, retrieve_fn=rec)
        check("graph sql route via MCP: accepted, SQL went over MCP", out["route"] == "sql" and out["verdict"] == "accept" and out["sql_rows"] != [])
        check("graph sql route via MCP: report built from MCP rows", out["report"]["confidence"] != "" and "vendors" in out["sql_query"])

        rec = Recorder(_chunks("0001_ACME_AGREEMENT", "0002_GLOBEX_AGREEMENT"))
        gen = ScriptedGenerator(sql_replies=[VALID_SQL_BOTH])
        q = "Which vendors with total purchase-order spend above $1,000,000 have contracts that include an uncapped liability clause?"
        out = run_agent(q, None, None, None, gen, sql_executor=ex, retrieve_fn=rec, category_fn=False)
        check("graph both route via MCP: contract_ids from MCP rows narrowed the search",
              out["route"] == "both" and rec.calls and set(rec.calls[0].get("contract_ids", [])) == {"0001_ACME_AGREEMENT", "0002_GLOBEX_AGREEMENT"})

        gen = ScriptedGenerator(sql_replies=["DROP TABLE vendors"] * 5)
        out = run_agent("Who are the top 5 vendors by total spend?", None, None, None, gen, sql_executor=ex, retrieve_fn=Recorder([]), max_retries=1)
        check("model emitting DROP is stopped, run ends in give_up (never executes)", out["verdict"] == "give_up")
    finally:
        ex.close()


# ---------------------------------------------------------------- 4. config
def _config_tests():
    old = {k: os.environ.get(k) for k in ("PROCUREIQ_SQL_BACKEND", "PROCUREIQ_MCP_SQL_URL")}
    try:
        os.environ.pop("PROCUREIQ_SQL_BACKEND", None)
        check("default backend is local (None executor)", get_sql_executor() is None)
        os.environ["PROCUREIQ_SQL_BACKEND"] = "mcp"
        e = get_sql_executor()
        check("backend=mcp -> MCPSQLExecutor (stdio)", isinstance(e, MCPSQLExecutor) and e.url is None)
        os.environ["PROCUREIQ_MCP_SQL_URL"] = "http://localhost:8765/mcp"
        check("URL env -> HTTP transport", get_sql_executor().url == "http://localhost:8765/mcp")
        os.environ["PROCUREIQ_SQL_BACKEND"] = "bogus"
        try:
            get_sql_executor()
            check("bogus backend rejected", False)
        except ValueError:
            check("bogus backend rejected", True)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


if __name__ == "__main__":
    asyncio.run(_protocol_tests())
    _stdio_tests()
    _graph_tests()
    _config_tests()
    print(f"\nAll {N} checks passed.")
