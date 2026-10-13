"""
Phase 12 offline tests — no Docker needed. They guard the infra files against the drift that
otherwise only shows up as a confusing failure inside a container.

Needs PyYAML (already installed if you have `datasets`/`ragas`; else: pip install pyyaml).
Run:   python infra/test_infra_offline.py
"""
import ast
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
N = 0


def check(label, cond):
    global N
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label
    N += 1


compose = yaml.safe_load((ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8"))
docker = (ROOT / "infra" / "Dockerfile").read_text(encoding="utf-8")
ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
svc = compose["services"]


def env_keys(service):
    env = svc[service].get("environment", [])
    return {e.split("=", 1)[0] for e in env} if isinstance(env, list) else set(env)


def env_map(service):
    return {e.split("=", 1)[0]: e.split("=", 1)[1] for e in svc[service]["environment"] if "=" in e}


# ---- 1. data safety: your existing volumes must keep working
check("no top-level `name:` (project stays 'infra' => existing volumes are reused)", "name" not in compose)
check("volume names unchanged", set(compose["volumes"]) == {"procureiq_pgdata", "procureiq_qdrant_data"})
check("postgres still mounts its data volume + schema init", "procureiq_pgdata:/var/lib/postgresql/data" in svc["postgres"]["volumes"] and any("schema.sql" in v for v in svc["postgres"]["volumes"]))
check("qdrant still mounts its data volume", "procureiq_qdrant_data:/qdrant/storage" in svc["qdrant"]["volumes"])
check("container names of the dev services unchanged", svc["postgres"]["container_name"] == "procureiq_postgres" and svc["qdrant"]["container_name"] == "procureiq_qdrant")

# ---- 2. dev workflow preserved
check("postgres/qdrant have no profile (plain `up -d` still starts just them)", "profiles" not in svc["postgres"] and "profiles" not in svc["qdrant"])
check("api and mcp-sql are opt-in via profile 'app'", svc["api"]["profiles"] == ["app"] and svc["mcp-sql"]["profiles"] == ["app"])
published = [p for s in svc.values() for p in s.get("ports", [])]
check("every published port is bound to loopback only", published and all(p.startswith("127.0.0.1:") for p in published))

# ---- 3. security boundaries
check("mcp-sql publishes NO ports (it has no auth)", "ports" not in svc["mcp-sql"] and svc["mcp-sql"]["expose"] == ["8765"])
secret_vars = {"OPENROUTER_API_KEY", "GEMINI_API_KEY", "HF_TOKEN", "PROCUREIQ_API_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY"}
check("mcp-sql receives no LLM/API/tracing secrets", not (env_keys("mcp-sql") & secret_vars))
check("mcp-sql gets the read-only DB role variables", {"POSTGRES_RO_USER", "POSTGRES_RO_PASSWORD"} <= env_keys("mcp-sql"))
check("no env_file used (explicit allow-list per service)", "env_file" not in svc["api"] and "env_file" not in svc["mcp-sql"])
check("no secret value hard-coded in the compose file", not re.search(r"(sk-|pk-lf|AIza)[A-Za-z0-9_-]{8,}", (ROOT / "infra" / "docker-compose.yml").read_text()))

# ---- 4. wiring between containers
api = env_map("api")
check("api -> Postgres by service name", api["POSTGRES_HOST"] == "postgres" and api["POSTGRES_DB"] == "procureiq")
check("api -> Qdrant by service name; QDRANT_PATH blanked so a .env demo value can't win", api["QDRANT_URL"] == "http://qdrant:6333" and api["QDRANT_PATH"] == "")
check("api uses the MCP SQL backend at the mcp-sql service", api["PROCUREIQ_SQL_BACKEND"] == "mcp" and api["PROCUREIQ_MCP_SQL_URL"] == "http://mcp-sql:8765/mcp")
mcp = env_map("mcp-sql")
check("mcp-sql -> Postgres by service name", mcp["POSTGRES_HOST"] == "postgres")
cmd = svc["mcp-sql"]["command"]
check("mcp-sql serves HTTP on 0.0.0.0:8765 (reachable from other containers)", "--transport" in cmd and cmd[cmd.index("--transport") + 1] == "http" and cmd[cmd.index("--host") + 1] == "0.0.0.0" and cmd[cmd.index("--port") + 1] == "8765")
check("api and mcp-sql share one image", svc["api"]["image"] == svc["mcp-sql"]["image"])
check("passthrough of the knobs the code reads", {"OPENROUTER_API_KEY", "PROCUREIQ_PROVIDER", "PROCUREIQ_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "PROCUREIQ_MAX_CONCURRENT"} <= env_keys("api"))

# ---- 5. start-up ordering and health
check("postgres has a healthcheck", "healthcheck" in svc["postgres"])
check("api waits for healthy postgres + healthy mcp-sql", svc["api"]["depends_on"]["postgres"]["condition"] == "service_healthy" and svc["api"]["depends_on"]["mcp-sql"]["condition"] == "service_healthy")
check("mcp-sql waits for healthy postgres", svc["mcp-sql"]["depends_on"]["postgres"]["condition"] == "service_healthy")
check("api and mcp-sql have healthchecks; api allows model-load time", "healthcheck" in svc["mcp-sql"] and svc["api"]["healthcheck"]["start_period"] in ("90s", "120s", "180s"))
check("api healthcheck hits /health, which exists", "/health" in " ".join(svc["api"]["healthcheck"]["test"]) and '"/health"' in (ROOT / "api" / "app.py").read_text())

# ---- 6. Dockerfile
check("python 3.12 slim base (the version these tests ran on)", re.search(r"^FROM python:3\.12-slim", docker, re.M) is not None)
check("CPU-only torch installed BEFORE requirements", docker.index("whl/cpu") < docker.index("requirements-api.txt"))
check("runs as a non-root user", re.search(r"^USER app", docker, re.M) is not None and "useradd" in docker)
check("code is copied AFTER dependencies and models (cache-friendly)", docker.index("COPY agent") > docker.index("requirements-api.txt") > 0 and docker.index("COPY agent") > docker.index("SentenceTransformer("))
check("HF_HUB_OFFLINE set only after the models were baked in", docker.index("ENV HF_HUB_OFFLINE=1") > docker.index("SentenceTransformer("))
check("default CMD serves the API on 0.0.0.0:8000", "uvicorn" in docker and "0.0.0.0" in docker and "8000" in docker)
check("no secrets baked in (no COPY of .env / ARG keys)", ".env" not in re.findall(r"^COPY .*", docker, re.M).__str__() and "KEY" not in docker.upper().replace("KEYS", ""))


def default_model(path, cls):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == cls:
            for f in n.body:
                if isinstance(f, ast.FunctionDef) and f.name == "__init__":
                    return f.args.defaults[0].value
    raise AssertionError(f"{cls} not found")


embed_default = default_model("ingestion/embedder.py", "SentenceTransformerEmbedder")
rerank_default = default_model("retrieval/reranker.py", "CrossEncoderReranker")
check(f"baked embedding model == code default ({embed_default})", f"ARG EMBED_MODEL={embed_default}" in docker)
check(f"baked reranker model == code default ({rerank_default})", f"ARG RERANK_MODEL={rerank_default}" in docker)

# ---- 7. .dockerignore
for needed in (".env", "venv", "data", "data_source_cuad", ".git", "**/__pycache__", "**/test_*.py"):
    check(f".dockerignore excludes {needed}", needed in ignore)
check(".dockerignore does not exclude code the image copies", not ({"agent", "api", "retrieval", "ingestion", "mcp_servers"} & set(ignore)))

# ---- 8. requirements-api.txt covers every runtime import (and nothing eval-only sneaks in)
dirs = ["agent", "api", "retrieval", "ingestion", "mcp_servers"]
local = set(dirs) | {p.stem for d in dirs for p in (ROOT / d).glob("*.py")}
third = set()
for d in dirs:
    for p in (ROOT / d).glob("*.py"):
        if p.name.startswith("test_"):
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else ([n.module] if isinstance(n, ast.ImportFrom) and n.module and n.level == 0 else [])
            third |= {m.split(".")[0] for m in mods} - sys.stdlib_module_names - local
PKG = {"dotenv": "python-dotenv", "psycopg2": "psycopg2-binary", "qdrant_client": "qdrant-client", "sentence_transformers": "sentence-transformers", "google": "google-genai", "huggingface_hub": "huggingface_hub"}
reqs = {re.split(r"[<>=!~ ]", ln.strip(), 1)[0].lower().replace("_", "-") for ln in (ROOT / "requirements-api.txt").read_text().splitlines() if ln.strip() and not ln.startswith("#")}
missing = {m for m in third if PKG.get(m, m).lower().replace("_", "-") not in reqs}
check(f"every runtime import has a requirement ({len(third)} third-party modules)", not missing)
check("uvicorn (the server) is installed", "uvicorn" in reqs)
check("no evaluation/data-prep packages in the serving image", not (reqs & {"ragas", "pandas", "datasets", "faker", "langchain-openai", "langchain-huggingface", "langchain-google-genai"}))
check("runtime code imports nothing from the eval stack", not (third & {"ragas", "pandas", "datasets", "faker", "langchain_openai", "langchain_huggingface"}))

# ---- 9. Phase 13: repo hygiene and CI
import shutil
import subprocess
import tempfile

env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
documented = {ln.lstrip("# ").split("=", 1)[0].strip() for ln in env_example.splitlines() if "=" in ln}
passthrough = {e for s_ in ("api", "mcp-sql") for e in svc[s_]["environment"] if "=" not in e}
check("every variable compose passes through is documented in .env.example", passthrough <= documented)
secret_lines = [ln for ln in env_example.splitlines() if "=" in ln and not ln.lstrip().startswith("#") and re.search(r"(API_KEY|SECRET|TOKEN)=", ln)]
check(".env.example has no values for key/secret/token variables", all(ln.split("=", 1)[1].split("#")[0].strip() == "" for ln in secret_lines))
check(".env.example contains no key-shaped strings", not re.search(r"(sk-or-v1-|sk-lf-|pk-lf-|AIza|hf_)[A-Za-z0-9_-]{8,}", env_example))

if shutil.which("git"):
    tmp = Path(tempfile.mkdtemp())
    try:
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        shutil.copy(ROOT / ".gitignore", tmp / ".gitignore")

        def ignored(rel):
            f = tmp / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
            return subprocess.run(["git", "check-ignore", "-q", rel], cwd=tmp).returncode == 0

        for rel in (".env", ".env.local", "venv/lib/x.py", "data_source_cuad/data/CUAD.json", "data/raw/ofac/SDN.CSV",
                    "data/eval/results_agent/summary.json", "agent/__pycache__/x.cpython-312.pyc", ".vscode/settings.json"):
            check(f".gitignore ignores {rel}", ignored(rel))
        for rel in (".env.example", "data/eval/gold_questions.jsonl", "eval/baseline/agent_summary.json",
                    "requirements-ci.txt", ".github/workflows/ci.yml", "infra/docker-compose.yml", "risk/fixtures/SDN_sample.csv"):
            check(f".gitignore keeps {rel} tracked", not ignored(rel))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
else:
    print("(git not installed: .gitignore checks skipped)")

wf = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
check("workflow installs the lean CI requirements", "requirements-ci.txt" in wf)
check("workflow runs the secret scan", "scripts/check_no_secrets.py" in wf)
check("workflow discovers every *offline*.py suite", "*offline*.py" in wf)
check("no action pinned to a Node-20-era major (removed from runners 2026-09-16)",
      not re.search(r"actions/checkout@v[1-4]\b|actions/setup-python@v[1-5]\b|docker/build-push-action@v[1-6]\b|docker/setup-buildx-action@v[1-3]\b", wf))
check("stack-smoke job gates on tests + docker-config", re.search(r"needs:\s*\[tests,\s*docker-config\]", wf) is not None)
check("stack-smoke asserts the MCP SQL server is part of /ready", 'checks"].get("mcp_sql")' in wf or 'checks"].get("mcp_sql") == "ok"' in wf)
check("workflow always tears the stack down", "if: always()" in wf and "down -v" in wf)
check("live (database-needing) tests are not picked up by the discovery pattern", all("offline" not in p.name for p in ROOT.rglob("test_*_live.py")))


def pins(path):
    out = {}
    for ln in (ROOT / path).read_text().splitlines():
        ln = ln.split("#")[0].strip()
        if ln and not ln.startswith("-"):
            m = re.match(r"([A-Za-z0-9_.\-]+)\s*(.*)", ln)
            out[m.group(1).lower().replace("_", "-")] = m.group(2).replace(" ", "")
    return out


ci, api_pins = pins("requirements-ci.txt"), pins("requirements-api.txt")
shared = set(ci) & set(api_pins)
check(f"CI and image pin the same versions for shared packages ({len(shared)} shared)", all(ci[k] == api_pins[k] for k in shared))

print(f"\nAll {N} checks passed.")
