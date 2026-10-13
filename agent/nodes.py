"""
LangGraph node implementations for the agentic pipeline.

Every node is a *factory function* (make_x_node) that takes the external dependencies it
needs (generator, Qdrant client, embedder, reranker, ...) and returns the actual node function —
a closure over those dependencies. LangGraph nodes are plain functions of (state) -> dict, so
closures are the simplest, most explicit way to inject dependencies without leaning on
LangGraph's own config/context-passing conventions.

Phase 6 changes vs Phase 5:
  * router   - real decision (rag | sql | both) via router.route_question, sticky across retries
  * sql_tool - NEW node: question -> validated read-only SQL -> rows (+ contract_ids for "both")
  * rag_tool - can be narrowed to the contracts SQL found (multi-hop), and diversified per contract
  * synthesizer - uses a combined SQL+excerpt prompt whenever SQL was involved
  * critic   - route-aware: checks SQL success, and only demands contract citations when contract
               text was actually part of the answer
  * planner  - on retry only rewrites the *search query* when the RAG side was what failed
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retrieval"))

from baseline_rag import build_prompt  # noqa: E402
from hybrid_retriever import hybrid_retrieve  # noqa: E402

from prompts import build_synthesis_prompt  # noqa: E402
from category_lookup import chunks_with_category  # noqa: E402
from guardrails import UNTRUSTED_NOTICE, guard_output, sanitize_chunks, sanitize_rows  # noqa: E402
from schemas import build_risk_report, fallback_report  # noqa: E402
from router import clause_category_for, clause_query_for, route_question  # noqa: E402
from sql_tool import run_sql_tool  # noqa: E402
from state import AgentState  # noqa: E402

MAX_CATEGORY_EXCERPTS = 25   # excerpts shown when the exact category lookup is used
EXCERPT_CHARS = 600          # per-excerpt text cap on that path (keeps the prompt small)
MAX_FILTER_CONTRACTS = 300   # cap on contract_ids pushed into the RAG filter
BOTH_CANDIDATES_K = 60       # wider candidate pool per retriever on a "both" route
BOTH_MAX_CHUNKS = 12         # excerpts handed to the synthesizer on a "both" route


_CONTRACT_ID_OK = re.compile(r"^\d{4}_\w+$")


def merge_flags(state: AgentState, new: list[str]) -> list[str]:
    """Append new guardrail flags to the running list, de-duplicated, order preserved."""
    return list(dict.fromkeys(state["guardrail_flags"] + new))


def best_per_contract(chunks: list[dict], max_n: int) -> list[dict]:
    """
    Keep only the highest-ranked chunk for each contract (input is already best-first), up to
    max_n. On a multi-hop question the SQL step may hand us dozens of candidate contracts; if we
    just took the top-k chunks, a few contracts with many near-duplicate clauses would crowd out
    all the others and most vendors would never be checked at all.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for c in chunks:
        if c["contract_id"] in seen:
            continue
        seen.add(c["contract_id"])
        out.append(c)
        if len(out) >= max_n:
            break
    return out


def append_missing_matches(answer: str, clause_note: str, sql_rows: list[dict]) -> str:
    """
    The exact clause lookup already KNOWS which vendors match; an LLM asked to re-list them can
    drop some (or, on a weak free-tier model, all of them). So after the model writes its answer,
    any matched vendor it failed to name is appended deterministically.
    """
    ids = re.findall(r"^- \[(\d{4}_\w+)\]", clause_note, re.MULTILINE)
    by_id = {r["contract_id"]: r for r in sql_rows if r.get("contract_id")}
    missing = []
    for cid in ids:
        name = by_id.get(cid, {}).get("vendor_name", "")
        # The EXACT vendor name must appear (case-insensitive, whole name). Citing the contract id is
        # NOT enough: a model can cite the right [id] yet mangle or drop the name.
        named = bool(name) and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", answer, re.IGNORECASE)
        if not named:
            missing.append(f"- {name or 'vendor'} [{cid}]")
    if not missing:
        return answer
    return answer.rstrip() + "\n\nMatches from the exact clause check not listed above:\n" + "\n".join(missing)


def build_clause_note(category: str, matched: list[dict], sql_rows: list[dict], n_candidates: int) -> str:
    """Deterministic (no LLM) summary of the exact clause lookup, using the SQL rows for context."""
    if not matched:
        return (
            f"Of the {n_candidates} contract(s) selected by the database query, NONE has a chunk "
            f"labelled {category!r}."
        )
    by_id = {r["contract_id"]: r for r in sql_rows if r.get("contract_id")}
    lines = [
        f"{len(matched)} of the {n_candidates} contract(s) selected by the database query have a "
        f"chunk labelled {category!r}:"
    ]
    for m in matched:
        row = by_id.get(m["contract_id"], {})
        details = ", ".join(f"{k}={v}" for k, v in row.items() if k != "contract_id")
        lines.append(f"- [{m['contract_id']}] {details}")
    return "\n".join(lines)


