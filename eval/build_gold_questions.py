"""
Phase 4 — build a gold (question, expected-answer) evaluation set.

Two question types are generated:

1. SINGLE-HOP: "what is the liability cap in [Vendor]'s contract?" — answerable from one
   contract, one clause category. Ground truth comes straight from CUAD's own expert-labeled
   clause spans (data/raw/cuad/clauses.jsonl), so there is no manual labeling step: CUAD
   already tells us which contract/category pairs exist and exactly which chunk(s) of text
   contain the answer. This is what "semi-automatic" means in docs/PRD.md's Phase 4 section.

2. MULTI-HOP: "which vendors with total spend above $X have contracts that are
   auto-renewing / include an uncapped-liability clause / etc.?" — answerable only by joining
   the clause-level data with the structured ERP data (vendor spend from purchase_orders.csv,
   auto_renewal/expiration_date from erp/contracts.csv). This is the question shape the
   agentic Phase 5+ pipeline exists to handle well; the baseline (single retrieval pass, no
   tool use) is expected to struggle with these, which is exactly the gap Phase 4 measures.

For every gold item we record enough to score both retrieval and generation later:
  - expected_source_chunk_ids: which chunk(s) in data/processed/chunks.jsonl actually contain
    the answer (found by overlapping the CUAD clause's char span with each chunk's char span).
    This lets Phase 4's eval harness compute a plain "did retrieval even find the right chunk"
    hit-rate WITHOUT needing an LLM judge — a fast, free, fully-offline check.
  - ground_truth: a reference answer string, used as the "ground truth" input RAGAS metrics
    (context_recall, context_precision) compare retrieved/generated content against.

Usage:
    python eval/build_gold_questions.py
    python eval/build_gold_questions.py --max-per-category 10 --seed 7
    python eval/build_gold_questions.py --out data/eval/gold_questions.jsonl

Everything in this script is pure Python + pandas over local CSV/JSONL files — no DB, no
Qdrant, no embedding model, no LLM. Fully testable offline, which is why it was built and
verified in-sandbox against your real Phase 0-3 data before being handed back to you.
"""
import argparse
import json
import random
import re
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------------------------
# Picking a real, in-text referent for contracts with no ERP-linked vendor name.
#
# BUG FOUND (after a real run on real data -- see docs/PROJECT_OVERVIEW.md Phase 4 section):
# the first version of this script fell back to the raw CUAD/SEC filename-derived title (e.g.
# "VERTEXENERGYINC_08_14_2014-EX-10.24-OPERATION AND MAINTENANCE AGREEMENT") for the 81 of 104
# single-hop contracts with no ERP vendor link. That string never appears anywhere in the
# contract body, and retrieval/keyword_retriever.py's websearch_to_tsquery ANDs together every
# word in the question -- mixing "liability cap" with filename gibberish made keyword search
# structurally unable to match anything, and diluted the dense-search embedding too. Result:
# a real run came back with a 0/5 chunk hit-rate, entirely because of this phrasing bug, not
# because of anything about the retrieval pipeline itself being scored.
#
# Fix: use CUAD's own "Parties" clause labels instead -- these are spans of the *actual*
# contract text (company names extracted by CUAD's human annotators), so they have a genuine
# chance of being lexically present near the top of the document, exactly like a real user's
# question would ("what's the liability cap in Acme Corp's contract").
# --------------------------------------------------------------------------------------------
_GENERIC_PARTY_TERMS = {
    "company", "the company", "distributor", "consultant", "buyer", "seller", "purchaser",
    "vendor", "client", "customer", "the customer", "licensor", "licensee", "party", "parties",
    "co-trustee", "the buyer", "the seller", "the vendor", "the client", "the distributor",
    "the consultant", "the licensor", "the licensee", "supplier", "the supplier",
    "manufacturer", "the manufacturer", "landlord", "tenant", "borrower", "lender", "grantor",
    "grantee", "employer", "employee", "contractor", "the contractor", "provider",
    "the provider", "recipient", "the recipient", "user", "the user", "owner", "the owner",
    "operator", "the operator", "franchisor", "franchisee", "the franchisor",
    "the franchisee", "service provider", "the parties", "end-user",
}
_ENTITY_SUFFIX_RE = re.compile(
    r"\b(inc\.?|llc|l\.l\.c\.?|corp\.?|corporation|ltd\.?|co\.?|company|group|holdings|lp|l\.p\.?)\b",
    re.IGNORECASE,
)
_LABEL_PREFIX_RE = re.compile(r"^[A-Za-z0-9 /\-]{2,40}:\s*")


