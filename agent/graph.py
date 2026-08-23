from langgraph.graph import StateGraph, END

from agent.state import ReviewState
from agent.nodes import (
    fetch_diff_node,
    retrieve_context_node,
    static_analysis_node,
    llm_review_node,
    prioritize_node,
    post_comments_node,
)


def build_review_graph():
    graph = StateGraph(ReviewState)

    graph.add_node("fetch_diff", fetch_diff_node)
    graph.add_node("retrieve_context", retrieve_context_node)
    graph.add_node("static_analysis", static_analysis_node)
    graph.add_node("llm_review", llm_review_node)
    graph.add_node("prioritize", prioritize_node)
    graph.add_node("post_comments", post_comments_node)

    graph.set_entry_point("fetch_diff")
    graph.add_edge("fetch_diff", "retrieve_context")
    graph.add_edge("retrieve_context", "static_analysis")
    graph.add_edge("static_analysis", "llm_review")
    graph.add_edge("llm_review", "prioritize")
    graph.add_edge("prioritize", "post_comments")
    graph.add_edge("post_comments", END)

    return graph.compile()


# A module-level compiled instance, ready to invoke
review_app = build_review_graph()