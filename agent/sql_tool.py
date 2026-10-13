"""
Phase 6 — the SQL tool ("The Accountant"): natural-language question -> safe, read-only SQL ->
exact numbers from the ERP tables.

Safety is layered, because an LLM writing SQL against a real database is the one place in this
project where a model's mistake (or a prompt-injected instruction) could do real damage:

  Layer 1 - PROMPT:     the model is told the schema and "SELECT only" (prompts.py). Advisory only;
                        never rely on this layer by itself.
  Layer 2 - VALIDATOR:  validate_sql() parses the model's output with sqlglot into an AST and
                        rejects anything that isn't exactly ONE plain SELECT over an allow-listed
                        set of tables. What gets executed is the validator's own re-rendering of
                        the AST, NOT the raw model text -- so there is no gap between "what we
                        checked" and "what ran" (comment tricks, odd whitespace, etc. vanish).
  Layer 3 - EXECUTOR:   execute_readonly() runs inside a READ ONLY transaction with a statement
                        timeout and a hard row cap.
  Layer 4 - DB ROLE:    optional but recommended -- connect as a role that only has SELECT on the
                        allow-listed tables (erp/create_readonly_role.sql). Even if layers 2 and 3
                        both had a bug, the database itself refuses to write.

Usage (standalone):
    python agent/sql_tool.py --validate "SELECT vendor_name FROM vendors"
    python agent/sql_tool.py --validate "DROP TABLE vendors"          # -> REJECTED
    python agent/sql_tool.py --run "SELECT vendor_name, country FROM vendors LIMIT 5"
"""
import argparse
import datetime
import decimal
import logging
import os
import re
from typing import Optional

import sqlglot
from dotenv import load_dotenv
from sqlglot import exp

load_dotenv()

# sqlglot logs a WARNING for syntax it can't fully parse (e.g. 'SET ROLE ...'); the validator
# rejects those statements anyway, so the log line is just noise on the console.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------

# Only structured ERP data. contract_chunks (huge text) and cuad_documents (bookkeeping) are
# deliberately excluded: contract *text* is the RAG tool's job, not the SQL tool's.
 
ALLOWED_TABLES = {
    "vendors", "contracts", "purchase_orders", "invoices", "vendor_annual_spend",
    "vendor_risk_screening",  # Phase 10
}

MAX_ROWS = 200                 # hard cap on rows pulled back from the DB per query
STATEMENT_TIMEOUT_MS = 5000    # a runaway query is killed by Postgres after 5 seconds

# Anything starting with one of these prefixes, or equal to one of these names, is rejected.
# pg_* covers pg_sleep / pg_read_file / pg_ls_dir / pg_terminate_backend etc. The *_to_xml family
# can execute an arbitrary query given as a *string*, which would sidestep table allow-listing.
_BLOCKED_FUNC_PREFIXES = (
    "pg_", "lo_", "dblink", "query_to_", "table_to_", "cursor_to_", "schema_to_", "database_to_",
)
_BLOCKED_FUNC_NAMES = {
    "set_config", "current_setting", "version", "current_user", "session_user", "current_database",
    "current_schema", "inet_server_addr", "inet_client_addr", "txid_current",
    # sqlglot's internal spellings of the same functions (see _function_name_candidates)
    "currentversion", "currentuser", "sessionuser", "currentdatabase", "currentschema",
    "current_version",
}

# Node types that must never appear ANYWHERE in the tree (this also catches data-modifying
# statements hidden inside a CTE, e.g. WITH x AS (DELETE ... RETURNING *) SELECT ...).
_FORBIDDEN_NODE_NAMES = [
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command", "Merge", "TruncateTable",
    "Copy", "Set", "Use", "Into", "Lock", "Transaction", "Commit", "Rollback", "Grant", "Declare",
    "Pragma", "Analyze", "Kill", "Show", "Describe",
]
_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if hasattr(exp, n))
_SETOP = getattr(exp, "SetOperation", exp.Union)  # UNION / INTERSECT / EXCEPT base class

