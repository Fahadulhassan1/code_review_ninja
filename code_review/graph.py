"""LangGraph orchestration for the code review multi-agent system.

Graph topology (sequential):
    Security → Performance → Style → Docs → Aggregator

Each specialist agent writes to its own state field so ordering
does not affect correctness — they are independent analyses.
The aggregator combines all findings into the final review comment.
"""

from langgraph.graph import END, StateGraph

from code_review.agents import (
    aggregator_agent,
    docs_agent,
    performance_agent,
    security_agent,
    style_agent,
)
from code_review.state import ReviewState


def build_review_graph():
    """Build the code review LangGraph graph.

    Agents run sequentially: security → performance → style → docs → aggregator.
    Each agent is independent and only writes to its own state field.
    """
    graph = StateGraph(ReviewState)

    graph.add_node("security", security_agent)
    graph.add_node("performance", performance_agent)
    graph.add_node("style", style_agent)
    graph.add_node("docs", docs_agent)
    graph.add_node("aggregator", aggregator_agent)

    graph.set_entry_point("security")
    graph.add_edge("security", "performance")
    graph.add_edge("performance", "style")
    graph.add_edge("style", "docs")
    graph.add_edge("docs", "aggregator")
    graph.add_edge("aggregator", END)

    return graph.compile()
