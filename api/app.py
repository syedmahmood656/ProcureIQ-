"""
Phase 8 — the serving layer: the agent behind an HTTP API.

    POST /ask          one JSON response: the validated RiskReport (+ optional trace)
    POST /ask/stream   Server-Sent Events: one event per graph step, then the final report
    GET  /health       liveness  (process is up; never touches dependencies)
    GET  /ready        readiness (Postgres + Qdrant reachable, models loaded) -> 503 if not

Design notes
  * Models, the Qdrant client and the compiled graph are built ONCE at startup (lifespan), not per
    request. The CLI pays model-load time on every question; the server pays it once.
  * Endpoints are plain `def` (not `async def`): the agent is blocking code (LLM HTTP calls,
    psycopg2, torch), and FastAPI runs sync endpoints in a worker thread so the event loop stays
    free to answer /health while a question is being processed.
  * A semaphore bounds concurrent agent runs (each run holds a reranker/embedder pass plus several
    LLM calls); excess requests wait briefly, then get 503 + Retry-After instead of piling up.
  * Finished answers are cached briefly (cache.py). Only trustworthy results are cached: verdict
    accept, no SQL error, confidence not low, no guardrail events.
  * Errors never leak internals: the client gets a request_id; the traceback goes to the log.
  * Optional API key (PROCUREIQ_API_KEY). If unset the API is open -- fine on localhost, NOT for
    anything exposed. /health and /ready are always open (load balancers need them).

`create_app(runner_factory, ...)` takes a factory so tests can inject a runner built from scripted
parts; api/main.py wires the real one.
"""
import json
import logging
import secrets
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for sub in ("agent", "retrieval", "ingestion"):
    sys.path.insert(0, str(REPO_ROOT / sub))

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, field_validator  # noqa: E402

from api.cache import TTLCache, make_key  # noqa: E402
from graph import initial_state  # noqa: E402
from observability import trace_request  # noqa: E402   
from guardrails import strip_hidden_chars  # noqa: E402
from schemas import RiskReport  # noqa: E402

log = logging.getLogger("procureiq.api")


# ---------------------------------------------------------------------------------------------
# request / response models
# ---------------------------------------------------------------------------------------------
class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=1000)
    max_retries: int = Field(default=2, ge=0, le=3)
    include_trace: bool = False
    refresh: bool = False  # bypass the cache and force a fresh run

    @field_validator("question")
    @classmethod
    def _clean_question(cls, v: str) -> str:
        v, _ = strip_hidden_chars(v)  # invisible/bidi/tag characters have no place in a question
        v = " ".join(v.split())
        if len(v) < 3:
            raise ValueError("question is too short after cleaning")
        return v


class AskResponse(BaseModel):
    request_id: str
    cached: bool
    latency_s: float
    report: RiskReport
    trace: list[str] | None = None


# ---------------------------------------------------------------------------------------------
# runner: the thin seam between the API and the LangGraph pipeline
# ---------------------------------------------------------------------------------------------
class GraphRunner:
    """Wraps a compiled agent graph. Tests can substitute any object with run/stream/ready."""

    def __init__(self, graph, readiness_checks: dict | None = None):
        self.graph = graph
        self.readiness_checks = readiness_checks or {}

    def run(self, question: str, max_retries: int) -> dict:
        return self.graph.invoke(initial_state(question, max_retries))

    def stream(self, question: str, max_retries: int):
        """Yield (node_name, state_update) as each graph node finishes."""
        for chunk in self.graph.stream(initial_state(question, max_retries), stream_mode="updates"):
            for node, update in chunk.items():
                yield node, (update or {})

    def ready(self) -> dict:
        results = {}
        for name, check in self.readiness_checks.items():
            try:
                check()
                results[name] = "ok"
            except Exception as e:  # noqa: BLE001
                results[name] = f"FAILED: {type(e).__name__}"
        return results


