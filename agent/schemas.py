"""
Phase 7 — the strict structured response: every agent run ends with a validated RiskReport.

Design decision: the LLM is NOT asked to emit JSON. Free-tier models are unreliable at that, and a
model that can emit arbitrary structure is also a model an injected instruction can steer. Instead:

    prose  (summary)  <- written by the LLM, after sanitisation + output guard
    structure (findings, figures, citations, sql, confidence) <- built in plain Python from what
                         the TOOLS returned (SQL rows, exact clause lookup, retrieved chunks)

and Pydantic validates the whole thing (extra="forbid", constrained ids, bounded sizes). If
validation ever fails, build falls back to a minimal valid report that says so -- the pipeline
never returns an unvalidated or half-structured response.
"""
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

CONTRACT_ID_PATTERN = r"^\d{4}_\w+$"
_CONTRACT_ID_RE = re.compile(CONTRACT_ID_PATTERN)
MAX_FINDINGS = 200

ContractId = Annotated[str, StringConstraints(pattern=CONTRACT_ID_PATTERN)]
Scalar = Union[str, int, float, bool, None]


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_name: str | None = None
    contract_id: ContractId | None = None
    statement: str = Field(min_length=1, max_length=500)
    # How strongly the pipeline stands behind this finding:
    #   exact_label       - contract carries the expert CUAD label for the clause asked about
    #   database_row      - a row returned by the (validated, read-only) ERP query
    #   retrieved_excerpt - a contract excerpt retrieved by similarity search (NOT verified)
    #   cited_in_answer   - a contract the answer cited (and the critic confirmed was retrieved)
    basis: Literal["exact_label", "database_row", "retrieved_excerpt", "cited_in_answer"]
    figures: dict[str, Scalar] = Field(default_factory=dict)


class RiskReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    route: Literal["rag", "sql", "both"]
    summary: str = Field(min_length=1)
    findings: list[Finding] = Field(default_factory=list, max_length=MAX_FINDINGS)
    citations: list[ContractId] = Field(default_factory=list)
    sql_query: str | None = None
    confidence: Literal["high", "medium", "low"]
    verdict: Literal["accept", "give_up"]
    retries: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    guardrail_flags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# deterministic builder
# ---------------------------------------------------------------------------------------------
_NOTE_ID_RE = re.compile(r"^- \[(\d{4}_\w+)\]", re.MULTILINE)


def _scalar(v) -> Scalar:
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)


def _figures(row: dict) -> dict:
    return {k: _scalar(v) for k, v in row.items() if k not in ("vendor_name", "contract_id")}


def _valid_cid(value) -> str | None:
    return value if isinstance(value, str) and _CONTRACT_ID_RE.match(value) else None


def _build_findings(state: dict) -> list[Finding]:
    route = state["route"]
    rows = state.get("sql_rows", [])
    by_id = {r["contract_id"]: r for r in rows if _valid_cid(r.get("contract_id"))}
    findings: list[Finding] = []

    if route == "both" and state.get("clause_category"):
        category = state["clause_category"]
        for cid in _NOTE_ID_RE.findall(state.get("clause_note", "")):
            row = by_id.get(cid, {})
            findings.append(Finding(
                vendor_name=row.get("vendor_name"), contract_id=cid,
                statement=f"Contract has a chunk labelled '{category}' (exact label lookup).",
                basis="exact_label", figures=_figures(row),
            ))
    elif route == "both":
        seen = set()
        for c in state.get("retrieved_chunks", []):
            cid = c["contract_id"]
            if cid in seen or not _valid_cid(cid):
                continue
            seen.add(cid)
            row = by_id.get(cid, {})
            findings.append(Finding(
                vendor_name=row.get("vendor_name"), contract_id=cid,
                statement="Contract excerpt retrieved for the contract-text condition; "
                "not verified by an exact label.",
                basis="retrieved_excerpt", figures=_figures(row),
            ))
    elif route == "sql":
        for row in rows[:50]:
            findings.append(Finding(
                vendor_name=row.get("vendor_name") if isinstance(row.get("vendor_name"), str) else None,
                contract_id=_valid_cid(row.get("contract_id")),
                statement="Row returned by the ERP query.",
                basis="database_row", figures=_figures(row),
            ))
    else:  # rag
        for cid in state.get("citations", []):
            findings.append(Finding(
                contract_id=cid, statement="Contract cited in the answer.", basis="cited_in_answer",
            ))
    return findings[:MAX_FINDINGS]


def _confidence(state: dict) -> str:
    exact = state["route"] == "sql" or (state["route"] == "both" and state.get("clause_category"))
    level = "high" if exact else "medium"
    if state["verdict"] == "give_up":
        return "low"
    if state["retry_count"] > 0 and level == "high":
        level = "medium"
    if state.get("guardrail_flags") and level != "low":
        level = {"high": "medium", "medium": "low"}[level]
    return level


def _warnings(state: dict) -> list[str]:
    w = []
    if state["verdict"] == "give_up":
        w.append("The pipeline gave up after retries; treat this answer as unreliable.")
    if state.get("sql_error"):
        w.append(f"SQL step failed: {state['sql_error']}")
    if state.get("sql_truncated"):
        w.append("The ERP result hit the row cap; results may be incomplete.")
    if state["route"] == "both" and not state.get("clause_category"):
        w.append("No exact clause label applies to this question; contract-text findings come from "
                 "similarity search and cover only a subset of candidates.")
    if state.get("guardrail_flags"):
        w.append(f"{len(state['guardrail_flags'])} guardrail event(s): untrusted text was "
                 "modified before reaching the model (see guardrail_flags).")
    return w


def build_risk_report(state: dict) -> RiskReport:
    """Raises pydantic.ValidationError if anything is malformed (caller falls back)."""
    return RiskReport(
        question=state["question"],
        route=state["route"],
        summary=state["answer"].strip() or "(the model produced an empty answer)",
        findings=_build_findings(state),
        citations=state.get("citations", []),
        sql_query=state.get("sql_query") or None,
        confidence=_confidence(state),
        verdict="give_up" if state["verdict"] == "give_up" else "accept",
        retries=state["retry_count"],
        warnings=_warnings(state),
        guardrail_flags=state.get("guardrail_flags", []),
    )


def fallback_report(state: dict, error: str) -> RiskReport:
    """Minimal, always-valid report used if build_risk_report itself fails."""
    return RiskReport(
        question=state.get("question", ""),
        route=state.get("route") if state.get("route") in ("rag", "sql", "both") else "rag",
        summary=(state.get("answer") or "").strip() or "(no answer)",
        confidence="low",
        verdict="give_up",
        retries=state.get("retry_count", 0),
        warnings=[f"Structured report could not be built ({error}); returning prose only."],
    )
