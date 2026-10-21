"""
Phase 8 — the real wiring: loads the actual models / connections and exposes `app` for uvicorn.

Run (from the repo root, with `docker compose -f infra/docker-compose.yml up -d` already running):
    uvicorn api.main:app --port 8000

Configuration (environment variables, e.g. in .env):
    PROCUREIQ_PROVIDER        LLM provider: openrouter | huggingface | gemini | fake   (default openrouter)
    PROCUREIQ_FAKE_MODELS     1 = fake embedder/reranker (offline plumbing only)       (default 0)
    PROCUREIQ_API_KEY         if set, /ask and /ask/stream require header X-API-Key    (default: open)
    PROCUREIQ_CACHE_TTL_S     answer-cache lifetime in seconds, 0 disables            (default 600)
    PROCUREIQ_CACHE_MAX       max cached answers                                       (default 256)
    PROCUREIQ_MAX_CONCURRENT  max agent runs at once                                   (default 2)
    PROCUREIQ_QUEUE_TIMEOUT_S how long a request waits for a slot before 503           (default 30)
    QDRANT_URL                                                                         (default http://localhost:6333)
    QDRANT_PATH               use a file-based local Qdrant instead of QDRANT_URL (demos/tests only)
"""
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
for sub in ("agent", "retrieval", "ingestion"):
    sys.path.insert(0, str(REPO_ROOT / sub))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from api.app import GraphRunner, create_app  # noqa: E402
from api.cache import TTLCache  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("procureiq.api")


def build_real_runner() -> GraphRunner:
    from observability import init_tracing  
    init_tracing()
    from embedder import FakeEmbedder, SentenceTransformerEmbedder
    from generator import get_generator
    from graph import build_agent_graph
    from qdrant_client import QdrantClient
    from reranker import CrossEncoderReranker, FakeReranker
    from sql_tool import _connect
    from mcp_client import get_sql_executor

    provider = os.getenv("PROCUREIQ_PROVIDER", "openrouter")
    fake_models = os.getenv("PROCUREIQ_FAKE_MODELS", "0") == "1"
    if provider == "fake" or fake_models:
        log.warning("FAKE provider/models active: answers are NOT real. Offline plumbing only.")

    embedder = FakeEmbedder() if fake_models else SentenceTransformerEmbedder()
    reranker = FakeReranker() if fake_models else CrossEncoderReranker()
    qdrant_path = os.getenv("QDRANT_PATH")
    client = (
        QdrantClient(path=qdrant_path)
        if qdrant_path
        else QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"), api_key=os.getenv("QDRANT_API_KEY") or None)
    )
    generator = get_generator(provider)
    
    sql_executor = get_sql_executor()          # None = in-process (Phase 6); else MCP-backed
    graph = build_agent_graph(client, embedder, reranker, generator, sql_executor=sql_executor)

    try:  # warm the embedder so the first real request doesn't pay lazy-init cost
        embedder.embed_query("warm up")
    except Exception as e:  # noqa: BLE001
        log.warning("embedder warm-up failed (%s); first request may be slow", type(e).__name__)

    def check_postgres():
        conn = _connect()
        try:
            conn.cursor().execute("SELECT 1")
        finally:
            conn.close()

    checks = {
        "postgres": check_postgres,
        "qdrant": lambda: client.get_collections(),
    }
    if sql_executor is not None:
        checks["mcp_sql"] = sql_executor.ping
        
    return GraphRunner(graph, readiness_checks=checks)

app = create_app(
    build_real_runner,
    api_key=os.getenv("PROCUREIQ_API_KEY") or None,
    cache=TTLCache(
        max_entries=int(os.getenv("PROCUREIQ_CACHE_MAX", "256")),
        ttl_s=float(os.getenv("PROCUREIQ_CACHE_TTL_S", "600")),
    ),
    max_concurrent=int(os.getenv("PROCUREIQ_MAX_CONCURRENT", "2")),
    queue_timeout_s=float(os.getenv("PROCUREIQ_QUEUE_TIMEOUT_S", "30")),
)