"""
Phase 6 — pure, dependency-free scoring helpers for run_agent_eval.py (same philosophy as
metrics.py: no imports from retrieval/ingestion/agent, so they can be unit-tested anywhere).

Multi-hop questions are scored TWICE, on purpose, because they answer different questions:

  tool_*   - the set of contracts the pipeline's TOOLS surfaced (SQL narrowing + exact clause
             lookup). Independent of how the LLM words its answer. Measures "did the machinery
             find the right contracts?"
  answer_* - the set of vendors the final ANSWER TEXT names (among the vendors SQL returned).
             Measures "did the user actually get told the right vendors?"

A gap between the two means the LLM mangled correct tool output (a prompt/model problem), not a
retrieval problem. mention_recall is the Phase 4 baseline's own metric, kept so before/after
numbers are directly comparable.
"""
import re
import statistics
from collections import defaultdict


def set_prf(predicted: set, expected: set) -> dict:
    """Precision / recall / F1 / exact-match of two sets."""
    hits = len(predicted & expected)
    precision = hits / len(predicted) if predicted else (1.0 if not expected else 0.0)
    recall = hits / len(expected) if expected else 1.0
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1, "exact": predicted == expected}


def names_in_text(names, text: str) -> set:
    """Which of `names` appear in `text` (case-insensitive, whole-name match, not substring-of-word)."""
    found = set()
    for name in names:
        if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
            found.add(name)
    return found


_NOTE_ID_RE = re.compile(r"^- \[(\d{4}_\w+)\]", re.MULTILINE)


def predicted_contract_ids(state: dict) -> set:
    """
    Contracts the tools surfaced. Prefer the clause note (it lists EVERY exact-lookup match, while
    retrieved_chunks is capped for prompt size); otherwise fall back to the retrieved chunks.
    """
    ids = set(_NOTE_ID_RE.findall(state.get("clause_note") or ""))
    if ids:
        return ids
    return {c["contract_id"] for c in state.get("retrieved_chunks", [])}


def expected_route(item_type: str) -> str:
    return "both" if item_type == "multi_hop" else "rag"


def _mean(values):
    values = list(values)
    return statistics.mean(values) if values else None


def aggregate_agent_metrics(results: list[dict]) -> dict:
    ok = [r for r in results if not r.get("error")]
    multi = [r for r in ok if r["type"] == "multi_hop"]
    single = [r for r in ok if r["type"] == "single_hop"]

    by_cat = defaultdict(list)
    for r in single:
        by_cat[r["clause_category"]].append(r["chunk_hit"])

    out = {
        "items_total": len(results),
        "items_errored": len(results) - len(ok),
        "route_accuracy_overall": _mean(r["route_ok"] for r in ok),
        "route_accuracy_single_hop": _mean(r["route_ok"] for r in single),
        "route_accuracy_multi_hop": _mean(r["route_ok"] for r in multi),
        "avg_retries": _mean(r["retries"] for r in ok),
        "verdicts": {v: sum(1 for r in ok if r["verdict"] == v) for v in sorted({r["verdict"] for r in ok})},
        "sql_error_items": sum(1 for r in ok if r.get("sql_error")),
        "avg_latency_s": _mean(r["latency_s"] for r in ok),
        "multi_hop_count": len(multi),
        "multi_hop_tool_precision": _mean(r["tool"]["precision"] for r in multi),
        "multi_hop_tool_recall": _mean(r["tool"]["recall"] for r in multi),
        "multi_hop_tool_f1": _mean(r["tool"]["f1"] for r in multi),
        "multi_hop_tool_exact_match_rate": _mean(r["tool"]["exact"] for r in multi),
        "multi_hop_answer_precision": _mean(r["answer_score"]["precision"] for r in multi),
        "multi_hop_answer_recall": _mean(r["answer_score"]["recall"] for r in multi),
        "multi_hop_answer_f1": _mean(r["answer_score"]["f1"] for r in multi),
        "multi_hop_avg_vendor_mention_recall": _mean(r["vendor_mention_recall"] for r in multi),
        "single_hop_count": len(single),
        "single_hop_chunk_hit_rate": _mean(r["chunk_hit"] for r in single),
        "single_hop_hit_rate_by_category": {c: statistics.mean(h) for c, h in sorted(by_cat.items())},
    }
    return out
