"""
Phase 11 — observability: see exactly what the agent did on any run (Langfuse tracing).

WHAT YOU GET in the Langfuse UI for every question:
    procureiq.ask / procureiq.run_agent          <- one trace per question (latency, route, verdict)
      planner > router > sql_tool > synthesizer > critic > planner (retry) > ... > report
        llm.generate                             <- every LLM call: prompt, reply, model, duration
    + scores per trace: verdict, retries, confidence  (filter "all retried runs", chart quality over time)

DESIGN RULES
  1. Tracing must NEVER break or slow a request. Every Langfuse call is wrapped; failures are logged
     at debug level and swallowed. With no keys configured everything is a free no-op.
  2. No changes inside the nodes. Nodes and the LLM generator are wrapped from outside
     (traced_node / TracedGenerator), so the agent logic stays exactly as tested.
  3. Privacy switch: LANGFUSE_CAPTURE_CONTENT=false keeps timings, structure and scores but replaces
     every text value (questions, prompts, contract text, SQL rows) with "<N chars>" BEFORE it leaves
     your machine. Default is true (this project uses public/synthetic data).

CONFIGURATION (environment / .env)
    LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY   from your Langfuse project (cloud or self-hosted)
    LANGFUSE_HOST                              e.g. https://cloud.langfuse.com  (default: EU cloud)
    LANGFUSE_TRACING_ENABLED=false             force-disable even if keys are present
    LANGFUSE_CAPTURE_CONTENT=false             redact all text content (see rule 3)

Not traced (known gap): the /ask/stream endpoint -- its graph steps are consumed across several worker
threads, which would break trace nesting. Use /ask (or the CLI) for traced runs.
"""
import atexit
import functools
import logging
import os
from contextlib import contextmanager
from typing import Any, Callable, Optional

log = logging.getLogger("procureiq.observability")

_client = None  # the Langfuse client, or None when tracing is off

NODE_TYPES = {  # Langfuse observation types -> distinct icons/filters in the UI
    "planner": "chain", "router": "chain", "sql_tool": "tool", "rag_tool": "retriever",
    "synthesizer": "chain", "critic": "guardrail", "report": "chain",
}


# ------------------------------------------------------------------ setup
def _truthy(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() not in ("0", "false", "no", "off", "")


def _redact(data: Any = None, **_: Any) -> Any:
    """Langfuse `mask` hook: keep structure, numbers and booleans; replace every string by its length."""
    if isinstance(data, str):
        return f"<{len(data)} chars>"
    if isinstance(data, dict):
        return {k: _redact(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_redact(v) for v in data]
    return data


def init_tracing(**client_overrides) -> bool:
    """Start tracing if configured. Safe to call repeatedly. Returns True when tracing is active.
    `client_overrides` go straight to langfuse.Langfuse (tests pass span_exporter=...)."""
    global _client
    if _client is not None:
        return True
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # never overrides real env vars
    if not _truthy("LANGFUSE_TRACING_ENABLED", True):
        return False
    have_keys = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))
    if not have_keys and not client_overrides.get("span_exporter"):
        log.info("tracing off: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set")
        return False
    try:
        from langfuse import Langfuse

        kwargs = dict(client_overrides)
        if not _truthy("LANGFUSE_CAPTURE_CONTENT", True):
            kwargs.setdefault("mask", _redact)
        _client = Langfuse(**kwargs)
        atexit.register(shutdown_tracing)
        log.info("tracing on (content capture %s)", "ON" if "mask" not in kwargs else "OFF/redacted")
        return True
    except Exception as e:  # noqa: BLE001 - a bad config must not stop the app
        log.warning("tracing disabled: could not start Langfuse (%s: %s)", type(e).__name__, str(e)[:120])
        _client = None
        return False


def enabled() -> bool:
    return _client is not None


def flush() -> None:
    if _client is not None:
        try:
            _client.flush()
        except Exception:  # noqa: BLE001
            log.debug("flush failed", exc_info=True)


def shutdown_tracing() -> None:
    global _client
    if _client is not None:
        try:
            _client.shutdown()  # flushes pending spans
        except Exception:  # noqa: BLE001
            log.debug("shutdown failed", exc_info=True)
        _client = None


# ------------------------------------------------------------------ safe primitives
class _NoopObs:
    def update(self, **_: Any) -> None: ...
    def result(self, **_: Any) -> None: ...
    def result_from_state(self, _state: Any) -> None: ...


_NOOP = _NoopObs()


