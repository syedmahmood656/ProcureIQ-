"""
Phase 6 — real routing logic: which tool(s) does this question need?

    "rag"  - the answer lives in contract TEXT        ("what is the liability cap in X's contract?")
    "sql"  - the answer lives in the ERP TABLES       ("top 5 vendors by total spend")
    "both" - needs both, SQL first, then RAG narrowed to the contracts SQL found
             ("vendors with spend above $1M that have an uncapped liability clause")

Design: signals first, LLM only as a tiebreaker.
  * Cheap, deterministic keyword/regex signals decide almost every question. That means zero
    extra LLM calls (and zero extra free-tier rate-limit pressure) on the common path, the same
    input always routes the same way, and the behaviour is unit-testable offline.
  * Only when NO signal fires at all (a vague question) do we spend one LLM call on a
    classification prompt; if that reply is unparseable the default is "rag" -- the Phase 5
    behaviour -- so a flaky model can never make routing worse than before.

This module is pure Python with no heavy imports, so it can be tested without any services.
"""
import re

# --- Structured-data signals (ERP tables: vendors / contracts / purchase_orders / invoices) ----
_SQL_PATTERNS = [
    r"\bspend\b", r"\bspent\b", r"\bspending\b",
    r"\bpurchase[- ]orders?\b", r"\bPOs?\b", r"\binvoices?\b", r"\boverdue\b", r"\bunpaid\b",
    r"\bpayments?\b", r"\bpaid\b", r"\bbilled\b",
    r"\$\s?\d", r"\bUSD\b",
    r"\bhow many vendors\b", r"\bnumber of vendors\b", r"\btop \d+\b", r"\bhighest[- ]spend",
    r"\bauto[- ]?renew\w*", r"\bexpir(?:e|es|ed|ing)\b", r"\bexpiration date\b",
    r"\bcountr(?:y|ies)\b", r"\bvendor categor(?:y|ies)\b", r"\bcategory of vendor",
    r"\brisk tier\b", r"\beffective date\b",
        r"\bsanction\w*", r"\bOFAC\b", r"\bSDN\b", r"\bwatch ?lists?\b", r"\bscreen(?:ed|ing)\b",
]
_SQL_RE = [re.compile(p, re.IGNORECASE) for p in _SQL_PATTERNS]

# --- Contract-text signals. Each topic carries the short search phrase used to query the contract
# index (see clause_query_for). Order matters: more specific topics come first. ----------------
CLAUSE_TOPICS = [
    ("uncapped liability", r"\buncapped\b|\bunlimited liability\b", "uncapped liability"),
    ("liability cap", r"\bliabilit\w*|\bindemnif\w*|\bcap on\b|\bliability cap\b", "liability cap"),
    ("termination for convenience", r"\bterminat\w*", "termination for convenience"),
    ("exclusivity", r"\bexclusiv\w*", "exclusivity"),
    ("non-compete", r"\bnon-?compet\w*", "non-compete"),
    ("governing law", r"\bgoverning law\b|\bjurisdiction\b", "governing law"),
    ("anti-assignment", r"\bassign(?:ment|able|s)?\b", "anti-assignment"),
    ("change of control", r"\bchange of control\b", "change of control"),
    ("warranty", r"\bwarrant\w*", "warranty duration"),
    ("audit rights", r"\baudit\w*", "audit rights"),
    ("insurance", r"\binsurance\b", "insurance requirement"),
    ("ip / license", r"\blicens\w*|\bintellectual property\b", "license grant intellectual property"),
    ("most favored nation", r"\bmost[- ]favou?red\b|\bMFN\b", "most favored nation"),
    ("notice period", r"\bnotice period\b", "notice period to terminate"),
    ("renewal term", r"\brenewal term\b|\brenewal period\b", "renewal term"),
    ("generic clause", r"\bclauses?\b|\bprovisions?\b|\bcontract (?:language|terms?|text)\b", ""),
]
_CLAUSE_RE = [(name, re.compile(p, re.IGNORECASE), phrase) for name, p, phrase in CLAUSE_TOPICS]

