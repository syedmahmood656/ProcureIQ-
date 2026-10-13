"""
Phase 9 — the SQL tool as an MCP server.

WHY MCP: the Phase 6 SQL tool was a Python function inside the agent process. As an MCP server it
becomes a standalone, reusable service: any MCP client (this agent, Claude Desktop, an IDE...) can
use the same vetted, read-only ERP access, and the security boundary moves out of the agent.

WHAT IT EXPOSES (deliberately small):
  get_schema()              -> the allow-listed tables/columns (so a client LLM can write SQL)
  run_readonly_sql(sql)     -> validate -> execute read-only -> {sql, columns, rows, truncated}

It does NOT expose Text-to-SQL. The LLM stays in the agent; this server only ever receives SQL and
never trusts it: it re-runs the full validator (agent/sql_tool.validate_sql) itself, then the same
READ ONLY / timeout / row-cap executor, then the DB role. A buggy or prompt-injected client
therefore gets exactly the same protection as before.

Run:
    python mcp_servers/sql_server.py                                   # stdio (spawned by a client)
    python mcp_servers/sql_server.py --transport http --port 8765      # streamable HTTP service

NOTE: in stdio mode stdout IS the protocol channel -- never print() in this file; log to stderr.
"""
import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent"))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from prompts import SCHEMA_DESCRIPTION  # noqa: E402
from sql_tool import MAX_ROWS, SQLToolError, execute_readonly, validate_sql  # noqa: E402

log = logging.getLogger("procureiq.mcp.sql")

MAX_SQL_CHARS = 4000  # a legitimate analytical query is far shorter; bounds parser work


def create_server(
    executor: Optional[Callable[[str], dict]] = None, host: str = "127.0.0.1", port: int = 8765
) -> FastMCP:
    """Build the server. `executor` is injectable so tests can run without Postgres."""
    run = executor or execute_readonly
    mcp = FastMCP("procureiq-sql", host=host, port=port)
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=read_only)
    def get_schema() -> str:
        """Describe the ERP tables and columns that run_readonly_sql may query (PostgreSQL)."""
        return SCHEMA_DESCRIPTION

    @mcp.tool(annotations=read_only)
    def run_readonly_sql(sql: str) -> dict[str, Any]:
        """Run ONE read-only SELECT over the allow-listed ERP tables and return the rows.

        Anything else (writes, DDL, several statements, other tables, system functions) is
        rejected with an explanatory error. Results are capped at 200 rows (`truncated` says so).
        """
        if len(sql) > MAX_SQL_CHARS:
            raise ToolError(f"SQL too long ({len(sql)} chars; max {MAX_SQL_CHARS})")
        try:
            safe_sql = validate_sql(sql)  # re-validate HERE: never trust the caller
            result = run(safe_sql)
        except SQLToolError as e:
            log.info("rejected/failed query: %s", str(e)[:200])
            raise ToolError(str(e))
        return {
            "sql": safe_sql,
            "columns": result["columns"],
            "rows": result["rows"][:MAX_ROWS],
            "truncated": bool(result["truncated"]),
        }

    return mcp


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("mcp").setLevel(logging.WARNING)  # drop per-request library chatter
    server = create_server(host=args.host, port=args.port)
    server.run(transport="stdio" if args.transport == "stdio" else "streamable-http")