class _Obs:
    """Thin wrapper over a Langfuse span/generation whose methods never raise."""

    def __init__(self, raw):
        self._raw = raw

    def update(self, **kw: Any) -> None:
        try:
            self._raw.update(**kw)
        except Exception:  # noqa: BLE001
            log.debug("observation update failed", exc_info=True)

    def result(self, *, route=None, verdict=None, retries=None, confidence=None, **extra: Any) -> None:
        """Trace-level outcome: output payload + scores you can filter/aggregate on in Langfuse."""
        out = {k: v for k, v in dict(route=route, verdict=verdict, retries=retries, confidence=confidence, **extra).items() if v is not None}
        self.update(output=out)
        for name, value, kind in (("verdict", verdict, "CATEGORICAL"), ("confidence", confidence, "CATEGORICAL"), ("retries", retries, "NUMERIC")):
            if value is None or _client is None:
                continue
            try:
                _client.score_current_trace(name=name, value=value if kind == "NUMERIC" else str(value), data_type=kind)
            except Exception:  # noqa: BLE001
                log.debug("score failed", exc_info=True)

    def result_from_state(self, state: Any) -> None:
        try:
            report = state.get("report") or {}
            self.result(
                route=state.get("route"), verdict=state.get("verdict"),
                retries=state.get("retry_count"), confidence=report.get("confidence"),
                answer_chars=len(state.get("answer") or ""),
            )
        except Exception:  # noqa: BLE001
            log.debug("result_from_state failed", exc_info=True)


@contextmanager
def _guarded(factory: Callable[[], Any]):
    """Enter a Langfuse context manager; if Langfuse misbehaves, yield None and carry on untraced.
    Exceptions raised by the CALLER's body always propagate unchanged."""
    try:
        cm = factory()
        value = cm.__enter__()
    except Exception:  # noqa: BLE001
        log.debug("could not open observation", exc_info=True)
        yield None
        return
    try:
        yield value
    except BaseException as e:
        try:
            cm.__exit__(type(e), e, e.__traceback__)
        except Exception:  # noqa: BLE001
            log.debug("could not close observation", exc_info=True)
        raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            log.debug("could not close observation", exc_info=True)


@contextmanager
def observation(name: str, as_type: str = "span", input: Any = None, metadata: Any = None, **kw: Any):
    """A child span (or generation/tool/...) under whatever span is currently open."""
    if _client is None:
        yield _NOOP
        return
    with _guarded(lambda: _client.start_as_current_observation(
        name=name, as_type=as_type, input=input, metadata=metadata, **kw
    )) as raw:
        obs = _Obs(raw) if raw is not None else _NOOP
        try:
            yield obs
        except BaseException as e:
            obs.update(level="ERROR", status_message=f"{type(e).__name__}: {str(e)[:200]}")
            raise


@contextmanager
def trace_request(
    name: str, question: str, *, request_id: Optional[str] = None, user_id: Optional[str] = None,
    session_id: Optional[str] = None, tags: Optional[list[str]] = None,
):
    """Root of one trace = one user question. Everything opened inside nests under it."""
    if _client is None:
        yield _NOOP
        return
    from langfuse import propagate_attributes

    meta = {"request_id": request_id} if request_id else None
    with _guarded(lambda: propagate_attributes(
        user_id=user_id, session_id=session_id, tags=tags, trace_name=name, metadata=meta
    )):
        with observation(name, as_type="agent", input={"question": question}) as root:
            yield root


# ------------------------------------------------------------------ wrapping the agent
def _summarize(value: Any, depth: int = 0) -> Any:
    """Small, readable view of a node's state update (full retrieved texts would flood the UI)."""
    if isinstance(value, str):
        return value if len(value) <= 400 else value[:400] + f"... (+{len(value) - 400} chars)"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _summarize(v, depth + 1) for k, v in list(value.items())[:20]} if depth < 2 else f"<dict {len(value)} keys>"
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(v, str) for v in value):
            return list(value)[-3:]
        return {"n": len(value), "sample": [_summarize(v, depth + 1) for v in list(value)[:2]]} if depth < 2 else f"<{len(value)} items>"
    return f"<{type(value).__name__}>"


def traced_node(name: str, fn: Callable) -> Callable:
    """Wrap a LangGraph node (state -> update dict) in a span. Behaviour is otherwise unchanged."""

    @functools.wraps(fn)
    def wrapper(state, *args, **kwargs):
        if _client is None:
            return fn(state, *args, **kwargs)
        with observation(
            name, as_type=NODE_TYPES.get(name, "span"),
            input=_summarize({k: state.get(k) for k in ("question", "search_query", "route", "retry_count", "critic_feedback") if state.get(k) not in (None, "")}),
        ) as obs:
            update = fn(state, *args, **kwargs)
            obs.update(output=_summarize(update))
            return update

    return wrapper


class TracedGenerator:
    """Wraps any LLM generator: each generate() becomes a 'generation' (prompt, reply, model, timing).
    Everything else (is_fake, model, ...) is delegated untouched."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def generate(self, prompt: str, *args, **kwargs) -> str:
        if _client is None:
            return self._inner.generate(prompt, *args, **kwargs)
        model = str(getattr(self._inner, "model", type(self._inner).__name__))
        with observation("llm.generate", as_type="generation", input=prompt, model=model) as obs:
            out = self._inner.generate(prompt, *args, **kwargs)
            obs.update(output=out)
            return out


def wrap_generator(generator):
    return generator if isinstance(generator, TracedGenerator) else TracedGenerator(generator)
