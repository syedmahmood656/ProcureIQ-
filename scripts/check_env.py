"""
Check a .env file for mistakes that silently break Python tools and Docker Compose, and repair them.

    python scripts/check_env.py              # checks .env (and .env.example if present); changes nothing
    python scripts/check_env.py --fix        # repairs .env, after saving a backup next to it
    python scripts/check_env.py some\\other.env

What it finds
  1. A comment right after an EMPTY value:      PGSSLMODE=        # "require" for Neon
     python-dotenv AND Docker Compose both read that as the value  # "require" for Neon  (a real incident:
     "invalid sslmode value" on every database connection). Fix: the line becomes  PGSSLMODE=
     (comments after a REAL value, KEY=value  # note, are read correctly and are left alone)
  2. A UTF-8 byte-order mark at the start of the file (Windows PowerShell 5.1 adds one with
     `Set-Content -Encoding utf8`): the first variable name is then corrupted. Fix: the mark is removed.

It NEVER prints values (a .env holds secrets): only variable names and line numbers.
Exit code: 0 clean (or repaired), 1 problems found (and not repaired), 2 file not found.
"""
import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOM = b"\xef\xbb\xbf"
# after '=', optional spaces, then '#': the comment is parsed as the value. Quoted values start with a quote,
# so KEY="a # b" never matches; whole-line comments are skipped before this pattern is applied.
BAD_LINE = re.compile(r"^(?P<head>\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)\s*=)\s*#.*?(?P<eol>\r?\n)?$", re.S)


def analyse(raw: bytes):
    """-> (has_bom, [(line_number, key)])  Works on bytes so nothing about the file is altered."""
    has_bom = raw.startswith(BOM)
    text = raw[len(BOM):].decode("utf-8", "surrogateescape") if has_bom else raw.decode("utf-8", "surrogateescape")
    bad = []
    for n, line in enumerate(text.splitlines(keepends=True), 1):
        if line.lstrip().startswith("#"):
            continue
        m = BAD_LINE.match(line)
        if m:
            bad.append((n, m.group("key")))
    return has_bom, bad


def repair(raw: bytes) -> bytes:
    """Same bytes, except: BOM removed, and `KEY=   # comment` becomes `KEY=` (line ending kept)."""
    has_bom = raw.startswith(BOM)
    text = raw[len(BOM):].decode("utf-8", "surrogateescape") if has_bom else raw.decode("utf-8", "surrogateescape")
    out = []
    for line in text.splitlines(keepends=True):
        m = None if line.lstrip().startswith("#") else BAD_LINE.match(line)
        out.append(m.group("head") + (m.group("eol") or "") if m else line)
    return "".join(out).encode("utf-8", "surrogateescape")


def check_file(path: Path, fix: bool) -> int:
    """Returns 0 (clean or repaired) or 1 (problems left)."""
    raw = path.read_bytes()
    has_bom, bad = analyse(raw)
    if not has_bom and not bad:
        print(f"ok: {path.name} has no problems")
        return 0
    print(f"{path.name}: problems found")
    if has_bom:
        print("  - starts with a byte-order mark (BOM): the first variable name is corrupted")
    for n, key in bad:
        print(f"  - line {n}: {key}  has a comment right after an empty value (it would be read as the value)")
    if not fix:
        print(f"  fix it with:  python scripts/check_env.py --fix        (a backup is saved first)")
        return 1
    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    path.write_bytes(repair(raw))
    print(f"  repaired. Backup of the original: {backup.name}  (it may contain secrets: delete it when you no longer need it)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path, help="files to check (default: .env and .env.example in the repo root)")
    ap.add_argument("--fix", action="store_true", help="repair problems (saves a backup first)")
    args = ap.parse_args(argv)

    explicit = bool(args.files)
    targets = args.files or [REPO_ROOT / ".env", REPO_ROOT / ".env.example"]
    status = 0
    checked = 0
    for p in targets:
        if not p.exists():
            if explicit:
                print(f"not found: {p}")
                return 2
            print(f"(no {p.name} here, skipped)")
            continue
        checked += 1
        # by default only .env is repaired; the committed example is checked, never rewritten
        status = max(status, check_file(p, fix=args.fix and (explicit or p.name != ".env.example")))
    if checked == 0:
        print("nothing to check")
        return 2
    return status


if __name__ == "__main__":
    sys.exit(main())
