"""
Fail if anything that git tracks (or has staged) looks like a real secret.

    git add .
    python scripts/check_no_secrets.py          # exit 0 = clean, 1 = found something

Run it BEFORE your first push and whenever you add files; CI runs it on every push as a second line
of defence (the first is .gitignore). It checks:
  1. a tracked file named .env / .env.* (only .env.example is allowed)
  2. well-known API-key shapes inside any tracked text file (OpenRouter, Langfuse, Google, Hugging
     Face, PEM private keys). Matches are printed MASKED -- the scanner never echoes a full key.
It is a safety net, not a guarantee: a leaked key must still be revoked, not just deleted from git.
"""
import re
import subprocess
import sys
from pathlib import Path

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PATTERNS = {
    "OpenRouter API key": re.compile(r"sk-or-v1-[0-9a-f]{32,}"),
    "Langfuse secret key": re.compile(r"sk-lf-" + _UUID),
    "Langfuse public key": re.compile(r"pk-lf-" + _UUID),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "Hugging Face token": re.compile(r"hf_[A-Za-z0-9]{34,}"),
    "PEM private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}
MAX_BYTES = 2_000_000


def mask(s: str) -> str:
    return s[:7] + "..." + f"({len(s)} chars)"


def scan_text(name: str, text: str) -> list[tuple[str, int, str]]:
    """-> [(file, line_number, kind)] -- never the secret itself."""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for kind, rx in PATTERNS.items():
            if rx.search(line):
                hits.append((name, i, kind))
    return hits


def is_env_file(path: str) -> bool:
    base = Path(path).name
    return (base == ".env" or base.startswith(".env.")) and base != ".env.example"


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def scan_repo(root: Path) -> list[str]:
    problems = []
    for rel in tracked_files(root):
        if is_env_file(rel):
            problems.append(f"{rel}: an env file is tracked by git (only .env.example may be)")
            continue
        p = root / rel
        if not p.is_file() or p.stat().st_size > MAX_BYTES:
            continue
        try:
            text = p.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary
        for f, line, kind in scan_text(rel, text):
            problems.append(f"{f}:{line}: looks like a {kind}")
    return problems


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent
    try:
        problems = scan_repo(root)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"could not list git files ({type(e).__name__}); run `git init` / `git add .` first")
        return 2
    if problems:
        print("POSSIBLE SECRETS IN GIT:")
        for p in problems:
            print("  " + p)
        print("\nRemove them, add the file to .gitignore, and REVOKE any key that was exposed.")
        return 1
    print(f"ok: no secrets found in {len(tracked_files(root))} tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
