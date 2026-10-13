"""Offline tests for scripts/check_no_secrets.py. Fake keys are assembled at runtime so this file
itself contains nothing the scanner (or GitHub's push protection) would flag.
Run: python scripts/test_check_no_secrets_offline.py"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_no_secrets as cns  # noqa: E402

N = 0


def check(label, cond):
    global N
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label
    N += 1


UUID = "12345678-1234-1234-1234-123456789abc"
FAKES = {
    "OpenRouter API key": "sk-or-" + "v1-" + "a1" * 20,
    "Langfuse secret key": "sk-lf-" + UUID,
    "Langfuse public key": "pk-lf-" + UUID,
    "Google API key": "AI" + "za" + "A" * 35,
    "Hugging Face token": "hf" + "_" + "B" * 36,
    "PEM private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
}
for kind, fake in FAKES.items():
    hits = cns.scan_text("x.py", f'KEY = "{fake}"\n')
    check(f"detects {kind}", [h[2] for h in hits] == [kind])
check("reports the line number", cns.scan_text("x", "a\nb\n" + FAKES["Google API key"])[0][1] == 3)
check("the finding never contains the secret", all(FAKES["OpenRouter API key"] not in str(h) for h in cns.scan_text("x", FAKES["OpenRouter API key"])))
check("mask() hides the body", FAKES["Google API key"][12:] not in cns.mask(FAKES["Google API key"]))
for ok in ("OPENROUTER_API_KEY=", "pk-test-1", "sk-test", "LANGFUSE_PUBLIC_KEY=pk-lf-...", "sk-or-v1-short", "hf_token = None"):
    check(f"no false positive on {ok!r}", cns.scan_text("x", ok) == [])
check(".env is an env file", cns.is_env_file(".env") and cns.is_env_file("sub/.env.local") and cns.is_env_file(".env.ci"))
check(".env.example is allowed", not cns.is_env_file(".env.example") and not cns.is_env_file("README.md"))

if shutil.which("git"):
    tmp = Path(tempfile.mkdtemp())
    try:
        def git(*a):
            subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True)

        git("init", "-q")
        (tmp / "ok.py").write_text("print('hi')\n")
        (tmp / ".env.example").write_text("OPENROUTER_API_KEY=\n")
        git("add", ".")
        check("clean repo -> no problems", cns.scan_repo(tmp) == [])
        (tmp / "leak.py").write_text(f'K = "{FAKES["Langfuse secret key"]}"\n')
        (tmp / ".env").write_text("X=1\n")
        (tmp / "blob.bin").write_bytes(b"\xff\xfe\x00" + FAKES["Google API key"].encode())
        git("add", "-f", ".")
        problems = cns.scan_repo(tmp)
        check("tracked secret in code is found with file:line", any(p.startswith("leak.py:1:") for p in problems))
        check("tracked .env is found", any(p.startswith(".env:") for p in problems))
        check("binary files are skipped, not crashed on", not any("blob.bin" in p for p in problems))
        (tmp / "untracked.py").write_text(f'K = "{FAKES["Google API key"]}"\n')  # never `git add`ed
        check("untracked files are not scanned (only what git would commit)", not any("untracked.py" in p for p in cns.scan_repo(tmp)))
        (tmp / ".gitignore").write_text(".env\n")
        git("rm", "-q", "--cached", ".env")
        git("rm", "-q", "--cached", "leak.py")
        check("after untracking both, repo is clean again", cns.scan_repo(tmp) == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
else:
    print("(git not installed: repo-level checks skipped)")

print(f"\nAll {N} checks passed.")
