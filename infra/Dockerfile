# One image for both app services: the API (default CMD) and the MCP SQL server (command override
# in docker-compose.yml). Build context = repo root (see compose `build.context: ..`).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# 1) CPU-only PyTorch first. sentence-transformers would otherwise pull the CUDA build (several GB)
#    that cannot be used without a GPU anyway. Later `pip install` sees torch as already satisfied.
# hadolint ignore=DL3013
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# 2) Third-party dependencies (own layer: rebuilt only when requirements-api.txt changes)
COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

# 3) Bake the model weights into the image so containers start offline and fast.
#    KEEP IN SYNC with the defaults in ingestion/embedder.py and retrieval/reranker.py
#    (infra/test_infra_offline.py checks this).
ARG EMBED_MODEL=BAAI/bge-small-en-v1.5
ARG RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
RUN python -c "import os; from sentence_transformers import SentenceTransformer, CrossEncoder; SentenceTransformer(os.environ['EMBED_MODEL']); CrossEncoder(os.environ['RERANK_MODEL'])" \
    && chmod -R a+rX /opt/hf
ENV HF_HUB_OFFLINE=1

# 4) Application code last (changes most often -> cheapest rebuilds)
COPY agent ./agent
COPY api ./api
COPY retrieval ./retrieval
COPY ingestion ./ingestion
COPY mcp_servers ./mcp_servers

# Never run as root
RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
