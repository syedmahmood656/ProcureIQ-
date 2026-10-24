"""
Phase 13c offline tests: free public access from your own PC (infra/go_public.ps1 + tunnel services).

  * static checks: compose services, .env.example docs, script hygiene (run everywhere)
  * behaviour checks: runs the REAL go_public.ps1 under PowerShell against a fake `docker` and a fake
    API server (Linux/macOS with pwsh only -- GitHub's ubuntu runners have it; skipped on Windows)

No Docker, no tunnel, no internet needed.   Run:   python infra/test_public_offline.py
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
N = 0


def check(label, cond):
    global N
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label
    N += 1


script = (HERE / "go_public.ps1").read_text(encoding="utf-8")
smoke = (HERE / "smoke_test.ps1").read_text(encoding="utf-8")
compose = yaml.safe_load((HERE / "docker-compose.yml").read_text(encoding="utf-8"))
svc = compose["services"]


def code_only(body):
    return re.sub(r"#.*", "", re.sub(r"<#.*?#>", "", body, flags=re.S))


# ---------------------------------------------------------------- 1. compose: the tunnel services
t, n = svc["tunnel"], svc["ngrok"]
check("tunnel is opt-in: profile 'public' only", t["profiles"] == ["public"])
check("ngrok is opt-in: profile 'public-ngrok' only", n["profiles"] == ["public-ngrok"])
check("tunnel image is pinned to a release (not :latest)", re.fullmatch(r"cloudflare/cloudflared:\d{4}\.\d+\.\d+", t["image"]) is not None)
check("tunnel targets the API over the compose network and never self-updates", t["command"] == ["tunnel", "--no-autoupdate", "--url", "http://api:8000"])
check("ngrok targets api:8000 with its static domain from the environment", n["command"][0] == "http" and n["command"][-1] == "api:8000" and n["command"][1].startswith("--url=${NGROK_DOMAIN"))
check("neither tunnel publishes a port (outbound connections only)", "ports" not in t and "ports" not in n)
check("both wait for a healthy api", t["depends_on"]["api"]["condition"] == "service_healthy" and n["depends_on"]["api"]["condition"] == "service_healthy")
check("the tunnel gets no environment at all; ngrok gets only its token", "environment" not in t and n["environment"] == ["NGROK_AUTHTOKEN"])
check("default dev profile still starts only postgres + qdrant", {k for k, v in svc.items() if "profiles" not in v} == {"postgres", "qdrant"})
env_doc = (ROOT / ".env.example").read_text(encoding="utf-8")
documented = {ln.lstrip("# ").split("=", 1)[0].strip() for ln in env_doc.splitlines() if "=" in ln}
check(".env.example documents NGROK_AUTHTOKEN, NGROK_DOMAIN and PROCUREIQ_API_KEY", {"NGROK_AUTHTOKEN", "NGROK_DOMAIN", "PROCUREIQ_API_KEY"} <= documented)

# ---------------------------------------------------------------- 2. script hygiene
body = code_only(script)
check("go_public.ps1: ASCII only (Windows PowerShell 5.1)", all(ord(c) < 128 for c in script))
check("go_public.ps1: no PowerShell-7-only syntax", not re.search(r"\?\?|\?\.|(?<!['\"])&&|(?<![|'\"])\|\|(?!['\"])|-AsPlainText|-Parallel|\$IsWindows|\)\s*\?\s*\S+\s*:\s", body))
check("API key is ensured BEFORE anything is started", body.index("Set-EnvValue \"PROCUREIQ_API_KEY\"") < body.index('@("up", "-d")'))
check("an API key of fewer than 16 characters is refused", "-lt 16" in body)
check("keys come from a cryptographic RNG", "RandomNumberGenerator" in body and "Get-Random" not in body)
check(".env is edited in place, UTF-8 without BOM", "UTF8Encoding($false)" in body and "WriteAllText" in body)
check("never runs `down` or removes volumes (your data is safe)", "down" not in re.findall(r'@\("([a-z]+)"', body) and "-v" not in re.findall(r'"(-v)"', body))
check("the other provider's tunnel is stopped before starting (never two public paths)", body.index("$otherService") < body.index('@("up", "-d")'))
check("url detection ignores Cloudflare's own api.trycloudflare.com and takes the newest match", '-ne "api"' in body and "[-1]" in body)
check("-Stop only stops/removes the tunnel services", re.search(r'@\("stop", "tunnel", "ngrok"\)', body) and re.search(r'@\("rm", "-f", "tunnel", "ngrok"\)', body))
check("verification keeps the real error text (no silent catch around the check)", "Last result:" in body and "Detail" in body and "Test-PublicReady" in body)
check("a restart is attempted only for an auto-detected cloudflare URL", "-not $res.Ok -and $Provider -eq \"cloudflare\" -and -not $PublicUrl" in body)
check("the tunnel restart never touches other services (--no-deps)", '"up", "-d", "--no-deps", "tunnel"' in body)
check("the API must be healthy before a tunnel restart is attempted", body.index('"{{.State.Health.Status}}", "procureiq_api"') < body.index('"rm", "-f", "-s", "tunnel"'))
check("busy-port errors get a plain-language hint", "Get-PortHint" in body and "findstr :8000" in script)
check("the URL pattern is a parameter (survives a Cloudflare domain change)", "[string]$TunnelUrlPattern" in script)
check("smoke test accepts extra headers (ngrok interstitial bypass)", "[hashtable]$ExtraHeaders" in smoke and "-Headers $headers" in smoke)
ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
check("CI validates the tunnel profiles of the compose file too", "--profile public --profile public-ngrok config --quiet" in ci)
check("an outside /ready check runs before the smoke test", body.index("Wait-PublicReady $url $extra") < body.index('(Join-Path $ScriptDir "smoke_test.ps1")'))

# ---- Windows PowerShell 5.1 rule: $PSScriptRoot is EMPTY inside a param() block (PowerShell 7 fills it in,
# so tests run on pwsh cannot see this bug). Resolve script-relative defaults in the body instead.
def param_block(text):
    m = re.search(r"(?im)^\s*param\s*\(", text)
    if not m:
        return ""
    i, depth, quote = m.end(), 1, None
    while i < len(text) and depth:
        c = text[i]
        if quote:
            quote = None if c == quote else quote
        elif c in "\"'":
            quote = c
        elif c == "#":
            i = text.find("\n", i) - 1 if text.find("\n", i) != -1 else len(text)
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return text[m.start():i]


for ps1 in sorted(HERE.glob("*.ps1")):
    txt = ps1.read_text(encoding="utf-8")
    pb = param_block(txt)
    check(f"{ps1.name}: param() block found and parsed", pb.lower().lstrip().startswith("param"))
    check(f"{ps1.name}: no $PSScriptRoot / $MyInvocation / $PSCommandPath inside param() (empty on Windows PowerShell 5.1)", not re.search(r"\$PSScriptRoot|\$MyInvocation|\$PSCommandPath", pb))
for name in ("go_public.ps1", "deploy_gcp.ps1", "migrate_data.ps1"):
    txt = (HERE / name).read_text(encoding="utf-8")
    check(f"{name}: resolves the script folder in the body, with a fallback", "$ScriptDir = $PSScriptRoot" in txt and "$MyInvocation.MyCommand.Path" in txt)
check("param_block() really reads past parentheses/comments/strings in a param block", "ReadyTimeoutS" in param_block(script) and "VerifyTimeoutS" in param_block(script))

ps = shutil.which("pwsh") or shutil.which("powershell")
if ps:
    for f in ("go_public.ps1", "smoke_test.ps1"):
        cmd = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$t,[ref]$e);"
               "if($e.Count){$e|ForEach-Object{$_.Message};exit 1}" % str(HERE / f).replace("\\", "/"))
        r = subprocess.run([ps, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=120)
        check(f"{f} parses with {Path(ps).stem}", r.returncode == 0)

# ---------------------------------------------------------------- 3. behaviour (posix + pwsh)
if not (os.name == "posix" and shutil.which("pwsh") and shutil.which("bash")):
    print("(behaviour checks skipped: they need Linux/macOS with pwsh and bash)")
    print(f"\nAll {N} checks passed.")
    sys.exit(0)

SHIM = r"""#!/bin/bash
echo "$*" >> "$FAKE_DIR/calls.log"
case "$1" in
  info)    [ -n "${FAKE_INFO_FAIL:-}" ] && exit 1; exit 0 ;;
  compose) if [[ " $* " == *" up "* ]]; then
             (grep -h '^PROCUREIQ_API_KEY=' "$FAKE_ENV" || echo "NOKEY") | tail -1 >> "$FAKE_DIR/key_at_up.log"
             if [ -n "${FAKE_UP_FAIL:-}" ] || { [ -n "${FAKE_UP_FAIL_NODEPS:-}" ] && [[ " $* " == *" --no-deps "* ]]; }; then
               echo "Error response from daemon: failed to set up container networking: Bind for 0.0.0.0:8000 failed: port is already allocated"; exit 1; fi
           fi
           if [[ " $* " == *" rm "* && " $* " == *" tunnel"* && -f "$FAKE_DIR/tunnel_after.log" ]]; then cp "$FAKE_DIR/tunnel_after.log" "$FAKE_DIR/tunnel.log"; fi
           exit 0 ;;
  inspect) n=$(( $(cat "$FAKE_DIR/inspect_n" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE_DIR/inspect_n"
           if [ -n "${FAKE_UNHEALTHY_AFTER:-}" ] && [ $n -gt "$FAKE_UNHEALTHY_AFTER" ]; then echo "unhealthy"; else echo "${FAKE_HEALTH:-healthy}"; fi
           exit 0 ;;
  logs)    cat "$FAKE_DIR/tunnel.log" 2>/dev/null; exit 0 ;;
esac
exit 0
"""


class FakeApi(BaseHTTPRequestHandler):
    ready = True
    key = ""

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/ready":
            if FakeApi.ready:
                self._send(200, {"status": "ready", "checks": {"postgres": "ok", "qdrant": "ok", "mcp_sql": "ok"}})
            else:
                self._send(503, {"status": "not_ready"})
        else:
            self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.headers.get("X-API-Key") != FakeApi.key:
            return self._send(401, {"detail": "bad key"})
        route = "rag" if "contract" in body.get("question", "").lower() else "sql"
        self._send(200, {"report": {"route": route, "verdict": "accept", "confidence": "high"}})


class DeadTunnel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(530)   # what Cloudflare answers for a quick tunnel that is not running
        self.send_header("Content-Length", "0")
        self.end_headers()


class BadGateway(DeadTunnel):
    def do_GET(self):
        self.send_response(502)   # Cloudflare: the tunnel is up but cannot reach the origin
        self.send_header("Content-Length", "0")
        self.end_headers()


bad = HTTPServer(("127.0.0.1", 0), BadGateway)
threading.Thread(target=bad.serve_forever, daemon=True).start()
BAD502 = f"http://127.0.0.1:{bad.server_port}"
dead = HTTPServer(("127.0.0.1", 0), DeadTunnel)
threading.Thread(target=dead.serve_forever, daemon=True).start()
DEAD = f"http://127.0.0.1:{dead.server_port}"
server = HTTPServer(("127.0.0.1", 0), FakeApi)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_port}"

work = Path(tempfile.mkdtemp())
shim_dir = work / "bin"
shim_dir.mkdir()
(shim_dir / "docker").write_text(SHIM)
(shim_dir / "docker").chmod((shim_dir / "docker").stat().st_mode | stat.S_IEXEC)
FAKE = work / "fake"


def run(*args, env_text="OPENROUTER_API_KEY=x\nPOSTGRES_USER=postgres\n", env_exists=True, extra_env=None, tunnel_log="", tunnel_log_after=None, timeout=120):
    shutil.rmtree(FAKE, ignore_errors=True)
    FAKE.mkdir()
    (FAKE / "tunnel.log").write_text(tunnel_log)
    if tunnel_log_after is not None:
        (FAKE / "tunnel_after.log").write_text(tunnel_log_after)
    envfile = work / ".env"
    if env_exists:
        envfile.write_bytes(env_text.encode())
    elif envfile.exists():
        envfile.unlink()
    env = dict(os.environ, PATH=f"{shim_dir}:{os.environ['PATH']}", FAKE_DIR=str(FAKE), FAKE_ENV=str(envfile), DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1")
    env.pop("PROCUREIQ_API_KEY", None)
    env.update(extra_env or {})
    r = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-File", str(HERE / "go_public.ps1"), "-EnvFile", str(envfile),
                        "-ComposeFile", str(HERE / "docker-compose.yml"), *args], capture_output=True, text=True, env=env, timeout=timeout)
    calls = (FAKE / "calls.log").read_text().splitlines() if (FAKE / "calls.log").exists() else []
    keys_at_up = (FAKE / "key_at_up.log").read_text().splitlines() if (FAKE / "key_at_up.log").exists() else []
    return r.returncode, r.stdout + r.stderr, calls, keys_at_up, envfile.read_text() if envfile.exists() else None


def ups(calls):
    return [c for c in calls if " up -d" in c]


CF_LOG = """2026-10-20T09:00:01Z INF Thank you for trying Cloudflare Tunnel. Doing so, without a Cloudflare account, is a quick way to experiment and try it out.
2026-10-20T09:00:01Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-10-20T09:00:02Z ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp: lookup api.trycloudflare.com: no such host
2026-10-20T09:00:09Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-10-20T09:00:11Z INF +--------------------------------------------------------------------------------------------+
2026-10-20T09:00:11Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
2026-10-20T09:00:11Z INF |  https://rivers-catalog-pending-smile.trycloudflare.com                                    |
2026-10-20T09:00:11Z INF +--------------------------------------------------------------------------------------------+
2026-10-20T09:00:15Z INF Registered tunnel connection connIndex=0 location=bom01 protocol=quic
"""
CF_LOG_TRAILING_ERROR = CF_LOG + '2026-10-20T09:40:00Z ERR failed to refresh quick Tunnel: Post "https://api.trycloudflare.com/tunnel": context deadline exceeded\n'
CF_LOG_RESTARTED = CF_LOG + CF_LOG.replace("rivers-catalog-pending-smile", "kingston-inside-best-graphic").replace("09:00", "11:30")

# ---- precondition failures: nothing may be started
code, out, calls, _, _ = run("-NoVerify", extra_env={"FAKE_INFO_FAIL": "1"})
check("Docker not running -> clear message, nothing started", code != 0 and "Docker is not running" in out and not ups(calls))
code, out, calls, _, _ = run("-NoVerify", env_exists=False)
check("no .env -> tells you how to create it, nothing started", code != 0 and "Copy-Item .env.example .env" in out and not ups(calls))

if not (ROOT / ".env").exists():
    shutil.rmtree(FAKE, ignore_errors=True); FAKE.mkdir()
    env0 = dict(os.environ, PATH=f"{shim_dir}:{os.environ['PATH']}", FAKE_DIR=str(FAKE), DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1")
    r0 = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-File", str(HERE / "go_public.ps1"), "-NoVerify"], capture_output=True, text=True, env=env0, timeout=60)
    check("no -EnvFile given: the default is <repo root>/.env (and a missing file is reported with that path)", r0.returncode != 0 and str(ROOT / ".env") in (r0.stdout + r0.stderr).replace("\\", "/").replace(str(ROOT).replace("\\", "/") + "/.env", str(ROOT / ".env")))
else:
    print("(default-path check skipped: a real .env exists in the repo root and must not be touched)")

# ---- the API key rules
code, out, calls, keys, envtxt = run("-NoVerify", tunnel_log=CF_LOG)
key = re.search(r"^PROCUREIQ_API_KEY=([0-9a-f]+)$", envtxt, re.M)
check("no key in .env -> a 48-hex-char key is generated and saved", code == 0 and key is not None and len(key.group(1)) == 48)
check("the key is in .env BEFORE the stack is started (so the API never runs open)", keys == [f"PROCUREIQ_API_KEY={key.group(1)}"])
check("the other .env lines are untouched", envtxt.startswith("OPENROUTER_API_KEY=x\nPOSTGRES_USER=postgres\n"))
check("the new key is shown once", key.group(1) in out)

code, out, calls, keys, envtxt = run("-NoVerify", env_text="A=1\nPROCUREIQ_API_KEY=\nB=2\n", tunnel_log=CF_LOG)
check("a blank PROCUREIQ_API_KEY= line is replaced, not duplicated", envtxt.count("PROCUREIQ_API_KEY=") == 1 and re.search(r"^PROCUREIQ_API_KEY=[0-9a-f]{48}$", envtxt, re.M) and "A=1\n" in envtxt and "B=2\n" in envtxt)
code, out, calls, keys, envtxt = run("-NoVerify", env_text="A=1", tunnel_log=CF_LOG)
check(".env without a trailing newline is handled", re.search(r"^A=1\r?\nPROCUREIQ_API_KEY=[0-9a-f]{48}", envtxt, re.M) is not None)
existing = "x" * 8 + "abcdef0123456789" * 2
code, out, calls, keys, envtxt = run("-NoVerify", env_text=f"PROCUREIQ_API_KEY={existing}\n", tunnel_log=CF_LOG)
check("an existing key is kept as it is", code == 0 and f"PROCUREIQ_API_KEY={existing}\n" in envtxt and existing not in out and "New API key" not in out)
code, out, calls, keys, envtxt = run("-NoVerify", "-RotateKey", env_text=f"PROCUREIQ_API_KEY={existing}\n", tunnel_log=CF_LOG)
check("-RotateKey replaces the key", code == 0 and existing not in envtxt and re.search(r"^PROCUREIQ_API_KEY=[0-9a-f]{48}$", envtxt, re.M) is not None)
code, out, calls, keys, envtxt = run("-NoVerify", env_text="PROCUREIQ_API_KEY=short\n", tunnel_log=CF_LOG)
check("a weak existing key is refused, not exposed", code != 0 and "too short" in out and not ups(calls))

# ---- what gets started
code, out, calls, _, _ = run("-NoVerify", tunnel_log=CF_LOG)
up = ups(calls)
check("cloudflare: starts with --profile app --profile public and the .env file", len(up) == 1 and "--profile app --profile public" in up[0] and "--profile public-ngrok" not in up[0] and "--env-file" in up[0])
check("the ngrok tunnel is stopped first (never two public paths)", any(" stop ngrok" in c for c in calls) and calls.index(next(c for c in calls if " stop ngrok" in c)) < calls.index(up[0]))
check("nothing is ever torn down with `down`/volumes", not any(" down" in c or " -v" in c for c in calls))
check("waits for the api container's health status", any("inspect -f {{.State.Health.Status}} procureiq_api" in c for c in calls))

code, out, calls, _, _ = run("-NoVerify", "-ReadyTimeoutS", "4", tunnel_log=CF_LOG, extra_env={"FAKE_HEALTH": "starting"})
check("API never healthy -> fails with a pointer to the logs, no URL announced", code != 0 and "did not become healthy" in out and "PUBLIC" not in out)

# ---- URL detection
code, out, calls, _, _ = run("-NoVerify", tunnel_log=CF_LOG)
check("cloudflare URL found in realistic logs (box, noise, Cloudflare's api. hostname ignored)", code == 0 and "https://rivers-catalog-pending-smile.trycloudflare.com" in out and "https://api.trycloudflare.com" not in out)
code, out, calls, _, _ = run("-NoVerify", tunnel_log=CF_LOG_TRAILING_ERROR)
check("an api.trycloudflare.com error AFTER the URL does not hijack the URL", "https://rivers-catalog-pending-smile.trycloudflare.com" in out and "https://api.trycloudflare.com" not in out)
code, out, calls, _, _ = run("-NoVerify", tunnel_log=CF_LOG_RESTARTED)
check("after a tunnel restart the NEWEST URL is used", "https://kingston-inside-best-graphic.trycloudflare.com" in out and "PUBLIC" in out)
code, out, calls, _, _ = run("-NoVerify", tunnel_log="INF Requesting new quick Tunnel on trycloudflare.com...\n", timeout=150)
check("no URL ever appears -> explains how to continue (-PublicUrl)", code != 0 and "-PublicUrl" in out and "docker logs procureiq_tunnel" in out)

# ---- outside check + smoke test through the (fake) public URL
GOOD_KEY = "ab" * 24
FakeApi.ready, FakeApi.key = True, GOOD_KEY
code, out, calls, _, _ = run("-PublicUrl", BASE, env_text=f"PROCUREIQ_API_KEY={GOOD_KEY}\n", tunnel_log=CF_LOG)
check("outside /ready check passes and the smoke test runs through the URL with the API key", code == 0 and "[PASS] public /ready is ready" in out and "SMOKE TEST PASSED" in out and "route=sql" in out and "route=rag" in out)
check("the summary tells you what to share and how to stop", "PUBLIC" in out and BASE in out and "-Stop" in out and "-RotateKey" in out and "X-API-Key" in out)

FakeApi.key = "not-the-key-in-env"
code, out, calls, _, _ = run("-PublicUrl", BASE, env_text=f"PROCUREIQ_API_KEY={GOOD_KEY}\n", tunnel_log=CF_LOG)
check("wrong API key through the tunnel -> smoke test fails, script fails", code != 0 and "SMOKE TEST FAILED" in out)

FakeApi.key = GOOD_KEY
FakeApi.ready = False
code, out, calls, _, _ = run("-PublicUrl", BASE, "-VerifyTimeoutS", "4", env_text=f"PROCUREIQ_API_KEY={GOOD_KEY}\n", tunnel_log=CF_LOG)
check("public URL not ready -> fails instead of announcing a dead link", code != 0 and "did not answer /ready" in out and "PUBLIC ====" not in out)
FakeApi.ready = True

# ---- a stale quick tunnel heals itself (the failure seen on a real machine) and failures explain themselves
PAT = ["-TunnelUrlPattern", r"http://(127)\.0\.0\.1:\d+"]
GOODENV = f"PROCUREIQ_API_KEY={GOOD_KEY}\n"
FakeApi.ready, FakeApi.key = True, GOOD_KEY
restarts = lambda calls: [c for c in calls if " rm -f -s tunnel" in c]  # noqa: E731

code, out, calls, _, _ = run(*PAT, "-VerifyTimeoutS", "4", env_text=GOODENV, tunnel_log=f"INF tunnel at {DEAD}\n", tunnel_log_after=f"INF tunnel at {BASE}\n")
check("stale tunnel address (HTTP 530): the script notices, says why, restarts the tunnel and succeeds", code == 0 and "HTTP 530" in out and "Restarting the tunnel for a fresh address" in out and "SMOKE TEST PASSED" in out)
check("...it uses the NEW address, not the stale one", f"URL:      {BASE}" in out and DEAD not in out.split("PUBLIC")[-1])
check("...with exactly one restart (rm -f -s tunnel, then up -d --no-deps tunnel: the API container is never touched)", len(restarts(calls)) == 1 and any(c.endswith("up -d --no-deps tunnel") for c in calls))

code, out, calls, _, _ = run(*PAT, "-VerifyTimeoutS", "4", env_text=GOODENV, tunnel_log=f"INF tunnel at {BASE}\n", tunnel_log_after=f"INF tunnel at {DEAD}\n")
check("a healthy tunnel is left alone (no restart, same address)", code == 0 and not restarts(calls) and f"URL:      {BASE}" in out)

code, out, calls, _, _ = run(*PAT, "-VerifyTimeoutS", "4", env_text=GOODENV, tunnel_log=f"INF tunnel at {DEAD}\n", tunnel_log_after=f"INF tunnel at {DEAD}\n")
check("dead before AND after the restart: fails after ONE restart, never loops", code != 0 and len(restarts(calls)) == 1 and "PUBLIC ====" not in out)
check("the failure shows the real reason (Last result: HTTP 530)", "Last result: HTTP 530" in out)
check("the failure shows the tunnel's own log lines and what to try", "Tunnel log (last lines)" in out and "VPN" in out and "7844" in out and "-Provider ngrok" in out)
check("the tail of the tunnel log was fetched from the tunnel container", any("logs --tail 8 procureiq_tunnel" in c for c in calls))

code, out, calls, _, _ = run(*PAT, "-VerifyTimeoutS", "4", env_text=GOODENV, tunnel_log="INF tunnel at http://127.0.0.1:1\n", tunnel_log_after="INF tunnel at http://127.0.0.1:1\n")
check("a connection error is reported with its own message, not hidden", code != 0 and re.search(r"Last result: \S", out) is not None and "HTTP 530" not in out)

code, out, calls, _, _ = run(*PAT, "-VerifyTimeoutS", "4", env_text=GOODENV, tunnel_log=f"INF tunnel at {BAD502}\n", tunnel_log_after=f"INF tunnel at {BASE}\n")
check("HTTP 502 is explained in plain words, then healed with a fresh tunnel", code == 0 and "HTTP 502 means the tunnel itself is up but cannot reach your API container" in out and "SMOKE TEST PASSED" in out and len(restarts(calls)) == 1)

code, out, calls, _, _ = run(*PAT, "-VerifyTimeoutS", "4", env_text=GOODENV, tunnel_log=f"INF tunnel at {BAD502}\n", tunnel_log_after=f"INF tunnel at {BAD502}\n")
check("a persistent 502 fails with the meaning of 502, and facts about BOTH containers (state, health, networks)", code != 0 and "HTTP 502 = Cloudflare reached your tunnel" in out and "API container:" in out and "Tunnel container:" in out)

code, out, calls, _, _ = run(*PAT, "-VerifyTimeoutS", "4", env_text=GOODENV, tunnel_log=f"INF tunnel at {DEAD}\n", tunnel_log_after=f"INF tunnel at {BASE}\n", extra_env={"FAKE_UNHEALTHY_AFTER": "1"})
check("API turned unhealthy: NO tunnel restart, a clear message with the commands to look at the API instead", code != 0 and not restarts(calls) and "API container is not healthy" in out and "logs api" in out)

code, out, calls, _, _ = run(*PAT, "-NoVerify", env_text=GOODENV, tunnel_log=CF_LOG, extra_env={"FAKE_UP_FAIL": "1"})
check("port already allocated on first start: nothing is announced, and the hint says how to find the culprit", code != 0 and "port is already allocated" in out and "netstat -ano | findstr :8000" in out and "PUBLIC ====" not in out)

code, out, calls, _, _ = run(*PAT, "-VerifyTimeoutS", "4", env_text=GOODENV, tunnel_log=f"INF tunnel at {DEAD}\n", tunnel_log_after=f"INF tunnel at {BASE}\n", extra_env={"FAKE_UP_FAIL_NODEPS": "1"})
check("the same hint appears if the tunnel restart itself fails", code != 0 and "Could not restart the tunnel" in out and "netstat -ano | findstr :8000" in out)

code, out, calls, _, _ = run("-PublicUrl", DEAD, "-VerifyTimeoutS", "4", env_text=GOODENV, tunnel_log=CF_LOG)
check("a URL you pass with -PublicUrl is never 'healed' by restarting anything", code != 0 and not restarts(calls) and "Last result: HTTP 530" in out)

code, out, calls, _, _ = run("-Provider", "ngrok", "-VerifyTimeoutS", "4", env_text=GOODENV + "NGROK_AUTHTOKEN=tok\nNGROK_DOMAIN=no-such-name.invalid\n")
check("ngrok never restarts the cloudflare tunnel, and reads ITS container's log", code != 0 and not restarts(calls) and any("logs --tail 8 procureiq_ngrok" in c for c in calls))

# ---- ngrok
KEYED = f"PROCUREIQ_API_KEY={'cd' * 24}\n"
code, out, calls, _, _ = run("-Provider", "ngrok", "-NoVerify", env_text=KEYED)
check("ngrok without token/domain -> says exactly what to add to .env, starts nothing", code != 0 and "NGROK_AUTHTOKEN" in out and "NGROK_DOMAIN" in out and not ups(calls))
code, out, calls, _, _ = run("-Provider", "ngrok", "-NoVerify", env_text=KEYED + "NGROK_AUTHTOKEN=tok\nNGROK_DOMAIN=https://my-name.ngrok-free.app\n")
check("ngrok domain with https:// is rejected", code != 0 and "bare host name" in out and not ups(calls))
code, out, calls, _, _ = run("-Provider", "ngrok", "-NoVerify", env_text=KEYED + "NGROK_AUTHTOKEN=tok\nNGROK_DOMAIN=my-name.ngrok-free.app\n")
up = ups(calls)
check("ngrok: starts --profile public-ngrok, stops the cloudflare tunnel, URL is the static domain", code == 0 and len(up) == 1 and "--profile public-ngrok" in up[0] and any(" stop tunnel" in c for c in calls) and "https://my-name.ngrok-free.app" in out)
check("ngrok summary does not claim the URL changes", "this URL changes" not in out)

# ---- stop
code, out, calls, _, envtxt = run("-Stop", env_text=KEYED)
check("-Stop stops and removes only the tunnel services", code == 0 and any(" stop tunnel ngrok" in c for c in calls) and any(" rm -f tunnel ngrok" in c for c in calls))
check("-Stop does not start, rebuild or take anything else down", not ups(calls) and not any(" down" in c for c in calls) and envtxt == KEYED)
check("-Stop works even if Docker is idle (no precondition prompts, no key churn)", "New API key" not in out)

shutil.rmtree(work, ignore_errors=True)
server.shutdown()
print(f"\nAll {N} checks passed.")
