"""
Builds and compiles the LangGraph agentic pipeline.

Phase 5 shape (single tool):
    Planner -> Router -> RAG tool -> Synthesizer -> Critic -(retry)-> Planner

Phase 6 shape (the Router now really branches):

    START -> Planner -> Router --rag---> RAG tool ------------------------> Synthesizer
                          |                                                      ^
                          +--sql---> SQL tool --ok, route=sql--------------------+
                          |             |                                        |
                          +--both-> SQL tool --ok, route=both--> RAG tool -------+
                                        |
                                        +--error--> Critic  (skips synthesis; critic sends it back)

    Synthesizer -> Critic --accept / give_up--> Report (validated RiskReport) -> END   [Phase 7]
                          --retry-----------> Planner

On a "both" route SQL runs FIRST and its output (the contract_ids of vendors matching the
structured conditions) narrows the contract-text search -- that's what makes it a genuine
multi-hop pipeline rather than two independent lookups glued together.

Usage:
    python agent/graph.py "what is the liability cap for our vendors" --provider fake --fake-embedder --fake-reranker
    python agent/graph.py "top 5 vendors by total spend" --provider gemini
    python agent/graph.py "which vendors with spend above $1M have an uncapped liability clause" --provider gemini
"""
import argparse
import sys
from pathlib import Path

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retrieval"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestion"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retrieval"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestion"))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from nodes import (  # noqa: E402
    make_critic_node,
    make_planner_node,
    make_rag_tool_node,
    make_report_node,
    make_router_node,
    make_sql_tool_node,
    make_synthesizer_node,
)
from prompts import format_rows_for_prompt  # noqa: E402
from state import AgentState  # noqa: E402
from observability import flush, init_tracing, trace_request, traced_node, wrap_generator  # noqa: E402

from embedder import FakeEmbedder, SentenceTransformerEmbedder  # noqa: E402
from generator import get_generator  # noqa: E402
from reranker import CrossEncoderReranker, FakeReranker  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

RECURSION_LIMIT = 60

def _after_sql(state: AgentState) -> str:
    """Where to go once the SQL tool has run."""
    if state["sql_error"]:
        return "critic"          # nothing to synthesize; the critic will route back for a SQL retry
    if state["route"] == "both":
        return "rag_tool"        # SQL narrowed the candidates -> now check their contract text
    return "synthesizer"         # pure-SQL question: the rows are the whole answer


def build_agent_graph(
    qdrant_client, embedder, reranker, generator, sql_executor=None, retrieve_fn=None, category_fn=None
):
    """
    `sql_executor`, `retrieve_fn` and `category_fn` are injection points for tests (run the whole
    graph with no Postgres / Qdrant). In normal use leave them None. category_fn=False disables the
    exact clause-category lookup on the "both" route.
    """
    generator = wrap_generator(generator)
    planner = make_planner_node(generator)
    router = make_router_node(generator)
    sql_tool = make_sql_tool_node(generator, executor=sql_executor)
    rag_tool = make_rag_tool_node(
        qdrant_client, embedder, reranker, retrieve_fn=retrieve_fn, category_fn=category_fn
    )
    synthesizer = make_synthesizer_node(generator)
    critic = make_critic_node()
    report = make_report_node()

    graph = (
        StateGraph(AgentState)
        .add_node("planner", traced_node("planner", planner))
        .add_node("router", traced_node("router", router))
        .add_node("sql_tool", traced_node("sql_tool", sql_tool))
        .add_node("rag_tool", traced_node("rag_tool", rag_tool))
        .add_node("synthesizer", traced_node("synthesizer", synthesizer))
        .add_node("critic", traced_node("critic", critic))
        .add_node("report", traced_node("report", report))
        .add_edge(START, "planner")
        .add_edge("planner", "router")
        .add_conditional_edges(
            "router", lambda s: s["route"], {"rag": "rag_tool", "sql": "sql_tool", "both": "sql_tool"}
        )
        .add_conditional_edges(
            "sql_tool", _after_sql,
            {"rag_tool": "rag_tool", "synthesizer": "synthesizer", "critic": "critic"},
        )
        .add_edge("rag_tool", "synthesizer")
        .add_edge("synthesizer", "critic")
        .add_conditional_edges(
            "critic", lambda s: s["verdict"], {"retry": "planner", "accept": "report", "give_up": "report"}
        )
        .add_edge("report", END)
        .compile()
        )
    # A "both" attempt is 6 nodes; max_retries=3 means 6*4 + 1 report = 25 steps, exactly LangGraph's
    # default recursion limit. 60 safely covers max_retries up to 8.
    return graph.with_config(recursion_limit=RECURSION_LIMIT)


