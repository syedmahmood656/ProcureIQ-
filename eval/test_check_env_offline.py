"""Offline tests for scripts/check_env.py (uses the real python-dotenv parser, the thing the bug was about).
Run: python scripts/test_check_env_offline.py"""
import io
import re
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_env as ce  # noqa: E402

N = 0


def check(label, cond):
    global N
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label
    N += 1


SECRET = "sk-or-" + "v1-" + "ab" * 20      # assembled at runtime so no key-shaped literal sits in this file
SAMPLE = (
    "# comment line\n"
    "PGSSLMODE=                           # \"require\" for Neon\n"
    "QDRANT_API_KEY=# no space before the comment\n"
    "export PROCUREIQ_FAKE_MODELS=  # exported, empty\n"
    f"OPENROUTER_API_KEY={SECRET}\n"
    "WITHVALUE=local          # a real value, then a comment: read correctly\n"
    "QUOTED=\"a # b\"\n"
    "SINGLE='#x'\n"
    "# COMMENTED=   # a commented-out assignment\n"
    "EMPTY=\n"
    "\n"
    "LAST=   # final line without newline"
)
BAD_KEYS = ["PGSSLMODE", "QDRANT_API_KEY", "PROCUREIQ_FAKE_MODELS", "LAST"]

# ---- 1. what the real parser does with the pattern (the bug) and what is NOT the bug
parsed = dotenv_values(stream=io.StringIO(SAMPLE))
check("the real parser reads a comment after an empty value AS THE VALUE (the incident)", parsed["PGSSLMODE"].startswith("#") and parsed["QDRANT_API_KEY"].startswith("#"))
check("...but reads a comment after a real value correctly", parsed["WITHVALUE"] == "local")

# ---- 2. detection
has_bom, bad = ce.analyse(SAMPLE.encode())
check("finds exactly the four bad lines, by variable name and line number", [k for _, k in bad] == BAD_KEYS and [n for n, _ in bad] == [2, 3, 4, 12])
check("every flagged line really parses to a value starting with '#' (no false positives)", all(parsed[k].startswith("#") for k in BAD_KEYS))
check("no unflagged variable has a comment as its value (no false negatives); a QUOTED '#x' is a real value", all(not (v or "").startswith("#") for k, v in parsed.items() if k not in BAD_KEYS and k != "SINGLE") and parsed["SINGLE"] == "#x")
check("not flagged: comment after a value, quoted '#', whole-line and commented-out assignments, empty values", {"WITHVALUE", "QUOTED", "SINGLE", "EMPTY", "COMMENTED"}.isdisjoint({k for _, k in bad}))
check("CRLF files are detected too", [k for _, k in ce.analyse(b"A=   # c\r\nB=1\r\n")[1]] == ["A"])
check("no BOM in the sample", has_bom is False)

# ---- 3. repair
fixed = ce.repair(SAMPLE.encode())
check("repair changes ONLY the bad lines (everything else byte-identical)", [l for l in SAMPLE.split("\n") if l not in fixed.decode().split("\n")] == [
    "PGSSLMODE=                           # \"require\" for Neon", "QDRANT_API_KEY=# no space before the comment",
    "export PROCUREIQ_FAKE_MODELS=  # exported, empty", "LAST=   # final line without newline"])
after = dotenv_values(stream=io.StringIO(fixed.decode()))
check("after the repair the real parser gives EMPTY values for those keys", all(after[k] == "" for k in BAD_KEYS))
check("after the repair all other values are unchanged (secret included)", after["OPENROUTER_API_KEY"] == SECRET and after["WITHVALUE"] == "local" and after["QUOTED"] == "a # b" and after["SINGLE"] == "#x")
check("the 'export' prefix and a missing final newline are preserved", b"export PROCUREIQ_FAKE_MODELS=\n" in fixed and fixed.endswith(b"LAST="))
check("repair is idempotent and leaves a clean result", ce.repair(fixed) == fixed and ce.analyse(fixed) == (False, []))
crlf = ce.repair(b"A=   # c\r\nB=1\r\n")
check("CRLF line endings survive the repair", crlf == b"A=\r\nB=1\r\n")
check("leading indentation is preserved", ce.repair(b"  A=  # c\n") == b"  A=\n")

# ---- 4. BOM
bom_raw = ce.BOM + b"FIRST=1\nSECOND=2\n"
check("a UTF-8 BOM is detected", ce.analyse(bom_raw)[0] is True)
check("with the BOM the real parser corrupts the first variable NAME (why it matters)", "FIRST" not in dotenv_values(stream=io.StringIO(bom_raw.decode("utf-8"))))
check("repair removes the BOM and the first variable works again", ce.repair(bom_raw) == b"FIRST=1\nSECOND=2\n" and "FIRST" in dotenv_values(stream=io.StringIO(ce.repair(bom_raw).decode())))

# ---- 5. the command line: output never contains values, backups, exit codes
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    env = d / ".env"
    env.write_bytes(SAMPLE.encode())

    def run(*a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ce.main([str(x) for x in a])
        return code, buf.getvalue()

    code, out = run(env)
    check("problems found -> exit 1, names the variables and line numbers", code == 1 and "PGSSLMODE" in out and "line 2" in out and "--fix" in out)
    check("the output NEVER contains a value or a comment's text (secrets stay private)", SECRET not in out and "require" not in out and "Neon" not in out and "local" not in out)
    check("without --fix the file is untouched and no backup exists", env.read_bytes() == SAMPLE.encode() and not list(d.glob(".env.bak-*")))
    code, out = run(env, "--fix")
    backups = list(d.glob(".env.bak-*"))
    check("--fix repairs the file and exits 0", code == 0 and env.read_bytes() == fixed)
    check("--fix saved a backup with the ORIGINAL bytes first", len(backups) == 1 and backups[0].read_bytes() == SAMPLE.encode())
    check("the repair report also never prints values", SECRET not in out and "require" not in out)
    code, out = run(env)
    check("re-checking the repaired file is clean (exit 0)", code == 0 and "no problems" in out)
    before = env.read_bytes()
    code, out = run(env, "--fix")
    check("--fix on a clean file changes nothing and makes no new backup", code == 0 and env.read_bytes() == before and len(list(d.glob(".env.bak-*"))) == 1)
    code, out = run(d / "missing.env")
    check("a missing file given explicitly -> exit 2", code == 2 and "not found" in out)

# ---- 6. the committed example must itself be clean
code, out = (lambda b: (ce.main([str(HERE.parent / ".env.example")]), None))(None)
check(".env.example in this repo has none of these problems", code == 0)
ex = dotenv_values(HERE.parent / ".env.example")
check(".env.example: no variable's value starts with '#' under the real parser", not [k for k, v in ex.items() if (v or "").startswith("#")])
check(".env.example still defines the expected variables", {"PGSSLMODE", "QDRANT_API_KEY", "PROCUREIQ_API_KEY", "OPENROUTER_API_KEY", "POSTGRES_HOST"} <= set(ex))

print(f"\nAll {N} checks passed.")
