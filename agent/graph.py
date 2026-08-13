from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent.nodes import (
    advance_next,
    deliver,
    evaluate,
    execute_tests,
    generate_tests,
    ingest_pr,
    plan_tests,
)
from agent.state import AgentState


def build_graph():
    g = StateGraph(AgentState)

    g.add_node("ingest_pr", ingest_pr)
    g.add_node("plan_tests", plan_tests)
    g.add_node("generate_tests", generate_tests)
    g.add_node("execute_tests", execute_tests)
    g.add_node("advance_next", advance_next)
    g.add_node("deliver", deliver)

    g.add_edge(START, "ingest_pr")
    g.add_edge("ingest_pr", "plan_tests")
    g.add_edge("plan_tests", "generate_tests")
    g.add_edge("generate_tests", "execute_tests")

    g.add_conditional_edges(
        "execute_tests",
        evaluate,
        {
            "retry": "generate_tests",
            "next": "advance_next",
            "done": "deliver",
        },
    )

    g.add_edge("advance_next", "plan_tests")
    g.add_edge("deliver", END)

    return g.compile()