def _cacheable(report: RiskReport, state: dict) -> bool:
    return (
        report.verdict == "accept"
        and report.confidence != "low"
        and not state.get("sql_error")
        and not report.guardrail_flags
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


# ---------------------------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------------------------
def create_app(
    runner_factory,
    *,
    api_key: str | None = None,
    cache: TTLCache | None = None,
    max_concurrent: int = 2,
    queue_timeout_s: float = 30.0,
) -> FastAPI:
    cache = cache if cache is not None else TTLCache()
    semaphore = threading.BoundedSemaphore(max_concurrent)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("starting: building runner (loading models, connecting)...")
        app.state.runner = runner_factory()
        if not api_key:
            log.warning("PROCUREIQ_API_KEY is not set: the API is OPEN. Fine for localhost, not for exposure.")
        log.info("ready")
        yield

    app = FastAPI(title="ProcureIQ API", version="0.8.0", lifespan=lifespan)
    app.state.cache = cache
    app.state.semaphore = semaphore

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = uuid.uuid4().hex[:12]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if api_key and not (x_api_key and secrets.compare_digest(x_api_key, api_key)):
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    def _acquire(rid: str) -> None:
        if not semaphore.acquire(timeout=queue_timeout_s):
            log.warning("request_id=%s rejected: server busy", rid)
            raise HTTPException(status_code=503, detail="server busy, try again shortly",
                                headers={"Retry-After": "10"})

    # ---- probes -------------------------------------------------------------------------
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready(response: Response):
        checks = app.state.runner.ready()
        ok = all(v == "ok" for v in checks.values())
        if not ok:
            response.status_code = 503
        return {"status": "ready" if ok else "not_ready", "checks": checks}

    # ---- /ask ---------------------------------------------------------------------------
    @app.post("/ask", response_model=AskResponse, response_model_exclude_none=True,
              dependencies=[Depends(require_key)])
    def ask(req: AskRequest, request: Request, response: Response):
        rid = request.state.request_id
        key = make_key(req.question, req.max_retries)

        if not req.refresh:
            hit = cache.get(key)
            if hit is not None:
                report, trace = hit
                response.headers["X-Cache"] = "HIT"
                log.info("ask request_id=%s cached=True route=%s", rid, report.route)
                with trace_request("procureiq.ask", req.question, request_id=rid, tags=["api", "cache_hit"]) as t:
                    t.result(route=report.route, verdict=report.verdict, retries=report.retries,
                             confidence=report.confidence, cached=True)
                return AskResponse(request_id=rid, cached=True, latency_s=0.0, report=report,
                                   trace=trace if req.include_trace else None)

        _acquire(rid)
        t0 = time.perf_counter()
        try:
            with trace_request("procureiq.ask", req.question, request_id=rid, tags=["api"]) as t:
                state = app.state.runner.run(req.question, req.max_retries)
                t.result_from_state(state)
            report = RiskReport(**state["report"])
        except Exception:  # noqa: BLE001
            log.exception("ask request_id=%s failed", rid)
            raise HTTPException(status_code=500, detail={"request_id": rid, "error": "internal error"})
        finally:
            semaphore.release()

        latency = round(time.perf_counter() - t0, 2)
        if _cacheable(report, state):
            cache.set(key, (report, state["trace"]))
        response.headers["X-Cache"] = "MISS"
        log.info("ask request_id=%s cached=False route=%s verdict=%s retries=%s confidence=%s "
                 "latency_s=%.2f q_len=%d", rid, report.route, report.verdict, report.retries,
                 report.confidence, latency, len(req.question))
        return AskResponse(request_id=rid, cached=False, latency_s=latency, report=report,
                           trace=state["trace"] if req.include_trace else None)

    # ---- /ask/stream (Server-Sent Events) -------------------------------------------------
    @app.post("/ask/stream", dependencies=[Depends(require_key)])
    def ask_stream(req: AskRequest, request: Request):
        rid = request.state.request_id
        key = make_key(req.question, req.max_retries)
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": rid}

        if not req.refresh:
            hit = cache.get(key)
            if hit is not None:
                report, trace = hit

                def cached_events():
                    yield _sse("start", {"request_id": rid, "cached": True})
                    yield _sse("result", {"request_id": rid, "cached": True, "latency_s": 0.0,
                                          "report": report.model_dump(),
                                          "trace": trace if req.include_trace else None})

                return StreamingResponse(cached_events(), media_type="text/event-stream", headers=headers)

        _acquire(rid)  # acquired BEFORE streaming starts so a busy server can still answer 503

        def events():
            t0 = time.perf_counter()
            try:
                yield _sse("start", {"request_id": rid, "cached": False})
                final = dict(initial_state(req.question, req.max_retries))
                emitted = 0
                for node, update in app.state.runner.stream(req.question, req.max_retries):
                    final.update(update)
                    lines = update.get("trace", [])[emitted:]
                    emitted = max(emitted, len(update.get("trace", [])))
                    yield _sse("step", {"node": node, "lines": lines})
                report = RiskReport(**final["report"])
                latency = round(time.perf_counter() - t0, 2)
                if _cacheable(report, final):
                    cache.set(key, (report, final["trace"]))
                log.info("stream request_id=%s route=%s verdict=%s retries=%s latency_s=%.2f",
                         rid, report.route, report.verdict, report.retries, latency)
                yield _sse("result", {"request_id": rid, "cached": False, "latency_s": latency,
                                      "report": report.model_dump(),
                                      "trace": final["trace"] if req.include_trace else None})
            except Exception:  # noqa: BLE001
                log.exception("stream request_id=%s failed", rid)
                yield _sse("error", {"request_id": rid, "error": "internal error"})
            finally:
                semaphore.release()  # also runs if the client disconnects mid-stream

        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    return app