def make_planner_node(generator):
    def planner_node(state: AgentState) -> dict:
        if state["retry_count"] == 0:
            query = state["question"]
            note = "initial query"
        elif state["failure_source"] == "sql" or state["route"] == "sql":
            # The SQL tool repairs itself from the error message (see sql_tool_node); rewriting
            # the contract-search query would be meaningless here.
            return {
                "trace": state["trace"] + [
                    f"planner (retry {state['retry_count']}): SQL side failed, search query unchanged"
                ]
            }
        else:
            rewrite_prompt = (
                "A previous attempt to answer this procurement-contract question failed.\n"
                f"Question: {state['question']}\n"
                f"Previous search query: {state['search_query']!r}\n"
                f"Why it failed: {state['critic_feedback']}\n\n"
                "Suggest ONE alternative search query — different wording or emphasis — that "
                "might retrieve better-matching contract clauses. Reply with ONLY the query "
                "text, nothing else."
            )
            query = generator.generate(rewrite_prompt).strip().strip('"').strip()
            note = f"rewritten after: {state['critic_feedback']}"
        return {
            "search_query": query,
            "trace": state["trace"] + [f"planner (retry {state['retry_count']}): {note} -> {query!r}"],
        }

    return planner_node


def make_router_node(generator=None):
    """
    Phase 6: a real routing decision. The route is decided once, on the first attempt, and then
    kept on retries so a retry can't flip-flop between tools (the critic's feedback is about
    *how well* the chosen tools worked, not about whether they were the right tools).
    """

    def router_node(state: AgentState) -> dict:
        if state["retry_count"] > 0 and state["route"]:
            return {
                "trace": state["trace"] + [f"router: keeping route {state['route']!r} on retry"]
            }

        route, reason = route_question(state["question"], generator)
        update: dict = {"route": route}
        note = f"router: {route} ({reason})"

        if route == "both":
            clause_query = clause_query_for(state["question"])
            if clause_query:
                update["search_query"] = clause_query
                note += f"; contract search narrowed to {clause_query!r}"
        update["trace"] = state["trace"] + [note]
        return update

    return router_node


def make_sql_tool_node(generator, executor=None):
    def sql_tool_node(state: AgentState) -> dict:
        # A retry blamed on the RAG side (e.g. a bad citation) doesn't invalidate a SQL result
        # that already succeeded -- reuse it instead of paying for another LLM call + query.
        if (
            state["retry_count"] > 0
            and state["failure_source"] == "rag"
            and state["sql_query"]
            and not state["sql_error"]
        ):
            return {"trace": state["trace"] + ["sql_tool: reusing previous successful result"]}

        retrying_sql = state["retry_count"] > 0 and state["failure_source"] == "sql"
        result = run_sql_tool(
            state["question"],
            generator,
            want_contract_ids=(state["route"] == "both"),
            prior_sql=state["sql_query"] if retrying_sql else "",
            prior_error=state["sql_error"] if retrying_sql else "",
            executor=executor,
        )

        # contract ids come from the RAW rows but must look like real ids -- a poisoned cell can
        # never smuggle anything but a well-formed id into the retrieval filter.
        contract_ids: list[str] = []
        if state["route"] == "both" and not result["error"] and "contract_id" in result["columns"]:
            contract_ids = list(
                dict.fromkeys(
                    r["contract_id"] for r in result["rows"]
                    if isinstance(r["contract_id"], str) and _CONTRACT_ID_OK.match(r["contract_id"])
                )
            )[:MAX_FILTER_CONTRACTS]
        # ERP cell values are untrusted text too (anyone who can enter data can plant an instruction)
        clean_rows, row_flags = sanitize_rows(result["rows"])

        if result["error"]:
            note = f"sql_tool: FAILED ({result['error']})"
        else:
            note = f"sql_tool: {len(result['rows'])} row(s)" + (" (truncated)" if result["truncated"] else "")
            if contract_ids:
                note += f", {len(contract_ids)} contract(s) to filter on"
        return {
            "sql_query": result["sql"],
            "sql_columns": result["columns"],
            "sql_rows": clean_rows,
            "guardrail_flags": merge_flags(state, row_flags),
            "sql_truncated": result["truncated"],
            "sql_error": result["error"],
            "contract_filter": contract_ids,
            "trace": state["trace"] + [note, f"  sql> {result['sql']}"],
        }

    return sql_tool_node