# What the Text-to-SQL prompt is allowed to fall back to when running with --provider fake
# (the fake generator can't write SQL, so offline runs use this canned, valid query instead).
FAKE_SQL = (
    "SELECT v.vendor_name, c.contract_id, s.total_spend "
    "FROM vendors v "
    "JOIN contracts c ON c.vendor_id = v.vendor_id "
    "JOIN (SELECT vendor_id, SUM(amount) AS total_spend FROM purchase_orders "
    "      WHERE status <> 'CANCELLED' GROUP BY vendor_id) s ON s.vendor_id = v.vendor_id "
    "ORDER BY s.total_spend DESC LIMIT 10"
)


class SQLToolError(Exception):
    """Raised for any validation or execution failure. The message is fed back to the model on
    retry, so keep messages short, specific and actionable."""


# ---------------------------------------------------------------------------------------------
# Layer 2 - validation
# ---------------------------------------------------------------------------------------------

_RENDERED_NAME_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")


def _function_name_candidates(func: exp.Func) -> set[str]:
    """
    Every name a function call could be known by, lower-cased. sqlglot's internal name for a
    typed function can differ from the name Postgres actually runs (version() is represented as
    CURRENT_VERSION internally), so we check the internal name, the class name AND the name as
    rendered for Postgres -- a block-list entry matches if ANY of them hit.
    """
    names = {type(func).__name__.lower(), func.sql_name().lower()}
    if isinstance(func, exp.Anonymous):
        names.add(func.name.lower())
    m = _RENDERED_NAME_RE.match(func.sql(dialect="postgres"))
    if m:
        names.add(m.group(1).lower())
    return names


def validate_sql(sql: str) -> str:
    """Return the validated, normalised SQL string, or raise SQLToolError."""
    if not sql or not sql.strip():
        raise SQLToolError("the model returned empty SQL")

    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.SqlglotError as e:
        raise SQLToolError(f"SQL could not be parsed: {str(e)[:200]}")

    if len(statements) != 1:
        raise SQLToolError(f"exactly one SQL statement is allowed, got {len(statements)}")
    tree = statements[0]

    if not isinstance(tree, (exp.Select, _SETOP)):
        raise SQLToolError(f"only SELECT queries are allowed, got {type(tree).__name__}")

    for node in tree.find_all(exp.Expression):
        if isinstance(node, _FORBIDDEN_NODES):
            raise SQLToolError(f"forbidden SQL construct: {type(node).__name__}")

    cte_names = {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SQLToolError("table-valued functions are not allowed in FROM")
        if table.db or table.catalog:
            raise SQLToolError("schema-qualified table names are not allowed; use bare table names")
        name = table.name.lower()
        if name in cte_names:
            continue  # reference to a CTE defined in this same query
        if name not in ALLOWED_TABLES:
            raise SQLToolError(
                f"table {name!r} is not allowed; allowed tables: {', '.join(sorted(ALLOWED_TABLES))}"
            )

    for func in tree.find_all(exp.Func):
        for fname in _function_name_candidates(func):
            if fname in _BLOCKED_FUNC_NAMES or fname.startswith(_BLOCKED_FUNC_PREFIXES):
                raise SQLToolError(f"function {fname!r} is not allowed")

    return tree.sql(dialect="postgres")


# ---------------------------------------------------------------------------------------------
# Layer 3 - read-only execution
# ---------------------------------------------------------------------------------------------

def _connect():
    import psycopg2

    # Prefer the dedicated read-only role if configured (Layer 4); otherwise fall back to the
    # main credentials -- Layers 2 and 3 still apply either way.
    user = os.getenv("POSTGRES_RO_USER") or os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_RO_PASSWORD") or os.getenv("POSTGRES_PASSWORD", "postgres")
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "procureiq"),
        user=user,
        password=password,
    )


