"""
Phase 9 — MCP client: lets the agent run its SQL through the MCP server instead of in-process.

`MCPSQLExecutor` is a drop-in for sql_tool.execute_readonly (same signature: sql -> dict, raises
SQLToolError), so it plugs into the existing `sql_executor=` injection point of build_agent_graph
with NO change to the graph or nodes.

The LangGraph pipeline is synchronous (and the API runs it in worker threads) while the MCP SDK is
async, so the executor owns ONE background thread with ONE event loop and ONE long-lived session.
Calls from any thread are submitted to that loop (thread-safe). If the connection dies it
reconnects once, transparently; if the server stays unreachable the caller gets a SQLToolError, which
the graph already handles (critic -> retry -> honest give-up).

Transport:  stdio (default: spawns mcp_servers/sql_server.py as a child process)
            or streamable HTTP when a URL is given (PROCUREIQ_MCP_SQL_URL=http://host:8765/mcp).

Config (environment):
    PROCUREIQ_SQL_BACKEND     local | mcp                      (default local = Phase 6 behaviour)
    PROCUREIQ_MCP_SQL_URL     if set (with backend=mcp) use HTTP instead of spawning a child
    PROCUREIQ_MCP_TIMEOUT_S   per-call timeout seconds          (default 20)
"""
import asyncio
import atexit
import json
import logging
import os
import re
import sys
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Optional

from sql_tool import SQLToolError

log = logging.getLogger("procureiq.mcp.client")

SERVER_SCRIPT = Path(__file__).resolve().parent.parent / "mcp_servers" / "sql_server.py"
_TOOL_ERR_PREFIX = re.compile(r"^Error executing tool \w+:\s*")


def _describe(e: BaseException) -> str:
    """Readable one-liner for an exception; unwraps anyio/asyncio ExceptionGroups to the root cause."""
    while getattr(e, "exceptions", None):
        e = e.exceptions[0]
    return f"{type(e).__name__}: {str(e)[:150]}".rstrip(": ")


class MCPSQLExecutor:
    def __init__(self, url: Optional[str] = None, command: Optional[list[str]] = None, timeout_s: float = 20.0):
        self.url = url
        self.command = command or [sys.executable, str(SERVER_SCRIPT)]
        self.timeout_s = timeout_s
        self._lock = threading.Lock()          # guards connect / disconnect
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session = None
        self._runner = None                    # concurrent.futures.Future of the session-owning task
        self._stop: Optional[asyncio.Event] = None
        atexit.register(self.close)

    # ------------------------------------------------------------------ connection lifecycle
    def _child_env(self) -> dict:
        # Least privilege: the SQL server needs DB settings, NOT the LLM API keys in our env.
        from mcp.client.stdio import get_default_environment

        env = get_default_environment()
        env.update({k: v for k, v in os.environ.items() if k.startswith("POSTGRES_")})
        return env

    async def _own_session(self, ready: threading.Event, box: dict) -> None:
        """One task owns the transport + session for its whole life (anyio requires enter/exit in
        the same task). Parks on `_stop` until close() or until the connection is dropped."""
        from mcp import ClientSession, StdioServerParameters

        self._stop = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                if self.url:
                    from mcp.client.streamable_http import streamablehttp_client

                    read, write, _ = await stack.enter_async_context(streamablehttp_client(self.url))
                else:
                    from mcp.client.stdio import stdio_client

                    params = StdioServerParameters(
                        command=self.command[0], args=self.command[1:], env=self._child_env()
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._session = session
                ready.set()
                await self._stop.wait()
        except Exception as e:  # noqa: BLE001 - reported to the caller via `box`
            box["error"] = e
        finally:
            self._session = None
            ready.set()  # never leave a connecting caller hanging

    def _connect(self) -> None:
        if self._session is not None:
            return
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-sql-client", daemon=True)
            self._thread.start()
        script = Path(self.command[-1]) if not self.url and len(self.command) > 1 else None
        if script is not None and script.suffix == ".py" and not script.exists():
            raise SQLToolError(
                f"SQL service unavailable (server script not found: {script}; the folder must be named 'mcp_servers')"
            )
        ready, box = threading.Event(), {}
        self._runner = asyncio.run_coroutine_threadsafe(self._own_session(ready, box), self._loop)
        if not ready.wait(timeout=self.timeout_s) or self._session is None:
            self._runner.cancel()
            reason = _describe(box["error"]) if box.get("error") else "timed out"
            raise SQLToolError(f"SQL service unavailable ({reason})")

    def _disconnect(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._runner is not None:
            try:
                self._runner.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._session = None
        self._runner = None

    def close(self) -> None:
        with self._lock:
            self._disconnect()

    # ------------------------------------------------------------------ calling tools
    async def _call(self, tool: str, arguments: dict):
        return await self._session.call_tool(tool, arguments)

    def _call_tool(self, tool: str, arguments: dict):
        for attempt in (1, 2):
            with self._lock:
                self._connect()
                session_loop = self._loop
            try:
                fut = asyncio.run_coroutine_threadsafe(self._call(tool, arguments), session_loop)
                return fut.result(timeout=self.timeout_s)
            except Exception as e:  # noqa: BLE001 - transport-level failure (tool errors are results)
                log.warning("MCP call failed (attempt %d): %s", attempt, type(e).__name__)
                with self._lock:
                    self._disconnect()
                if attempt == 2:
                    raise SQLToolError(f"SQL service unavailable ({_describe(e)})")

    @staticmethod
    def _text(result) -> str:
        return " ".join(getattr(b, "text", "") for b in (result.content or [])).strip()

    def __call__(self, sql: str) -> dict:
        """Same contract as sql_tool.execute_readonly."""
        result = self._call_tool("run_readonly_sql", {"sql": sql})
        if result.isError:
            raise SQLToolError(_TOOL_ERR_PREFIX.sub("", self._text(result)) or "SQL tool error")
        data = result.structuredContent
        if data is None:  # older servers: JSON in the text block
            try:
                data = json.loads(self._text(result))
            except ValueError:
                raise SQLToolError("SQL service returned an unreadable response")
        if "result" in data and "rows" not in data:  # some SDK versions wrap non-object returns
            data = data["result"]
        try:
            return {"columns": list(data["columns"]), "rows": list(data["rows"]), "truncated": bool(data["truncated"])}
        except (KeyError, TypeError):
            raise SQLToolError("SQL service returned an unexpected response shape")

    def get_schema(self) -> str:
        return self._text(self._call_tool("get_schema", {}))

    def ping(self) -> None:
        """Readiness probe: raises if the server can't be reached or lacks the expected tool."""
        with self._lock:
            self._connect()
        fut = asyncio.run_coroutine_threadsafe(self._session.list_tools(), self._loop)
        names = {t.name for t in fut.result(timeout=self.timeout_s).tools}
        if "run_readonly_sql" not in names:
            raise RuntimeError("MCP server is missing run_readonly_sql")


def get_sql_executor() -> Optional[MCPSQLExecutor]:
    """None = use the in-process Phase 6 executor. Otherwise an MCP-backed executor."""
    backend = os.getenv("PROCUREIQ_SQL_BACKEND", "local").strip().lower()
    if backend == "local":
        return None
    if backend != "mcp":
        raise ValueError(f"PROCUREIQ_SQL_BACKEND must be 'local' or 'mcp', got {backend!r}")
    return MCPSQLExecutor(
        url=os.getenv("PROCUREIQ_MCP_SQL_URL") or None,
        timeout_s=float(os.getenv("PROCUREIQ_MCP_TIMEOUT_S", "20")),
    )