def make_rag_tool_node(qdrant_client, embedder, reranker, retrieve_fn=None, category_fn=None):
    """`category_fn`: None -> real Postgres lookup; False -> disabled (semantic path only)."""
    retrieve = retrieve_fn or hybrid_retrieve
    if category_fn is None:
        category_fn = chunks_with_category

    def rag_tool_node(state: AgentState) -> dict:
        if state["route"] != "both":
            chunks = retrieve(state["search_query"], qdrant_client, embedder, reranker, final_k=8)
            chunks, flags = sanitize_chunks(chunks)
            return {
                "retrieved_chunks": chunks,
                "guardrail_flags": merge_flags(state, flags),
                "trace": state["trace"] + [f"rag_tool: retrieved {len(chunks)} chunks"]
                + ([f"guardrails: {len(flags)} untrusted-text event(s) in retrieved chunks"] if flags else []),
            }

        # ---- "both": multi-hop. SQL already ran; narrow the contract search with its output.
        ids = state["contract_filter"]
        category = clause_category_for(state["question"]) if category_fn else None
        if ids and category:
            try:
                matched = category_fn(category, ids)
            except Exception as e:  # DB hiccup -> degrade to semantic search, don't fail the run
                matched = None
                category_note = f"rag_tool: exact category lookup failed ({type(e).__name__}); using semantic search"
            if matched is not None:
                shown = [
                    {**m, "text": m["text"][:EXCERPT_CHARS]} for m in matched[:MAX_CATEGORY_EXCERPTS]
                ]
                shown, flags = sanitize_chunks(shown)
                return {
                    "guardrail_flags": merge_flags(state, flags),
                    "retrieved_chunks": shown,
                    "clause_category": category,
                    "clause_note": build_clause_note(category, matched, state["sql_rows"], len(ids)),
                    "trace": state["trace"] + [
                        f"rag_tool: exact lookup of {category!r} across {len(ids)} SQL-matched "
                        f"contract(s) -> {len(matched)} have it"
                    ],
                }
        else:
            category_note = ""
        if ids:
            candidates = retrieve(
                state["search_query"], qdrant_client, embedder, reranker,
                dense_k=BOTH_CANDIDATES_K, keyword_k=BOTH_CANDIDATES_K, final_k=BOTH_CANDIDATES_K,
                contract_ids=ids,
            )
            chunks = best_per_contract(candidates, BOTH_MAX_CHUNKS)
            note = (
                f"rag_tool: searched within {len(ids)} SQL-matched contract(s) -> "
                f"{len(candidates)} candidate chunks -> kept best chunk for {len(chunks)} contract(s)"
            )
        elif not state["sql_rows"]:
            chunks = []
            note = "rag_tool: skipped -- SQL matched no vendors, so there are no contracts to check"
        else:
            chunks = retrieve(state["search_query"], qdrant_client, embedder, reranker, final_k=8)
            note = (
                f"rag_tool: SQL result had no contract_id column, searched all contracts "
                f"-> {len(chunks)} chunks"
            )
        chunks, flags = sanitize_chunks(chunks)
        trace = state["trace"] + ([category_note] if category_note else []) + [note]
        if flags:
            trace.append(f"guardrails: {len(flags)} untrusted-text event(s) in retrieved chunks")
        return {
            "retrieved_chunks": chunks,
            "guardrail_flags": merge_flags(state, flags),
            "clause_category": "",
            "clause_note": "",
            "trace": trace,
        }

    return rag_tool_node


def make_synthesizer_node(generator):
    def synthesizer_node(state: AgentState) -> dict:
        if state["route"] == "rag":
            # Unchanged from Phase 5: contract-only questions use the baseline prompt.
            prompt = UNTRUSTED_NOTICE + build_prompt(state["question"], state["retrieved_chunks"])
        else:
            prompt = build_synthesis_prompt(
                state["question"],
                state["sql_columns"],
                state["sql_rows"],
                sql_ran=True,
                chunks=state["retrieved_chunks"],
                truncated=state["sql_truncated"],
                clause_note=state["clause_note"],
                contract_text_needed=(state["route"] == "both"),
            )
        answer = generator.generate(prompt)
        note = "synthesizer: answer generated"
        if state["route"] == "both" and state["clause_note"]:
            completed = append_missing_matches(answer, state["clause_note"], state["sql_rows"])
            if completed != answer:
                note += " (appended matches the model omitted)"
            answer = completed
        # Output guard: strip exfiltration channels (markdown images / URLs the model was never given)
        allowed = " ".join(
            [state["question"], str(state["sql_rows"])] + [c["text"] for c in state["retrieved_chunks"]]
        )
        answer, out_flags = guard_output(answer, allowed)
        if out_flags:
            note += f" (output guard: {', '.join(out_flags)})"
        return {
            "answer": answer,
            "guardrail_flags": merge_flags(state, out_flags),
            "trace": state["trace"] + [note],
        }

    return synthesizer_node