def _clean_party_text(text: str) -> str:
    """Strip a leading label like 'The buyer/End-User: ' down to the actual name."""
    t = _LABEL_PREFIX_RE.sub("", text.strip())
    return t.strip().strip(":").strip()


def pick_party_referent(party_texts: list[str]) -> str | None:
    """
    From CUAD's labeled 'Parties' spans for one contract, pick the best real-text referent:
    prefer a full legal entity name (has a suffix like Inc/LLC/Corp) over a generic role word
    (skipped entirely -- "the Company", "Buyer", etc. carry no retrieval signal) or a
    placeholder (rare CUAD annotation artifacts like blank underscores).
    """
    candidates = [_clean_party_text(t) for t in party_texts]
    candidates = [c for c in candidates if c and c.lower() not in _GENERIC_PARTY_TERMS and len(c) >= 4]
    candidates = [c for c in candidates if sum(ch.isalpha() for ch in c) >= 3]
    if not candidates:
        return None
    with_suffix = [c for c in candidates if _ENTITY_SUFFIX_RE.search(c)]
    pool = with_suffix if with_suffix else candidates
    return max(pool, key=len)


def load_parties(path: Path) -> dict[str, list[str]]:
    """contract_id -> list of raw 'Parties'-category clause_text spans."""
    by_contract: dict[str, list[str]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d["category"] == "Parties":
                by_contract.setdefault(d["contract_id"], []).append(d["clause_text"])
    return by_contract

# Clause categories chosen for single-hop questions: each has a natural procurement-relevant
# question phrasing AND enough labeled instances in CUAD (checked against the real
# clauses.jsonl: all of these have 100+ instances except Notice Period and MFN, which are
# kept for their high multi-hop relevance despite lower counts).
CATEGORY_TEMPLATES = {
    "Cap On Liability": "What is the liability cap in {referent}?",
    "Uncapped Liability": "Does {referent} include uncapped liability for either party?",
    "Governing Law": "Which state or country's law governs {referent}?",
    "Termination For Convenience": (
        "Can either party terminate {referent} for convenience, and what does the "
        "contract say about it?"
    ),
    "Renewal Term": "What is the renewal term for {referent}?",
    "Insurance": "What insurance requirements does {referent} impose?",
    "Non-Compete": "Does {referent} include a non-compete restriction?",
    "Audit Rights": "What audit rights does {referent} grant?",
    "Minimum Commitment": "What is the minimum commitment specified in {referent}?",
    "Warranty Duration": "What is the warranty duration in {referent}?",
    "Exclusivity": "Does {referent} include an exclusivity clause?",
    "Most Favored Nation": "Does {referent} include a most-favored-nation clause?",
    "Notice Period To Terminate Renewal": (
        "What notice period is required to stop {referent} from automatically renewing?"
    ),
}

# Multi-hop templates: each is (question, filter_fn) where filter_fn(row) -> bool decides
# whether a given ERP-linked contract (one row of the merged contracts+vendors+spend+clause
# frame) satisfies the question. Thresholds are computed at runtime from the real spend
# distribution so they're neither trivially empty nor trivially "everyone matches."
MULTI_HOP_SPECS = [
    {
        "id": "mh_liability_spend",
        "question_tpl": (
            "Which vendors with total purchase-order spend above ${threshold:,.0f} have "
            "contracts that include an uncapped liability clause?"
        ),
        "category": "Uncapped Liability",
        "spend_percentile": 75,
    },
    {
        "id": "mh_autorenew_termconv",
        "question_tpl": (
            "Which vendors have contracts that are set to auto-renew AND include a "
            "termination-for-convenience clause?"
        ),
        "category": "Termination For Convenience",
        "spend_percentile": None,
        "require_auto_renewal": True,
    },
    {
        "id": "mh_spend_exclusivity",
        "question_tpl": (
            "Which vendors with total purchase-order spend above ${threshold:,.0f} have "
            "contracts that include an exclusivity clause?"
        ),
        "category": "Exclusivity",
        "spend_percentile": 50,
    },
    {
        "id": "mh_expiring_soon_uncapped",
        "question_tpl": (
            "Which vendors have contracts expiring within {days} days of {as_of} that also "
            "include an uncapped liability clause?"
        ),
        "category": "Uncapped Liability",
        "spend_percentile": None,
        "expiring_within_days": 180,
    },
    {
        "id": "mh_spend_noncompete",
        "question_tpl": (
            "Which vendors with total purchase-order spend above ${threshold:,.0f} have "
            "contracts that include a non-compete restriction?"
        ),
        "category": "Non-Compete",
        "spend_percentile": 75,
    },
]


def load_clauses(path: Path) -> dict[str, list[dict]]:
    """contract_id -> list of {category, clause_text, char_start, char_end}."""
    by_contract: dict[str, list[dict]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            d["char_end"] = d["char_start"] + len(d["clause_text"])
            by_contract.setdefault(d["contract_id"], []).append(d)
    return by_contract


def load_chunks(path: Path) -> dict[str, list[dict]]:
    """contract_id -> list of {chunk_id, char_start, char_end}."""
    by_contract: dict[str, list[dict]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            by_contract.setdefault(d["contract_id"], []).append(d)
    return by_contract


def find_source_chunk_ids(clause: dict, chunks_for_contract: list[dict]) -> list[str]:
    """Any chunk whose [char_start, char_end) overlaps the clause's span is a source chunk."""
    lo, hi = clause["char_start"], clause["char_end"]
    return [
        c["chunk_id"]
        for c in chunks_for_contract
        if c["char_start"] < hi and c["char_end"] > lo
    ]


def build_single_hop(
    clauses_by_contract: dict,
    chunks_by_contract: dict,
    metadata_df: pd.DataFrame,
    vendor_name_by_contract: dict,
    parties_by_contract: dict,
    max_per_category: int,
    rng: random.Random,
) -> list[dict]:
    metadata_lookup = metadata_df.set_index("contract_id")["title"].to_dict()
    items = []
    for category, template in CATEGORY_TEMPLATES.items():
        # Every contract that has at least one labeled clause of this category.
        candidates = [
            (contract_id, clause)
            for contract_id, clauses in clauses_by_contract.items()
            for clause in clauses
            if clause["category"] == category
        ]
        rng.shuffle(candidates)
        taken = 0
        for contract_id, clause in candidates:
            if taken >= max_per_category:
                break
            chunks_for_contract = chunks_by_contract.get(contract_id, [])
            source_chunk_ids = find_source_chunk_ids(clause, chunks_for_contract)
            if not source_chunk_ids:
                # Shouldn't normally happen (every char offset should fall in some chunk) --
                # skip defensively rather than emit a gold item with no verifiable answer.
                continue

            vendor_name = vendor_name_by_contract.get(contract_id)
            party_referent = None
            if not vendor_name:
                party_referent = pick_party_referent(parties_by_contract.get(contract_id, []))

            if vendor_name:
                referent = f"{vendor_name}'s contract"
            elif party_referent:
                # A real name extracted from the contract's own text (CUAD's "Parties" label)
                # -- unlike the vendor name, this has a genuine chance of appearing near the
                # relevant chunk, giving both keyword and dense retrieval an actual anchor.
                referent = f"{party_referent}'s contract"
            else:
                # Last-resort fallback (should be rare -- 0/509 contracts hit this in testing).
                referent = f"the contract titled \"{metadata_lookup.get(contract_id, contract_id)}\""
            question = template.format(referent=referent)

            items.append(
                {
                    "id": f"sh_{len(items):04d}",
                    "type": "single_hop",
                    "question": question,
                    "contract_id": contract_id,
                    "vendor_name": vendor_name,
                    "clause_category": category,
                    "expected_source_chunk_ids": source_chunk_ids,
                    "ground_truth": clause["clause_text"],
                }
            )
            taken += 1
    return items


def build_multi_hop(
    clauses_by_contract: dict,
    erp_contracts_df: pd.DataFrame,
    vendors_df: pd.DataFrame,
    spend_by_vendor: dict,
    as_of: pd.Timestamp,
) -> list[dict]:
    vendor_name_by_id = vendors_df.set_index("vendor_id")["vendor_name"].to_dict()
    categories_by_contract = {
        cid: {c["category"] for c in clauses} for cid, clauses in clauses_by_contract.items()
    }

    items = []
    for spec in MULTI_HOP_SPECS:
        rows = []
        for _, row in erp_contracts_df.iterrows():
            contract_id = row["contract_id"]
            vendor_id = row["vendor_id"]
            cats = categories_by_contract.get(contract_id, set())
            if spec["category"] not in cats:
                continue
            if spec.get("require_auto_renewal") and not bool(row["auto_renewal"]):
                continue
            if spec.get("expiring_within_days") is not None:
                expiration = pd.Timestamp(row["expiration_date"])
                window_end = as_of + pd.Timedelta(days=int(spec["expiring_within_days"]))
                if not (as_of <= expiration <= window_end):
                    continue
            spend = spend_by_vendor.get(vendor_id, 0.0)
            threshold = None
            if spec.get("spend_percentile") is not None:
                threshold = spec["_threshold_value"]
                if spend <= threshold:
                    continue
            rows.append(
                {
                    "vendor_id": int(vendor_id),
                    "vendor_name": vendor_name_by_id.get(vendor_id, f"vendor_{vendor_id}"),
                    "contract_id": contract_id,
                    "total_spend": round(float(spend), 2),
                }
            )

        question = spec["question_tpl"].format(
            threshold=spec.get("_threshold_value") or 0,
            days=spec.get("expiring_within_days"),
            as_of=as_of.date().isoformat(),
        )
        vendor_names = sorted({r["vendor_name"] for r in rows})
        ground_truth = (
            f"{len(vendor_names)} vendor(s) match: " + ", ".join(vendor_names)
            if vendor_names
            else "No vendors currently match this combination of filters."
        )

        items.append(
            {
                "id": spec["id"],
                "type": "multi_hop",
                "question": question,
                "clause_category": spec["category"],
                "expected_vendor_ids": sorted({r["vendor_id"] for r in rows}),
                "expected_contract_ids": sorted({r["contract_id"] for r in rows}),
                "expected_matches": rows,
                "ground_truth": ground_truth,
            }
        )
    return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clauses", default=str(REPO_ROOT / "data/raw/cuad/clauses.jsonl"))
    parser.add_argument("--chunks", default=str(REPO_ROOT / "data/processed/chunks.jsonl"))
    parser.add_argument("--metadata", default=str(REPO_ROOT / "data/raw/cuad/contracts_metadata.csv"))
    parser.add_argument("--erp-contracts", default=str(REPO_ROOT / "data/processed/erp/contracts.csv"))
    parser.add_argument("--erp-vendors", default=str(REPO_ROOT / "data/processed/erp/vendors.csv"))
    parser.add_argument("--erp-pos", default=str(REPO_ROOT / "data/processed/erp/purchase_orders.csv"))
    parser.add_argument("--out", default=str(REPO_ROOT / "data/eval/gold_questions.jsonl"))
    parser.add_argument("--max-per-category", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--as-of",
        default=None,
        help="Reference date (YYYY-MM-DD) for 'expiring soon' questions. Defaults to today.",
    )
    args = parser.parse_args()

    rng = random.Random(args.seed)

    clauses_by_contract = load_clauses(Path(args.clauses))
    chunks_by_contract = load_chunks(Path(args.chunks))
    parties_by_contract = load_parties(Path(args.clauses))
    metadata_df = pd.read_csv(args.metadata)
    erp_contracts_df = pd.read_csv(args.erp_contracts)
    vendors_df = pd.read_csv(args.erp_vendors)
    pos_df = pd.read_csv(args.erp_pos)

    vendor_name_by_id = vendors_df.set_index("vendor_id")["vendor_name"].to_dict()
    vendor_name_by_contract = {
        row["contract_id"]: vendor_name_by_id.get(row["vendor_id"])
        for _, row in erp_contracts_df.iterrows()
    }
    spend_by_vendor = pos_df.groupby("vendor_id")["amount"].sum().to_dict()

    # Resolve each multi-hop spec's spend threshold now, from the real distribution, so the
    # question text and the filter use the exact same number.
    spend_series = pd.Series(list(spend_by_vendor.values()))
    for spec in MULTI_HOP_SPECS:
        if spec.get("spend_percentile") is not None:
            spec["_threshold_value"] = float(spend_series.quantile(spec["spend_percentile"] / 100))
        else:
            spec["_threshold_value"] = None

    as_of = pd.to_datetime(args.as_of) if args.as_of else pd.Timestamp.today().normalize()

    single_hop = build_single_hop(
        clauses_by_contract,
        chunks_by_contract,
        metadata_df,
        vendor_name_by_contract,
        parties_by_contract,
        args.max_per_category,
        rng,
    )
    multi_hop = build_multi_hop(clauses_by_contract, erp_contracts_df, vendors_df, spend_by_vendor, as_of)

    all_items = single_hop + multi_hop
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for item in all_items:
            f.write(json.dumps(item) + "\n")

    print(f"Wrote {len(all_items)} gold questions to {out_path}")
    print(f"  single-hop: {len(single_hop)} across {len(CATEGORY_TEMPLATES)} clause categories")
    print(f"  multi-hop:  {len(multi_hop)}")
    for item in multi_hop:
        print(f"    [{item['id']}] {len(item['expected_vendor_ids'])} matching vendor(s) -- {item['question']}")


if __name__ == "__main__":
    main()