def initial_state(question: str, max_retries: int = 2) -> AgentState:
    return {
        "question": question,
        "search_query": "",
        "route": "",
        "retrieved_chunks": [],
        "answer": "",
        "citations": [],
        "verdict": "",
        "critic_feedback": "",
        "retry_count": 0,
        "max_retries": max_retries,
        "trace": [],
        "sql_query": "",
        "sql_columns": [],
        "sql_rows": [],
        "sql_truncated": False,
        "sql_error": "",
        "contract_filter": [],
        "failure_source": "",
        "clause_category": "",
        "clause_note": "",
        "guardrail_flags": [],
        "report": {},
    }


def run_agent(
    question: str, qdrant_client, embedder, reranker, generator, max_retries: int = 2,
    sql_executor=None, retrieve_fn=None, category_fn=None,
) -> dict:
    graph = build_agent_graph(
        qdrant_client, embedder, reranker, generator,
        sql_executor=sql_executor, retrieve_fn=retrieve_fn, category_fn=category_fn,
    )
    with trace_request("procureiq.run_agent", question, tags=["cli"]) as t:
        final_state = graph.invoke(initial_state(question, max_retries))
        t.result_from_state(final_state)
    return final_state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument(
        "--provider", choices=["openrouter", "huggingface", "gemini", "fake"], default="fake"
    )
    parser.add_argument("--url", default="http://localhost:6333")
    parser.add_argument("--local-path", default=None, help="Use file-based local Qdrant instead of --url")
    parser.add_argument("--fake-embedder", action="store_true", help="Offline plumbing test only")
    parser.add_argument("--fake-reranker", action="store_true", help="Offline plumbing test only")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--json", action="store_true", help="Print only the validated RiskReport as JSON")
    args = parser.parse_args()

    embedder = FakeEmbedder() if args.fake_embedder else SentenceTransformerEmbedder()
    reranker = FakeReranker() if args.fake_reranker else CrossEncoderReranker()
    client = (
        QdrantClient(path=args.local_path)
        if args.local_path
        else QdrantClient(url=args.url)
    )
    generator = get_generator(args.provider)    

    from mcp_client import get_sql_executor
    sql_executor = get_sql_executor()

    init_tracing()
    result = run_agent(args.question, client, embedder, reranker, generator, max_retries=args.max_retries)
    flush()
    
    if args.json:
        import json

        print(json.dumps(result["report"], indent=2))
        sys.exit(0)

    print("QUESTION:", result["question"])
    print(f"ROUTE: {result['route']}")
    print("\nTRACE:")
    for line in result["trace"]:
        print("  -", line)
    print(f"\nFINAL VERDICT: {result['verdict']} (after {result['retry_count']} retries)")

    if result["clause_note"]:
        print("\nEXACT CLAUSE CHECK:\n" + result["clause_note"])
    if result["sql_query"]:
        print("\nSQL RESULT:")
        print(format_rows_for_prompt(result["sql_columns"], result["sql_rows"], max_rows=10))
    if result["guardrail_flags"]:
        print("\nGUARDRAIL EVENTS:")
        for f in result["guardrail_flags"]:
            print("  -", f)
    print(f"\nREPORT: confidence={result['report']['confidence']}, "
          f"{len(result['report']['findings'])} finding(s), {len(result['report']['warnings'])} warning(s)  (use --json for full report)")
    print("\nANSWER:\n", result["answer"])
    if result["retrieved_chunks"]:
        print("\nSOURCES:")
        for c in result["retrieved_chunks"]:
            print("  -", c["contract_id"])