# Topic -> the exact CUAD category label stored on chunks (contract_chunks.categories).
# Used by the "both" route for an exact "which candidate contracts have this clause" lookup.
TOPIC_TO_CUAD_CATEGORY = {
    "uncapped liability": "Uncapped Liability",
    "liability cap": "Cap On Liability",
    "termination for convenience": "Termination For Convenience",
    "exclusivity": "Exclusivity",
    "non-compete": "Non-Compete",
    "governing law": "Governing Law",
    "anti-assignment": "Anti-Assignment",
    "change of control": "Change Of Control",
    "warranty": "Warranty Duration",
    "audit rights": "Audit Rights",
    "insurance": "Insurance",
    "ip / license": "License Grant",
    "most favored nation": "Most Favored Nation",
    "notice period": "Notice Period To Terminate Renewal",
    "renewal term": "Renewal Term",
}

_VALID_ROUTES = ("rag", "sql", "both")


def signal_counts(question: str) -> tuple[int, int, list[str]]:
    """Return (n_sql_signals, n_rag_signals, matched_clause_topic_names)."""
    sql_hits = sum(1 for rx in _SQL_RE if rx.search(question))
    topics = [name for name, rx, _ in _CLAUSE_RE if rx.search(question)]
    return sql_hits, len(topics), topics


def clause_query_for(question: str) -> str | None:
    """
    Short, clause-focused search phrase for the contract index, or None if no known clause topic
    matched (caller should then just use the full question).

    Why this exists: in a "both" question like "vendors with spend above $1.5M that have an
    uncapped liability clause", the dollar/vendor words are irrelevant to the contract index and
    actively hurt it -- keyword search ANDs every word, so the spend half makes keyword search
    return nothing, and it dilutes the dense embedding. Searching just "uncapped liability" fixes both.
    """
    phrases: list[str] = []
    for name, rx, phrase in _CLAUSE_RE:
        if phrase and rx.search(question):
            phrases.append(phrase)
    if "uncapped liability" in phrases and "liability cap" in phrases:
        phrases.remove("liability cap")  # the specific topic subsumes the general one
    return " ".join(phrases) if phrases else None


def clause_category_for(question: str) -> str | None:
    """CUAD category label for the question's (most specific) clause topic, or None."""
    for name, rx, _ in _CLAUSE_RE:  # list order = most specific first
        if name in TOPIC_TO_CUAD_CATEGORY and rx.search(question):
            return TOPIC_TO_CUAD_CATEGORY[name]
    return None


_CLASSIFY_PROMPT = """\
Classify which data source is needed to answer a procurement question.

Reply with exactly ONE word:
  rag  - the answer is in the text of vendor contracts (clauses, terms, obligations)
  sql  - the answer is in structured ERP data (vendors, spend, purchase orders, invoices, dates)
  both - the question needs structured data AND contract text

Question: {question}

One word (rag, sql, or both):"""


def route_question(question: str, generator=None) -> tuple[str, str]:
    """Return (route, human-readable reason). `route` is always one of rag | sql | both."""
    n_sql, n_rag, topics = signal_counts(question)

    if n_sql and n_rag:
        return "both", f"structured-data signals ({n_sql}) + contract-clause signals ({', '.join(topics)})"
    if n_sql:
        return "sql", f"structured-data signals ({n_sql}), no contract-clause signals"
    if n_rag:
        return "rag", f"contract-clause signals ({', '.join(topics)}), no structured-data signals"

    # No signal at all: a vague question. One LLM call as tiebreaker, safe default = "rag".
    if generator is not None and not getattr(generator, "is_fake", False):
        try:
            reply = generator.generate(_CLASSIFY_PROMPT.format(question=question)).strip().lower()
            match = re.search(r"\b(rag|sql|both)\b", reply)
            if match:
                return match.group(1), f"no keyword signals; LLM classified as {match.group(1)!r}"
        except Exception as e:  # a router must never crash the pipeline
            return "rag", f"no keyword signals; LLM classification failed ({type(e).__name__}), default rag"
    return "rag", "no keyword signals; default rag"
