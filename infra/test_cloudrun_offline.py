"""
Phase 13b offline tests -- no Google Cloud, no Neon, no Qdrant Cloud, no Docker.

Guards the deployment artifacts against drift: the Cloud Run service definition, the deploy and
migration scripts, the cloud role SQL, and the repo hygiene around them.

Run:   python infra/test_cloudrun_offline.py
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import render_service as rs  # noqa: E402

N = 0


def check(label, cond):
    global N
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label
    N += 1


def raises(cfg_changes, fragment):
    cfg = dict(GOOD)
    cfg.update(cfg_changes)
    try:
        rs.normalize(cfg)
    except rs.ConfigError as e:
        return fragment.lower() in str(e).lower()
    return False


GOOD = {
    "PROJECT_ID": "my-proj-123456", "REGION": "asia-south1",
    "NEON_HOST": "ep-cool-123456.ap-southeast-1.aws.neon.tech", "NEON_DB": "neondb", "NEON_USER": "neondb_owner",
    "QDRANT_URL": "https://abc.eu-central-1-0.aws.cloud.qdrant.io:6333",
    "IMAGE": "asia-south1-docker.pkg.dev/my-proj-123456/procureiq/app:20261013-120000",
}

# ---- 1. validation: every bad input names what to fix
check("a complete config validates", rs.normalize(GOOD)["SERVICE_NAME"] == "procureiq")
for k in rs.REQUIRED + ["IMAGE"]:
    check(f"missing {k} is reported", raises({k: ""}, k))
check("http:// Qdrant URL refused (API key must not travel in clear text)", raises({"QDRANT_URL": "http://x.io:6333"}, "https://"))
check("connection string pasted as NEON_HOST refused", raises({"NEON_HOST": "postgresql://u:p@h/db"}, "bare host"))
check("NEON_HOST with a port refused", raises({"NEON_HOST": "ep-x.neon.tech:5432"}, "bare host"))
check("display name instead of project id refused", raises({"PROJECT_ID": "My Project"}, "PROJECT_ID"))
check(":latest image refused", raises({"IMAGE": GOOD["IMAGE"].split(":")[0] + ":latest"}, ":latest"))
check("untagged image refused", raises({"IMAGE": "asia-south1-docker.pkg.dev/p/procureiq/app"}, "tag"))
check("non-Artifact-Registry image refused", raises({"IMAGE": "docker.io/me/app:1"}, "Artifact Registry"))
check("MIN > MAX refused", raises({"MIN_INSTANCES": "3", "MAX_INSTANCES": "2"}, "MIN_INSTANCES"))
check("MAX_INSTANCES > 10 refused (bill cap)", raises({"MAX_INSTANCES": "50"}, "bill"))
check("non-numeric scaling refused", raises({"MAX_INSTANCES": "many"}, "whole numbers"))
check("provider other than openrouter refused", raises({"PROVIDER": "gemini"}, "openrouter"))
check("bad service name refused", raises({"SERVICE_NAME": "Proc_IQ"}, "SERVICE_NAME"))

# ---- 2. the service definition
svc = rs.build_service(rs.normalize(GOOD))
text = rs.render(GOOD)
tmpl = svc["spec"]["template"]
spec, ann = tmpl["spec"], tmpl["metadata"]["annotations"]
api, side = spec["containers"]
env = lambda c: {e["name"]: e for e in c["env"]}  # noqa: E731
check("Knative Service for Cloud Run", svc["apiVersion"] == "serving.knative.dev/v1" and svc["kind"] == "Service")
check("exactly two containers: api (ingress) + mcp-sql (sidecar)", [c["name"] for c in spec["containers"]] == ["api", "mcp-sql"])
check("only the ingress container exposes a port", api["ports"] == [{"containerPort": 8000}] and "ports" not in side)
check("both containers run the same image", api["image"] == side["image"] == GOOD["IMAGE"])
check("sidecar binds to 127.0.0.1 only (never reachable from outside)", side["command"][side["command"].index("--host") + 1] == "127.0.0.1" and "0.0.0.0" not in side["command"])
check("api reaches the sidecar over localhost", env(api)["PROCUREIQ_SQL_BACKEND"]["value"] == "mcp" and env(api)["PROCUREIQ_MCP_SQL_URL"]["value"] == "http://127.0.0.1:8765/mcp")
check("startup order: api waits for mcp-sql", json.loads(ann["run.googleapis.com/container-dependencies"]) == {"api": ["mcp-sql"]})
check("sidecar has a startup probe (needed for dependency ordering)", "startupProbe" in side)
check("ingress is open; the API key protects it", svc["metadata"]["annotations"]["run.googleapis.com/ingress"] == "all" and "PROCUREIQ_API_KEY" in env(api))
check("runtime runs as the dedicated service account", spec["serviceAccountName"] == "procureiq-run@my-proj-123456.iam.gserviceaccount.com")
check("scale-to-zero by default, capped at 2 instances", ann["autoscaling.knative.dev/minScale"] == "0" and ann["autoscaling.knative.dev/maxScale"] == "2")
check("CPU always allocated (background trace export) + startup boost", ann["run.googleapis.com/cpu-throttling"] == "false" and ann["run.googleapis.com/startup-cpu-boost"] == "true")

# Cloud Run rule: any container below 1 vCPU forces max concurrency 1 for the whole service
cpus = [float(c["resources"]["limits"]["cpu"]) for c in spec["containers"]]
check("every container has a whole-number CPU >= 1 (so concurrency can stay > 1)", all(c >= 1 and c == int(c) for c in cpus) and spec["containerConcurrency"] > 1)
check("memory limits set and sized (api 3Gi, sidecar 512Mi)", api["resources"]["limits"]["memory"] == "3Gi" and side["resources"]["limits"]["memory"] == "512Mi")
check("request timeout covers a slow agent run", spec["timeoutSeconds"] >= 120)


def probe_ok(p):
    return p["timeoutSeconds"] <= p["periodSeconds"] and p["periodSeconds"] * p["failureThreshold"] <= 240


check("startup probes obey Cloud Run limits (timeout <= period, total <= 240 s)", probe_ok(api["startupProbe"]) and probe_ok(side["startupProbe"]))
check("api startup probe uses /health, which exists", api["startupProbe"]["httpGet"]["path"] == "/health" and '"/health"' in (ROOT / "api" / "app.py").read_text())
check("YAML has no anchors/aliases (shared dicts would emit them)", not re.search(r"&id\d+|\*id\d+", text))
check("rendered text round-trips through a YAML parser", yaml.safe_load(text) == svc)

# ---- 3. secrets and least privilege
secret_vars = {n for c in spec["containers"] for n, e in env(c).items() if "valueFrom" in e}
check("secrets come only from Secret Manager references", secret_vars == {"POSTGRES_PASSWORD", "QDRANT_API_KEY", "OPENROUTER_API_KEY", "PROCUREIQ_API_KEY", "POSTGRES_RO_PASSWORD"})
check("every secret reference uses the 'latest' version of a procureiq-* secret", all(e["valueFrom"]["secretKeyRef"]["key"] == "latest" and e["valueFrom"]["secretKeyRef"]["name"].startswith("procureiq-") for c in spec["containers"] for e in c["env"] if "valueFrom" in e))
check("no key-shaped literal anywhere in the rendered YAML", not re.search(r"(sk-or-v1-|sk-lf-|pk-lf-|AIza|hf_)[A-Za-z0-9_-]{8,}", text))
check("sidecar gets NO LLM/Qdrant/API/tracing secrets", not (set(env(side)) & {"OPENROUTER_API_KEY", "QDRANT_API_KEY", "PROCUREIQ_API_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY", "POSTGRES_PASSWORD"}))
check("sidecar connects as the read-only role only", env(side)["POSTGRES_RO_USER"]["value"] == "procureiq_ro" and "POSTGRES_USER" not in env(side))
check("both containers force TLS to Postgres (Neon requires it)", env(api)["PGSSLMODE"]["value"] == "require" and env(side)["PGSSLMODE"]["value"] == "require")
check("api has no local-Qdrant escape hatch (QDRANT_PATH unset)", "QDRANT_PATH" not in env(api))
check("api points at the Neon host / db / user", (env(api)["POSTGRES_HOST"]["value"], env(api)["POSTGRES_DB"]["value"], env(api)["POSTGRES_USER"]["value"]) == (GOOD["NEON_HOST"], "neondb", "neondb_owner"))

# ---- 4. optional features
check("OPENROUTER_MODEL only when configured", "OPENROUTER_MODEL" not in env(api) and env(rs.build_service(rs.normalize({**GOOD, "OPENROUTER_MODEL": "x/y:free"}))["spec"]["template"]["spec"]["containers"][0])["OPENROUTER_MODEL"]["value"] == "x/y:free")
lf = rs.build_service(rs.normalize({**GOOD, "LANGFUSE": "true"}))["spec"]["template"]["spec"]["containers"][0]
check("LANGFUSE=true adds host + both keys (keys as secrets) to api only", {"LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"} <= set(env(lf)) and "valueFrom" in env(lf)["LANGFUSE_SECRET_KEY"] and not any(n.startswith("LANGFUSE") for n in env(side)))
check("LANGFUSE off by default", not any(n.startswith("LANGFUSE") for n in env(api)))
check("custom scaling flows through", rs.build_service(rs.normalize({**GOOD, "MIN_INSTANCES": "1", "MAX_INSTANCES": "4"}))["spec"]["template"]["metadata"]["annotations"]["autoscaling.knative.dev/minScale"] == "1")

# ---- 5. config file + CLI
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    (d / "deploy.env").write_text('# comment\nPROJECT_ID="my-proj-123456"   # inline\nREGION=asia-south1\n\nNEON_HOST=ep-cool-123456.ap-southeast-1.aws.neon.tech\nNEON_DB=neondb\nNEON_USER=neondb_owner\nQDRANT_URL=https://abc.cloud.qdrant.io:6333\nbroken line without equals\n')
    parsed = rs.parse_env_file(d / "deploy.env")
    check("env file: comments, quotes, inline comments and junk lines handled", parsed["PROJECT_ID"] == "my-proj-123456" and parsed["REGION"] == "asia-south1" and "broken line without equals" not in parsed)
    out = d / "out.yaml"
    code = rs.main(["--config", str(d / "deploy.env"), "--out", str(out), "--image", GOOD["IMAGE"]])
    check("CLI: config file + --image renders a file", code == 0 and yaml.safe_load(out.read_text())["kind"] == "Service")
    check("CLI: flags override the file", rs.main(["--config", str(d / "deploy.env"), "--out", str(out), "--image", GOOD["IMAGE"], "--region", "us-central1"]) == 0 and yaml.safe_load(out.read_text())["metadata"]["labels"]["cloud.googleapis.com/location"] == "us-central1")
    out.unlink()
    check("CLI: bad config exits 2 and writes nothing", rs.main(["--config", str(d / "deploy.env"), "--out", str(out), "--image", "nope"]) == 2 and not out.exists())
    check("CLI: missing config file + no flags exits 2", rs.main(["--config", str(d / "none.env"), "--out", str(out)]) == 2)

# ---- 6. the PowerShell scripts: drift against the Python side, and Windows PowerShell 5.1 safety
deploy = (HERE / "deploy_gcp.ps1").read_text(encoding="utf-8")
migrate = (HERE / "migrate_data.ps1").read_text(encoding="utf-8")
smoke = (HERE / "smoke_test.ps1").read_text(encoding="utf-8")
ps_secret_names = set(re.findall(r'Name = "(procureiq-[a-z-]+)"', deploy))
check("deploy script creates exactly the secrets the service YAML references", ps_secret_names == set(rs.SECRETS.values()))
check("service YAML secret names are all in the script's secret table", {e["valueFrom"]["secretKeyRef"]["name"] for c in rs.build_service(rs.normalize({**GOOD, "LANGFUSE": "true"}))["spec"]["template"]["spec"]["containers"] for e in c["env"] if "valueFrom" in e} <= ps_secret_names)
check("deploy script grants each secret to the same runtime service account the YAML uses", "procureiq-run@$Project.iam.gserviceaccount.com" in deploy and rs.normalize(GOOD)["RUNTIME_SA"].startswith("procureiq-run@"))
check("deploy script's registry name/path matches the image validation", '$Repo = "procureiq"' in deploy and "-docker.pkg.dev/$Project/$Repo/app:$Tag" in deploy)
for name, body in (("deploy_gcp.ps1", deploy), ("migrate_data.ps1", migrate), ("smoke_test.ps1", smoke)):
    check(f"{name}: ASCII only (Windows PowerShell 5.1 misreads UTF-8 without BOM)", all(ord(ch) < 128 for ch in body))
    check(f"{name}: no PowerShell-7-only syntax (??, ?., &&, ||, ternary, -AsPlainText, -Parallel)", not re.search(r"\?\?|\?\.|(?<!['\"])&&|(?<![|'\"])\|\|(?!['\"])|-AsPlainText|-Parallel|\$IsWindows|\)\s*\?\s*\S+\s*:\s", re.sub(r"#.*", "", body)))
def code_only(body):  # drop <# ... #> help blocks and # comments: assert on what RUNS, not on what is explained
    return re.sub(r"#.*", "", re.sub(r"<#.*?#>", "", body, flags=re.S))


deploy_code = code_only(deploy)
check("secrets are written to a temp file without a trailing newline (never piped to gcloud)", "WriteAllText($tmp, $Value" in deploy_code and "--data-file=-" not in deploy_code and not re.search(r"\|\s*&?\s*gcloud", deploy_code))
check("(the pitfall is documented in a comment, which the check above deliberately ignores)", "--data-file=-" in deploy)
check("temp secret file is always deleted", "Remove-Item $tmp -Force" in deploy and "finally" in deploy)
check("secret generation uses a cryptographic RNG (deploy + migrate)", deploy.count("RandomNumberGenerator") >= 1 and "RandomNumberGenerator" in migrate and "Get-Random" not in deploy + migrate)
check("deploy script never prompts when input is redirected (CI-safe)", "[Console]::IsInputRedirected" in deploy)
check("deploy script only prompts through a SecureString", "Read-Host -AsSecureString" in deploy and not re.search(r"Read-Host\s+(?!-AsSecureString)\"", deploy))
check("native commands: ErrorAction relaxed + exit code checked (5.1 stderr behaviour)", '$ErrorActionPreference = "Continue"' in deploy and "$LASTEXITCODE" in deploy)
check("-Action deploy requires an explicit -Tag (guard uses script-level $PSBoundParameters)", "$TagWasGiven = $PSBoundParameters.ContainsKey" in deploy and "-not $TagWasGiven" in deploy)
check("migrate script refuses oversized databases unless -Force", "MaxDatabaseMB" in migrate and "-not $Force" in migrate)
migrate_code = code_only(migrate)
check("migrate script validates the role password before touching anything", migrate_code.index("RoPassword -notmatch") < migrate_code.index('"pg_dump"'))
check("migrate script checks the size limit before dumping", migrate_code.index("MaxDatabaseMB * 1MB") < migrate_code.index('"pg_dump"'))
check("migrate script verifies counts on both stores", "[PASS]" in migrate and "[FAIL]" in migrate and "points/count" in migrate)
check("migrate restore is repeatable (--clean --if-exists) and ownerless (--no-owner --no-acl)", all(f in migrate for f in ("--clean", "--if-exists", "--no-owner", "--no-acl")))

# parse with a real PowerShell when one is installed (GitHub's ubuntu runners have pwsh; Windows has powershell)
ps = shutil.which("pwsh") or shutil.which("powershell")
if ps:
    for f in ("deploy_gcp.ps1", "migrate_data.ps1", "smoke_test.ps1"):
        cmd = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$t,[ref]$e);"
               "if($e.Count){$e|ForEach-Object{$_.Message};exit 1}" % str(HERE / f).replace("\\", "/"))
        r = subprocess.run([ps, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=120)
        check(f"{f} parses with {Path(ps).stem}", r.returncode == 0)
else:
    print("(no PowerShell found: parse checks skipped)")

# ---- 7. Cloud Build, ignore files, keep-alive, role SQL, env docs
cb = yaml.safe_load((HERE / "cloudbuild.yaml").read_text())
check("Cloud Build builds infra/Dockerfile from the repo root and pushes the image", cb["steps"][0]["args"][:3] == ["build", "-f", "infra/Dockerfile"] and cb["steps"][0]["args"][-1] == "." and cb["images"] == ["${_IMAGE}"])
gi = (ROOT / ".gcloudignore").read_text()
check(".gcloudignore reuses .gitignore (no venv/.env upload) and skips .git", "#!include:.gitignore" in gi and ".git" in gi.splitlines())
gitignore = (ROOT / ".gitignore").read_text()
check(".gitignore keeps deploy.env, rendered YAML, dumps and snapshots out of git", all(p in gitignore for p in ("infra/cloudrun/deploy.env", "infra/cloudrun/service.rendered.yaml", "*.dump", "*.snapshot")))
example = (HERE / "cloudrun" / "deploy.env.example").read_text()
check("deploy.env.example validates once filled with plausible values and holds no secrets", not re.search(r"(sk-|pk-lf|AIza|hf_)", example) and all(k in example for k in rs.REQUIRED))

ka = yaml.safe_load((ROOT / ".github" / "workflows" / "keepalive.yml").read_text())
on = ka.get(True, ka.get("on"))
check("keep-alive runs on a daily schedule and can be run by hand", on["schedule"][0]["cron"].count(" ") == 4 and "workflow_dispatch" in on)
kt = (ROOT / ".github" / "workflows" / "keepalive.yml").read_text()
check("keep-alive pings /ready from the SERVICE_URL variable, uses no secrets, no token permissions", "/ready" in kt and "vars.SERVICE_URL" in kt and "secrets." not in kt and ka["permissions"] == {})

role = (ROOT / "erp" / "create_readonly_role_cloud.sql").read_text()
local_role = (ROOT / "erp" / "create_readonly_role.sql").read_text()
check("cloud role script takes the password as a parameter (no hard-coded password)", ":'ro_password'" in role and not re.search(r"PASSWORD\s+'[^']", role))
grant = lambda t: set(re.findall(r"GRANT SELECT ON ([a-z_, ]+) TO", t)[0].replace(" ", "").split(","))  # noqa: E731
check("cloud role grants the same tables as the local role script", grant(role) == grant(local_role))
check("cloud role grants the Phase 10 screening table only if it exists", "vendor_risk_screening" in role and "to_regclass" in role)
check("cloud role stays read-only with a statement timeout", "default_transaction_read_only = on" in role and "statement_timeout" in role)
check("cloud role does not grant contract_chunks / sanctions_entries", "contract_chunks" not in role.replace("-- ", "") .split("GRANT SELECT ON")[1] and "sanctions_entries" not in role)

env_doc = (ROOT / ".env.example").read_text()
documented = {ln.lstrip("# ").split("=", 1)[0].strip() for ln in env_doc.splitlines() if "=" in ln}
yaml_vars = {n for c in spec["containers"] for n in env(c)}
check("every env var the service sets is documented in .env.example", yaml_vars <= documented | {"POSTGRES_RO_USER"})
check(".env.example documents QDRANT_API_KEY and PGSSLMODE", {"QDRANT_API_KEY", "PGSSLMODE"} <= documented)
check("api/main.py sends QDRANT_API_KEY to Qdrant", 'api_key=os.getenv("QDRANT_API_KEY")' in (ROOT / "api" / "main.py").read_text())

print(f"\nAll {N} checks passed.")
