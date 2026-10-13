"""
Phase 10 — load the OFAC SDN list (SDN.CSV + ALT.CSV) into Postgres `sanctions_entries`.

Get the files (either way works):
  A) automatic:  python risk/ofac_loader.py --download
  B) manual:     download SDN.CSV and ALT.CSV from https://sanctionslist.ofac.treas.gov/Home/static/sdn.html
                 and save them as  data/raw/ofac/SDN.CSV  and  data/raw/ofac/ALT.CSV

Then load (this also creates the Phase 10 tables for you, idempotently):
    python risk/ofac_loader.py
    python risk/ofac_loader.py --sdn some/SDN.CSV --alt some/ALT.CSV      # custom paths
    python risk/ofac_loader.py --schema-only                              # just (re)create tables/grants

Every load is a FULL REFRESH inside one transaction (OFAC's own guidance: the files are
comprehensive, so replace rather than merge). A failed load leaves the old list intact.

File formats (no header rows; "-0-" means empty):
  SDN.CSV: ent_num, name, type, program, title, call_sign, vess_type, tonnage, grt, vess_flag, vess_owner, remarks
  ALT.CSV: ent_num, alt_num, alt_type, alt_name, remarks
Weak aliases are not in ALT.CSV (OFAC puts them in the remarks field); we do not screen those.
"""
import argparse
import csv
import hashlib
import io
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
load_dotenv(REPO_ROOT / ".env")

DEFAULT_DIR = REPO_ROOT / "data" / "raw" / "ofac"
SCHEMA_SQL = HERE / "schema_risk.sql"

# Tried in order. (The current service first; the legacy treasury.gov path as a fallback.)
DOWNLOAD_URLS = {
    "SDN.CSV": [
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV",
        "https://www.treasury.gov/ofac/downloads/sdn.csv",
    ],
    "ALT.CSV": [
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ALT.CSV",
        "https://www.treasury.gov/ofac/downloads/alt.csv",
    ],
}


def connect(user_env="POSTGRES_USER", pw_env="POSTGRES_PASSWORD"):
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"), port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "procureiq"),
        user=os.getenv(user_env, "postgres"), password=os.getenv(pw_env, "postgres"),
    )


# ------------------------------------------------------------------ parsing (pure functions)
def _clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v == "-0-" else v


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _rows(text: str):
    for row in csv.reader(io.StringIO(text)):
        if row and row[0].strip().isdigit():   # skips blanks and the legacy trailing ^Z line
            yield row


def parse_sdn(text: str) -> list[dict]:
    out = []
    for r in _rows(text):
        if len(r) < 4 or not _clean(r[1]):
            continue
        etype = _clean(r[2]).lower() or "entity"       # "-0-" in the type column = an entity
        out.append({
            "ent_num": int(r[0]), "name": _clean(r[1]), "entry_type": etype,
            "program": _clean(r[3]).replace("] [", ", "), "source": "SDN", "alt_type": "",
        })
    return out


def parse_alt(text: str, primaries: list[dict]) -> list[dict]:
    """Aliases inherit entry_type + program from their primary record (joined on ent_num)."""
    by_ent = {p["ent_num"]: p for p in primaries}
    out = []
    for r in _rows(text):
        if len(r) < 4 or not _clean(r[3]):
            continue
        parent = by_ent.get(int(r[0]))
        if parent is None:
            continue
        out.append({
            "ent_num": int(r[0]), "name": _clean(r[3]), "entry_type": parent["entry_type"],
            "program": parent["program"], "source": "ALT", "alt_type": _clean(r[2]).lower(),
        })
    return out


# ------------------------------------------------------------------ download
def download(dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for fname, urls in DOWNLOAD_URLS.items():
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ProcureIQ-portfolio-project/0.10"})
                body = urllib.request.urlopen(req, timeout=60).read()
                head = _decode(body[:300]).lstrip().lower()
                if head.startswith("<") or b"," not in body[:300]:
                    raise ValueError("response is not CSV (got HTML?)")
                (dest_dir / fname).write_bytes(body)
                print(f"downloaded {fname} ({len(body):,} bytes) from {url}")
                break
            except Exception as e:  # noqa: BLE001
                print(f"  {fname}: {url} failed ({type(e).__name__}: {str(e)[:80]})")
        else:
            sys.exit(
                f"\nCould not download {fname}. Download it manually from\n"
                "  https://sanctionslist.ofac.treas.gov/Home/static/sdn.html\n"
                f"and save it as {dest_dir / fname}, then re-run without --download."
            )


# ------------------------------------------------------------------ database
def apply_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.commit()


def load(conn, sdn_path: Path, alt_path: Path) -> dict:
    sdn_raw, alt_raw = sdn_path.read_bytes(), alt_path.read_bytes()
    primaries = parse_sdn(_decode(sdn_raw))
    aliases = parse_alt(_decode(alt_raw), primaries)
    if len(primaries) < 3:
        raise SystemExit(f"{sdn_path} parsed to only {len(primaries)} records -- wrong file? Refusing to load.")

    apply_schema(conn)
    rows = [(e["ent_num"], e["name"], e["entry_type"], e["program"], e["source"], e["alt_type"]) for e in primaries + aliases]
    now = datetime.now()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE sanctions_entries RESTART IDENTITY")
            psycopg2.extras.execute_values(
                cur, "INSERT INTO sanctions_entries (ent_num, name, entry_type, program, source, alt_type) VALUES %s",
                rows, page_size=2000,
            )
            cur.execute(
                """INSERT INTO sanctions_list_info (id, loaded_at, sdn_sha256, alt_sha256, n_primary, n_alias)
                   VALUES (1, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET loaded_at = EXCLUDED.loaded_at, sdn_sha256 = EXCLUDED.sdn_sha256,
                   alt_sha256 = EXCLUDED.alt_sha256, n_primary = EXCLUDED.n_primary, n_alias = EXCLUDED.n_alias""",
                (now, hashlib.sha256(sdn_raw).hexdigest(), hashlib.sha256(alt_raw).hexdigest(), len(primaries), len(aliases)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    by_type: dict[str, int] = {}
    for e in primaries:
        by_type[e["entry_type"]] = by_type.get(e["entry_type"], 0) + 1
    return {"primary": len(primaries), "aliases": len(aliases), "by_type": by_type}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sdn", type=Path, default=DEFAULT_DIR / "SDN.CSV")
    ap.add_argument("--alt", type=Path, default=DEFAULT_DIR / "ALT.CSV")
    ap.add_argument("--download", action="store_true", help=f"fetch the files into {DEFAULT_DIR} first")
    ap.add_argument("--schema-only", action="store_true", help="only create tables/grants, load nothing")
    args = ap.parse_args()

    conn = connect()
    try:
        if args.schema_only:
            apply_schema(conn)
            print("Phase 10 tables/grants applied.")
            sys.exit(0)
        if args.download:
            download(DEFAULT_DIR)
        for p in (args.sdn, args.alt):
            if not p.exists():
                sys.exit(f"Missing {p}. Use --download, or save the OFAC file there (see --help).")
        stats = load(conn, args.sdn, args.alt)
        print(f"Loaded {stats['primary']:,} primary records + {stats['aliases']:,} aliases.  By type: {stats['by_type']}")
    finally:
        conn.close()