def make_critic_node():
    """
    Deterministic, route-aware grounding check — no extra LLM call needed.

    * SQL failed (any route that uses SQL)  -> retry; the SQL node sees the error and rewrites.
    * route "sql"                           -> accept a non-empty answer (numbers come from the DB).
    * route "both" and SQL matched nothing  -> accept: "no vendors match" is a legitimate answer
                                               with no contract text to cite.
    * everything else (contract text used)  -> the Phase 5 citation-grounding check: every
                                               [contract_id] cited must have been retrieved.
    """

    def critic_node(state: AgentState) -> dict:
        # Contract IDs always follow extract_cuad_contracts.py's format: a zero-padded 4-digit
        # index, underscore, slugified title (e.g. "0107_BELLRINGBRANDS_INC_..."). Matching
        # only that specific shape -- not any bracketed word-text -- matters because CUAD's
        # actual contract text contains redaction placeholders like [Redacted] or [***], which
        # a real LLM will often quote verbatim while discussing a redacted amount. A looser
        # regex here would misidentify a genuinely well-grounded answer as a hallucinated
        # citation and trigger an unnecessary (and misleading) retry.
        cited_contract_ids = set(re.findall(r"\[(\d{4}_\w+)\]", state["answer"]))
        retrieved_ids = {c["contract_id"] for c in state["retrieved_chunks"]}
        route = state["route"]

        feedback = ""
        verdict = "accept"
        failure_source = ""

        if route in ("sql", "both") and state["sql_error"]:
            feedback = f"SQL step failed: {state['sql_error']}"
            verdict, failure_source = "retry", "sql"
        elif route == "sql":
            if not state["answer"].strip():
                feedback, verdict, failure_source = "empty answer", "retry", "sql"
        elif route == "both" and not state["sql_rows"]:
            pass  # legitimately empty: nothing matched the structured conditions
        elif route == "both" and state["clause_category"]:
            pass  # exact clause lookup ran across ALL candidates; "0 or N matches" is a complete answer
        else:
            ungrounded = cited_contract_ids - retrieved_ids
            no_answer_signal = any(
                phrase in state["answer"].lower()
                for phrase in ["doesn't contain", "does not contain", "cannot find", "no information"]
            )
            if ungrounded:
                # Plain comma-separated text, not a Python list repr (avoid brackets here too --
                # anything that looks like citation syntax in this feedback can get echoed back
                # literally into the planner's rewritten search query).
                feedback = (
                    f"answer cited a source contract ({', '.join(sorted(ungrounded))}) that wasn't "
                    "in the retrieved context (possible hallucinated citation)"
                )
                verdict, failure_source = "retry", "rag"
            elif not cited_contract_ids:
                feedback = "answer didn't cite any source contract at all"
                verdict, failure_source = "retry", "rag"
            elif no_answer_signal:
                feedback = "the retrieved context didn't seem to contain the answer"
                verdict, failure_source = "retry", "rag"

        retry_count = state["retry_count"]
        update_answer: dict = {}
        if verdict == "retry":
            if retry_count >= state["max_retries"]:
                verdict = "give_up"
                if state["sql_error"] and route in ("sql", "both"):
                    update_answer = {
                        "answer": "I couldn't retrieve the structured spend/contract data needed "
                        f"to answer this ({state['sql_error']})."
                    }
            else:
                retry_count += 1

        return {
            **update_answer,
            "citations": sorted(cited_contract_ids),
            "verdict": verdict,
            "critic_feedback": feedback,
            "failure_source": failure_source,
            "retry_count": retry_count,
            "trace": state["trace"] + [
                f"critic: verdict={verdict}" + (f" ({feedback})" if feedback else "")
            ],
        }

    return critic_node


def make_report_node():
    """
    Final node: every response -- accepted OR given-up -- leaves the graph as a validated
    RiskReport (schemas.py). If building/validating it fails for any reason, a minimal valid
    fallback report is returned instead, so the output is never unstructured.
    """

    def report_node(state: AgentState) -> dict:
        try:
            report = build_risk_report(state)
            note = f"report: validated RiskReport (confidence={report.confidence}, {len(report.findings)} finding(s))"
        except Exception as e:  # noqa: BLE001 -- includes pydantic.ValidationError
            report = fallback_report(state, f"{type(e).__name__}: {str(e)[:150]}")
            note = "report: build FAILED validation -> fallback report"
        return {"report": report.model_dump(), "trace": state["trace"] + [note]}

    return report_node
