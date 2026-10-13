"""
Phase 10 offline tests — no database, no network, no models. Uses the fixtures in risk/fixtures/
(fictional entries, every remark says TEST FIXTURE).

Run:   python risk/test_risk_offline.py
"""
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from matcher import SanctionsIndex, core_tokens, score, status_for, tokens  # noqa: E402
from ofac_loader import _decode, parse_alt, parse_sdn  # noqa: E402

N = 0


def check(label, cond):
    global N
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    assert cond, label
    N += 1


# ---- 1. normalization
check("legal suffix + punctuation: 'Kestrel River Trading L.L.C.'", tokens("Kestrel River Trading L.L.C.") == ["kestrel", "river", "trading"])
check("generic word dropped for matching", core_tokens("Kestrel River Trading L.L.C.") == ["kestrel", "river"])
check("accents and ampersand: 'Müller & Söhne GmbH'", tokens("Müller & Söhne GmbH") == ["muller", "sohne"])
check("name made only of legal words keeps something", tokens("Company LLC") == ["company"] or tokens("Company LLC") != [])
check("name made only of generic words keeps something", len(core_tokens("Global Holdings Group")) > 0)
check("word order is irrelevant", core_tokens("River Kestrel") == core_tokens("Kestrel River"))
check("score: identical core = 1.0", score(core_tokens("Acme Inc"), core_tokens("ACME Incorporated")) == 1.0)
check("status thresholds", status_for(0.95) == "REVIEW" and status_for(0.90) == "REVIEW" and status_for(0.85) == "WEAK_MATCH" and status_for(0.5) == "CLEAR")

# ---- 2. parsing real-format files
sdn_text = _decode((HERE / "fixtures" / "SDN_sample.csv").read_bytes())
alt_text = _decode((HERE / "fixtures" / "ALT_sample.csv").read_bytes())
primaries = parse_sdn(sdn_text)
aliases = parse_alt(alt_text, primaries)
check("5 primary records parsed; trailing ^Z line skipped", len(primaries) == 5)
check("'-0- ' type column -> entity", primaries[0]["entry_type"] == "entity")
check("individual / vessel types preserved", {p["entry_type"] for p in primaries} == {"entity", "individual", "vessel"})
check("program '] [' joined to a readable list", primaries[1]["program"] == "TESTPROG, TESTPROG2")
check("quoted comma inside remarks does not shift columns", primaries[1]["name"] == "NORTHWIND ZED EXPORT LTD")
check("2 aliases parsed, inheriting type and program", len(aliases) == 2 and aliases[0]["entry_type"] == "entity" and aliases[0]["program"] == "TESTPROG")
check("alias keeps its own name and alt_type", aliases[1]["name"] == "NORTHWIND ZED IMPORT EXPORT" and aliases[1]["alt_type"] == "fka")
check("alias for unknown ent_num is dropped", parse_alt('77777,1,"aka","GHOST NAME","-0-"', primaries) == [])
check("latin-1 bytes decode without crashing", "caf" in _decode("café".encode("latin-1")))

# ---- 3. matching
idx = SanctionsIndex(primaries + aliases)
check("individuals/vessels excluded by default (entities only)", len(idx) == 5 - 2 + 2)

def st(name, index=idx):
    return index.screen(name)

r = st("Kestrel River Trading L.L.C.")
check("same company, different formatting -> REVIEW 1.0", r["status"] == "REVIEW" and r["match"]["score"] == 1.0 and r["match"]["ent_num"] == 9001)
r = st("Kestrel River Commodities Inc")
check("alias match is found and labelled 'alias'", r["status"] == "REVIEW" and r["match"]["matched_on"] == "alias")
r = st("Kestrel Rivet Trading LLC")
check("one-letter typo in a distinctive word still flagged for review", r["status"] == "REVIEW" and 0.9 <= r["match"]["score"] < 1.0)
check("KNOWN GAP (pinned): a typo inside a generic word ('Tradng') defeats generic-word removal -> not REVIEW",
      st("Kestrel Rivet Tradng LLC")["status"] != "REVIEW")
r = st("Northwind Zed Import & Export Limited")
check("alias with '&' and legal suffix matches", r["status"] == "REVIEW" and r["match"]["ent_num"] == 9002)
check("unrelated company -> CLEAR with no match", st("Buchanan LLC") == {"status": "CLEAR", "match": None})
check("only-generic-words name does not match a real entry", st("Holdings Group Inc")["status"] == "CLEAR")
check("too-short name is not screened", idx.search("AB") == [] and idx.search("") == [])
check("person-name vendor is CLEAR by default", st("Garcia-Smith")["status"] == "CLEAR")
idx_p = SanctionsIndex(primaries + aliases, include_individuals=True)
check("...but matches the individual with --include-individuals", st("Maria Garcia Smith", idx_p)["status"] == "REVIEW")
check("vessels are never searched", all(e["entry_type"] != "vessel" for e in idx_p.entries))
check("results deduplicated per OFAC entity (primary + alias = one hit)", len(idx.search("Kestrel River Trading")) == 1)
weak = idx.search("Kestrel Rover Trading Company Ltd", min_score=0.5)
check("min_score controls what is returned", len(weak) >= 1)

# ---- 4. scale: 40k synthetic list entries, 150 vendors
rnd = random.Random(7)
syll = ["ka", "to", "mi", "ra", "ne", "lo", "su", "vi", "da", "pe", "zo", "an", "el", "or", "ix"]
word = lambda: "".join(rnd.choice(syll) for _ in range(rnd.randint(2, 4)))
big = [{"ent_num": i, "name": f"{word()} {word()} {rnd.choice(['LLC', 'Ltd', 'Trading Co'])}", "entry_type": "entity", "program": "X", "source": "SDN"} for i in range(40000)]
t0 = time.time()
big_idx = SanctionsIndex(big)
build_s = time.time() - t0
t0 = time.time()
for _ in range(150):
    big_idx.screen(f"{word()} {word()} Inc")
screen_s = time.time() - t0
check(f"index of 40k names builds quickly ({build_s:.1f}s)", build_s < 10)
check(f"150 vendor screens against 40k names are fast ({screen_s:.1f}s)", screen_s < 15)
known = big[1234]["name"]
check("a known synthetic entry is found among 40k", big_idx.screen(known)["status"] == "REVIEW")

print(f"\nAll {N} checks passed.")