def _jsonable(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def execute_readonly(sql: str, max_rows: int = MAX_ROWS) -> dict:
    """
    Run already-validated SQL and return {"columns": [...], "rows": [{col: val}, ...],
    "truncated": bool}. Raises SQLToolError on any database error.

    NOTE: this function trusts its input -- call validate_sql() first. It still protects itself
    with a READ ONLY transaction + timeout, so a validator bug can't turn into a write.
    """
    import psycopg2

    conn = None
    try:
        conn = _connect()
        conn.set_session(readonly=True, autocommit=False)
        cur = conn.cursor()
        cur.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
        cur.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        fetched = cur.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = [
            {col: _jsonable(val) for col, val in zip(columns, row)} for row in fetched[:max_rows]
        ]
        return {"columns": columns, "rows": rows, "truncated": truncated}
    except psycopg2.Error as e:
        # pgerror is the server's own message (e.g. 'column "x" does not exist') -- exactly what
        # the model needs to see to repair its query on retry.
        msg = (getattr(e, "pgerror", None) or str(e)).strip().splitlines()[0]
        raise SQLToolError(f"database error: {msg}")
    finally:
        if conn is not None:
            conn.rollback()  # nothing to commit, ever
            conn.close()


# ---------------------------------------------------------------------------------------------
# Text-to-SQL generation
# ---------------------------------------------------------------------------------------------

_SQL_START_RE = re.compile(r"\b(with|select)\b", re.IGNORECASE)


def extract_sql(text: str) -> str:
    """Pull the SQL out of a model reply: strips ```sql fences, chatty preambles, trailing ';'."""
    text = text.strip()
    fence = re.search(r"```(?:sql|postgresql|postgres)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = _SQL_START_RE.search(text)
    if start:
        text = text[start.start():]
    return text.strip().rstrip(";").strip()


def generate_sql(
    question: str,
    generator,
    want_contract_ids: bool = False,
    prior_sql: str = "",
    prior_error: str = "",
) -> str:
    """Ask the generator for SQL (not yet validated). Offline fake generators get FAKE_SQL."""
    if getattr(generator, "is_fake", False):
        return FAKE_SQL

    from prompts import build_sql_prompt  # local import: keeps this module importable standalone

    prompt = build_sql_prompt(
        question,
        today=datetime.date.today().isoformat(),
        want_contract_ids=want_contract_ids,
        prior_sql=prior_sql,
        prior_error=prior_error,
    )
    return extract_sql(generator.generate(prompt))


def run_sql_tool(
    question: str,
    generator,
    want_contract_ids: bool = False,
    prior_sql: str = "",
    prior_error: str = "",
    executor=None,
) -> dict:
    """
    Full tool: generate -> validate -> execute. Always returns a dict (never raises for
    SQL-level problems) so the graph node can put the outcome straight into state:
        {"sql": str, "columns": [...], "rows": [...], "truncated": bool, "error": str}
    `executor` is injectable so tests can run the whole thing without a database.
    """
    executor = executor or execute_readonly
    raw_sql = ""
    try:
        raw_sql = generate_sql(question, generator, want_contract_ids, prior_sql, prior_error)
        safe_sql = validate_sql(raw_sql)
        result = executor(safe_sql)
        return {"sql": safe_sql, "error": "", **result}
    except SQLToolError as e:
        return {"sql": raw_sql, "columns": [], "rows": [], "truncated": False, "error": str(e)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--validate", metavar="SQL", help="Only validate; print the normalised SQL")
    group.add_argument("--run", metavar="SQL", help="Validate, then execute against Postgres")
    args = parser.parse_args()

    try:
        safe = validate_sql(args.validate or args.run)
        print("ACCEPTED:", safe)
        if args.run:
            out = execute_readonly(safe)
            print(f"{len(out['rows'])} row(s){' (truncated)' if out['truncated'] else ''}")
            for r in out["rows"]:
                print("  ", r)
    except SQLToolError as e:
        print("REJECTED:", e)
