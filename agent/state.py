"""
Shared state schema for the LangGraph agentic pipeline.

Phase 5 introduced the base fields. Phase 6 adds everything the SQL tool and the multi-tool
router need (marked "Phase 6" below). Every node takes an AgentState and returns a partial dict
of fields to update — LangGraph merges that dict into the running state.
"""
from typing import TypedDict


class AgentState(TypedDict):
    question: str                 # the original user question — never mutated after entry
    search_query: str             # query sent to hybrid_retrieve; planner rewrites it on a RAG retry,
                                  # router narrows it to the clause topic on a "both" route
    route: str                    # router's decision: "rag" | "sql" | "both"   (Phase 6: was always "rag")
    retrieved_chunks: list[dict]  # output of hybrid_retrieve
    answer: str                   # generator's answer text
    citations: list[str]          # contract_ids the answer actually cited (parsed by the critic)
    verdict: str                  # critic's verdict: "accept" | "retry" | "give_up"
    critic_feedback: str          # why the critic rejected the answer — fed back to planner / SQL node
    retry_count: int
    max_retries: int
    trace: list[str]              # human-readable log of which nodes ran and why

    # ---- Phase 6 ----
    sql_query: str                # the validated SQL that was executed (or the rejected raw SQL on error)
    sql_columns: list[str]        # result column names
    sql_rows: list[dict]          # result rows (capped at sql_tool.MAX_ROWS)
    sql_truncated: bool           # True if the row cap cut the result short
    sql_error: str                # non-empty if generation / validation / execution failed
    contract_filter: list[str]    # contract_ids found by SQL, used to narrow RAG on a "both" route
    failure_source: str           # which tool the critic blamed on a retry: "sql" | "rag" | ""
    clause_category: str          # CUAD category used for the exact clause lookup on a "both" route ("" if none)
    clause_note: str              # deterministic summary of that lookup, handed to the synthesizer

    # ---- Phase 7 ----
    guardrail_flags: list[str]    # every sanitisation / output-guard event (untrusted text that was altered)
    report: dict                  # the validated RiskReport (as a dict) -- the pipeline's final structured output

