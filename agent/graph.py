from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent.nodes import (
    advance_next,
    classify_failure,
    compute_coverage_gaps,
    deliver,
    evaluate,
    execute_tests,
    generate_tests,
    ingest_pr,
    plan_tests,
    run_existing_suite,
)
from agent.state import AgentState


def build_graph():
    g = StateGraph(AgentState)

    g.add_node("ingest_pr", ingest_pr)
    g.add_node("run_existing_suite", run_existing_suite)
    g.add_node("compute_coverage_gaps", compute_coverage_gaps)
    g.add_node("plan_tests", plan_tests)
    g.add_node("generate_tests", generate_tests)
    g.add_node("execute_tests", execute_tests)
    g.add_node("classify_failure", classify_failure)
    g.add_node("advance_next", advance_next)
    g.add_node("deliver", deliver)

    g.add_edge(START, "ingest_pr")
    g.add_edge("ingest_pr", "run_existing_suite")
    g.add_edge("run_existing_suite", "compute_coverage_gaps")
    g.add_edge("compute_coverage_gaps", "plan_tests")
    g.add_edge("plan_tests", "generate_tests")
    g.add_edge("generate_tests", "execute_tests")

    g.add_conditional_edges(
        "execute_tests",
        evaluate,
        {
            "retry": "generate_tests",
            "classify": "classify_failure",
            "next": "advance_next",
            "done": "deliver",
        },
    )

    g.add_conditional_edges(
        "classify_failure",
        lambda s: "next" if (s["current_index"] + 1) < len(s["work_items"]) else "done",
        {"next": "advance_next", "done": "deliver"},
    )

    g.add_edge("advance_next", "plan_tests")
    g.add_edge("deliver", END)

    return g.compile()
