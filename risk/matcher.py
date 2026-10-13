"""
Phase 10 — company-name matching against the OFAC SDN list (pure Python, no DB, no network).

THE PROBLEM: "Kestrel River Trading L.L.C." and "KESTREL RIVER TRADING LLC" are the same
company; "Kestrel Rivet Tradng LLC" is probably a typo of it. Exact string comparison misses both.
Comparing every vendor to every list entry with a fuzzy ratio is slow (120 x ~40k).

THE APPROACH (the standard "block, then score" pattern):
  1. normalize   - lowercase, strip accents/punctuation, drop legal suffixes (llc, inc, gmbh ...)
                   and filler words, so formatting differences vanish.
  2. block       - only compare names that share a 4-letter token prefix (tolerates typos in the
                   tail of a word). Cuts ~40k comparisons per vendor to a handful.
  3. score       - difflib similarity of the sorted core tokens (word order doesn't matter).
  4. status      - >= REVIEW_SCORE: REVIEW | >= WEAK_SCORE: WEAK_MATCH | else CLEAR

IMPORTANT HONESTY RULE: this is NAME similarity only. A REVIEW result means "a human should
look", never "this vendor is sanctioned". Real screening also checks addresses, IDs and countries.
"""
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher

REVIEW_SCORE = 0.90
WEAK_SCORE = 0.80
MIN_NAME_CHARS = 4  # shorter normalized names are too ambiguous to screen

# Legal-form words: "Acme LLC" and "Acme Incorporated" must compare equal.
LEGAL_SUFFIXES = {
    "llc", "inc", "incorporated", "ltd", "limited", "corp", "corporation", "co", "company",
    "gmbh", "sa", "plc", "llp", "lp", "pte", "pvt", "bv", "ag", "nv", "sarl", "srl", "ooo",
    "jsc", "ojsc", "pjsc", "fze", "fzco", "fzc", "sas", "spa", "kg", "oy", "ab", "as", "sdn", "bhd",
}
FILLER = {"and", "the", "of", "for"}
# Words so common in company names that sharing one proves nothing; ignored when scoring/blocking
# (unless that would leave nothing).
GENERIC = {
    "group", "holding", "holdings", "international", "intl", "trading", "services", "service",
    "industries", "industrial", "enterprises", "enterprise", "global", "solutions", "systems",
    "technologies", "technology", "partners", "associates", "resources", "logistics",
}


def tokens(name: str) -> list[str]:
    """Normalized word list with legal suffixes and filler removed."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[.'’`]", "", s)          # L.L.C. -> llc, O'Brien -> obrien
    s = re.sub(r"[^a-z0-9]+", " ", s)      # everything else separates words
    toks = [t for t in s.split() if t not in FILLER]
    stripped = [t for t in toks if t not in LEGAL_SUFFIXES]
    return stripped or toks


def core_tokens(name: str) -> list[str]:
    toks = tokens(name)
    core = [t for t in toks if t not in GENERIC]
    return sorted(core or toks)


def score(a_core: list[str], b_core: list[str]) -> float:
    a, b = " ".join(a_core), " ".join(b_core)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


def status_for(best: float) -> str:
    if best >= REVIEW_SCORE:
        return "REVIEW"
    if best >= WEAK_SCORE:
        return "WEAK_MATCH"
    return "CLEAR"


def _block_keys(core: list[str]) -> set[str]:
    return {t[:4] for t in core if len(t) >= 3}


class SanctionsIndex:
    """
    entries: dicts with name, ent_num, entry_type ('entity'|'individual'|'vessel'|'aircraft'),
    program, source ('SDN' primary name | 'ALT' alias). By default only ENTITY records are
    searched: vendors are companies, and matching them against people's names is mostly noise.
    """

    def __init__(self, entries, include_individuals: bool = False):
        allowed = {"entity", "individual"} if include_individuals else {"entity"}
        self.entries = [e for e in entries if e.get("entry_type") in allowed]
        self._core = [core_tokens(e["name"]) for e in self.entries]
        self._blocks: dict[str, list[int]] = defaultdict(list)
        for i, core in enumerate(self._core):
            for k in _block_keys(core):
                self._blocks[k].append(i)

    def __len__(self) -> int:
        return len(self.entries)

    def search(self, name: str, min_score: float = WEAK_SCORE, limit: int = 5) -> list[dict]:
        core = core_tokens(name)
        if len(" ".join(core)) < MIN_NAME_CHARS:
            return []
        candidates: set[int] = set()
        for k in _block_keys(core):
            candidates.update(self._blocks.get(k, ()))
        best_per_ent: dict = {}
        for i in candidates:
            s = score(core, self._core[i])
            if s < min_score:
                continue
            e = self.entries[i]
            key = e["ent_num"]
            if key not in best_per_ent or s > best_per_ent[key]["score"]:
                best_per_ent[key] = {
                    "score": round(s, 3), "matched_name": e["name"], "matched_on": "alias" if e["source"] == "ALT" else "primary",
                    "ent_num": e["ent_num"], "entry_type": e["entry_type"], "program": e.get("program", ""),
                }
        return sorted(best_per_ent.values(), key=lambda m: -m["score"])[:limit]

    def screen(self, name: str) -> dict:
        """One vendor -> {status, best match fields or None}."""
        matches = self.search(name, limit=1)
        if not matches:
            return {"status": "CLEAR", "match": None}
        return {"status": status_for(matches[0]["score"]), "match": matches[0]}
