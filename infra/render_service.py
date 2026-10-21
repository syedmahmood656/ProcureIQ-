"""
Phase 13b — build the Cloud Run service definition (YAML) for ProcureIQ.

    python infra/render_service.py                  # reads infra/cloudrun/deploy.env, writes service.rendered.yaml
    python infra/render_service.py --image REGION-docker.pkg.dev/PROJECT/procureiq/app:TAG

ONE Cloud Run service with TWO containers that share a network namespace (so they talk over localhost):
    api      (ingress, port 8000, 2 vCPU / 3 GiB)   the FastAPI app + models
    mcp-sql  (sidecar, 1 vCPU / 512 MiB)            the Phase 9 MCP SQL server, listening on 127.0.0.1 ONLY
The SQL server is therefore never reachable from the internet -- it needs no authentication layer
because nothing outside the instance can connect to it. Same security property as docker-compose.

Secrets never appear in the YAML: they are referenced from Google Secret Manager (secretKeyRef).
The sidecar receives ONLY database settings -- no LLM key, no Qdrant key, no API key.

Config (deploy.env, KEY=VALUE per line; CLI flags override):
    PROJECT_ID  REGION  NEON_HOST  NEON_DB  NEON_USER  QDRANT_URL        (required)
    SERVICE_NAME=procureiq  MIN_INSTANCES=0  MAX_INSTANCES=2  MAX_CONCURRENT=2
    OPENROUTER_MODEL=       LANGFUSE=false  LANGFUSE_HOST=https://cloud.langfuse.com
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "cloudrun" / "deploy.env"
DEFAULT_OUT = HERE / "cloudrun" / "service.rendered.yaml"

# Secret Manager secret names (created by infra/deploy_gcp.ps1 -Action setup)
SECRETS = {
    "postgres_password": "procureiq-postgres-password",
    "postgres_ro_password": "procureiq-postgres-ro-password",
    "qdrant_api_key": "procureiq-qdrant-api-key",
    "api_key": "procureiq-api-key",
    "openrouter_api_key": "procureiq-openrouter-api-key",
    "langfuse_public_key": "procureiq-langfuse-public-key",
    "langfuse_secret_key": "procureiq-langfuse-secret-key",
}
REQUIRED = ["PROJECT_ID", "REGION", "NEON_HOST", "NEON_DB", "NEON_USER", "QDRANT_URL"]
RO_USER = "procureiq_ro"  # the role created by erp/create_readonly_role_cloud.sql


class ConfigError(ValueError):
    pass


def parse_env_file(path: Path) -> dict:
    cfg = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return cfg


def normalize(cfg: dict) -> dict:
    """Apply defaults and validate. Raises ConfigError with a message that says what to fix."""
    c = {k: str(v).strip() for k, v in cfg.items() if v is not None}
    missing = [k for k in REQUIRED + ["IMAGE"] if not c.get(k)]
    if missing:
        raise ConfigError("missing required setting(s): " + ", ".join(missing))
    c.setdefault("SERVICE_NAME", "procureiq")
    c.setdefault("MIN_INSTANCES", "0")
    c.setdefault("MAX_INSTANCES", "2")
    c.setdefault("MAX_CONCURRENT", "2")
    c.setdefault("LANGFUSE", "false")
    c.setdefault("LANGFUSE_HOST", "https://cloud.langfuse.com")
    c.setdefault("RUNTIME_SA", f"procureiq-run@{c['PROJECT_ID']}.iam.gserviceaccount.com")
    c.setdefault("PROVIDER", "openrouter")

    if c["PROVIDER"] != "openrouter":
        raise ConfigError("only PROVIDER=openrouter is wired for the cloud deployment")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", c["SERVICE_NAME"]):
        raise ConfigError("SERVICE_NAME must be lowercase letters, digits and dashes")
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", c["PROJECT_ID"]):
        raise ConfigError("PROJECT_ID looks wrong (use the project ID, not its display name)")
    if "://" in c["NEON_HOST"] or "/" in c["NEON_HOST"] or ":" in c["NEON_HOST"] or "." not in c["NEON_HOST"]:
        raise ConfigError("NEON_HOST must be a bare host name like ep-xxx-123456.ap-southeast-1.aws.neon.tech")
    if not c["QDRANT_URL"].startswith("https://"):
        raise ConfigError("QDRANT_URL must start with https:// (Qdrant Cloud requires TLS for API-key auth)")
    try:
        mn, mx, conc = int(c["MIN_INSTANCES"]), int(c["MAX_INSTANCES"]), int(c["MAX_CONCURRENT"])
    except ValueError:
        raise ConfigError("MIN_INSTANCES, MAX_INSTANCES and MAX_CONCURRENT must be whole numbers")
    if not 0 <= mn <= mx:
        raise ConfigError("need 0 <= MIN_INSTANCES <= MAX_INSTANCES")
    if mx > 10:
        raise ConfigError("MAX_INSTANCES > 10 refused: it caps your worst-case bill")
    if conc < 1:
        raise ConfigError("MAX_CONCURRENT must be at least 1")
    img = c["IMAGE"]
    if "-docker.pkg.dev/" not in img or ":" not in img.rsplit("/", 1)[-1] and "@sha256:" not in img:
        raise ConfigError("IMAGE must be an Artifact Registry image with a tag, e.g. asia-south1-docker.pkg.dev/PROJECT/procureiq/app:20261013-1200")
    if img.endswith(":latest"):
        raise ConfigError("IMAGE must not use :latest (use a unique tag so every deploy is a distinct, rollback-able revision)")
    c["_langfuse"] = c["LANGFUSE"].lower() in ("1", "true", "yes", "on")
    return c


def _secret(name: str, secret: str) -> dict:
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": "latest"}}}


def _plain(name: str, value) -> dict:
    return {"name": name, "value": str(value)}


def build_service(c: dict) -> dict:
    def db_plain() -> list:
        # a FRESH list per container: sharing dict objects makes the YAML emitter write &anchors/*aliases
        return [
            _plain("POSTGRES_HOST", c["NEON_HOST"]),
            _plain("POSTGRES_PORT", "5432"),
            _plain("POSTGRES_DB", c["NEON_DB"]),
            _plain("PGSSLMODE", "require"),  # libpq reads this: Neon only accepts TLS connections
        ]

    api_env = db_plain() + [
        _plain("POSTGRES_USER", c["NEON_USER"]),
        _secret("POSTGRES_PASSWORD", SECRETS["postgres_password"]),  # /ready probes Postgres with the main role
        _plain("QDRANT_URL", c["QDRANT_URL"]),
        _secret("QDRANT_API_KEY", SECRETS["qdrant_api_key"]),
        _plain("PROCUREIQ_SQL_BACKEND", "mcp"),
        _plain("PROCUREIQ_MCP_SQL_URL", "http://127.0.0.1:8765/mcp"),
        _plain("PROCUREIQ_PROVIDER", c["PROVIDER"]),
        _secret("OPENROUTER_API_KEY", SECRETS["openrouter_api_key"]),
        _secret("PROCUREIQ_API_KEY", SECRETS["api_key"]),  # the API is public: it MUST be key-protected
        _plain("PROCUREIQ_MAX_CONCURRENT", c["MAX_CONCURRENT"]),
    ]
    if c.get("OPENROUTER_MODEL"):
        api_env.append(_plain("OPENROUTER_MODEL", c["OPENROUTER_MODEL"]))
    if c["_langfuse"]:
        api_env += [
            _plain("LANGFUSE_HOST", c["LANGFUSE_HOST"]),
            _secret("LANGFUSE_PUBLIC_KEY", SECRETS["langfuse_public_key"]),
            _secret("LANGFUSE_SECRET_KEY", SECRETS["langfuse_secret_key"]),
        ]

    sidecar_env = db_plain() + [
        _plain("POSTGRES_RO_USER", RO_USER),
        _secret("POSTGRES_RO_PASSWORD", SECRETS["postgres_ro_password"]),
    ]

    return {
        "apiVersion": "serving.knative.dev/v1",
        "kind": "Service",
        "metadata": {
            "name": c["SERVICE_NAME"],
            "labels": {"cloud.googleapis.com/location": c["REGION"]},
            "annotations": {"run.googleapis.com/ingress": "all"},
        },
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "autoscaling.knative.dev/minScale": c["MIN_INSTANCES"],
                        "autoscaling.knative.dev/maxScale": c["MAX_INSTANCES"],
                        # CPU stays allocated while the instance lives, so background work (Langfuse span
                        # export) is not frozen between requests.
                        "run.googleapis.com/cpu-throttling": "false",
                        "run.googleapis.com/startup-cpu-boost": "true",
                        # start the SQL sidecar first; `api` starts once its startup probe passes
                        "run.googleapis.com/container-dependencies": json.dumps({"api": ["mcp-sql"]}),
                    }
                },
                "spec": {
                    "serviceAccountName": c["RUNTIME_SA"],
                    "containerConcurrency": 8,
                    "timeoutSeconds": 300,
                    "containers": [
                        {
                            "name": "api",
                            "image": c["IMAGE"],
                            "ports": [{"containerPort": 8000}],
                            "env": api_env,
                            "resources": {"limits": {"cpu": "2", "memory": "3Gi"}},
                            "startupProbe": {
                                "httpGet": {"path": "/health", "port": 8000},
                                "periodSeconds": 5, "timeoutSeconds": 3, "failureThreshold": 40,
                            },
                        },
                        {
                            "name": "mcp-sql",
                            "image": c["IMAGE"],
                            "command": ["python", "mcp_servers/sql_server.py", "--transport", "http",
                                        "--host", "127.0.0.1", "--port", "8765"],
                            "env": sidecar_env,
                            "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
                            "startupProbe": {
                                "tcpSocket": {"port": 8765},
                                "periodSeconds": 2, "timeoutSeconds": 1, "failureThreshold": 30,
                            },
                        },
                    ],
                },
            }
        },
    }


def render(cfg: dict) -> str:
    return yaml.safe_dump(build_service(normalize(cfg)), sort_keys=False, default_flow_style=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--image", help="Artifact Registry image with a unique tag (required)")
    for k in ("project-id", "region", "neon-host", "neon-db", "neon-user", "qdrant-url", "service-name",
              "min-instances", "max-instances", "max-concurrent", "openrouter-model", "langfuse", "runtime-sa"):
        ap.add_argument("--" + k)
    args = ap.parse_args(argv)

    cfg = parse_env_file(args.config) if args.config.exists() else {}
    for k, v in vars(args).items():
        if k not in ("config", "out") and v is not None:
            cfg[k.upper()] = v
    try:
        text = render(cfg)
    except ConfigError as e:
        print(f"config error: {e}\n(settings come from {args.config} and --flags; see --help)")
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
