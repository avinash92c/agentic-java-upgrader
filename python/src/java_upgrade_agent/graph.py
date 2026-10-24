"""LangGraph wiring for the upgrade flow. Routing is deterministic graph logic, not LLM decisions."""

from langgraph.graph import END, START, StateGraph

from . import nodes, repair
from .state import AgentState


def after_preflight(state: AgentState) -> str:
    return "baseline_build" if state.get("preflight_ok") else "report"


def after_baseline(state: AgentState) -> str:
    return "extract_dependencies" if state.get("baseline_ok") else "report"


def after_extraction(state: AgentState) -> str:
    return "next_dependency" if state.get("extraction_ok") else "report"


def after_next_dependency(state: AgentState) -> str:
    if state.get("current") is not None:
        return "find_versions"
    # Build when something changed: upgrades, or a Java version update (whose baseline build was skipped).
    return "build" if state.get("applied") or state.get("java_update") else "report"


def after_find_versions(state: AgentState) -> str:
    current = state["current"]
    outdated = state.get("latest_version") not in (None, current.dependency.version)
    return "apply_version" if outdated else "next_dependency"


def after_build(state: AgentState) -> str:
    return "report" if state.get("build_ok") else "classify_failure"


def after_classify(state: AgentState) -> str:
    """Repair while some issue in the build log still has tries left (max_retries per issue), up to
    MAX_REPAIR_ROUNDS rounds in total; otherwise hand over to the user."""
    if state.get("failure_kind") == "environment":
        return "defer_to_user"
    if state.get("attempts", 0) >= nodes.MAX_REPAIR_ROUNDS:
        return "defer_to_user"
    issue = repair.next_issue(state.get("build_log", ""), state.get("issue_tries", {}), state.get("max_retries", 3))
    return "repair_agent" if issue else "defer_to_user"


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("preflight", nodes.preflight)
    graph.add_node("baseline_build", nodes.baseline_build)
    graph.add_node("extract_dependencies", nodes.extract_dependencies)
    graph.add_node("next_dependency", nodes.next_dependency)
    graph.add_node("find_versions", nodes.find_versions)
    graph.add_node("apply_version", nodes.apply_version)
    graph.add_node("build", nodes.build)
    graph.add_node("classify_failure", nodes.classify_failure)
    graph.add_node("repair_agent", nodes.repair_agent)
    graph.add_node("defer_to_user", nodes.defer_to_user)
    graph.add_node("report", nodes.report)

    graph.add_edge(START, "preflight")
    graph.add_conditional_edges("preflight", after_preflight, ["baseline_build", "report"])
    graph.add_conditional_edges("baseline_build", after_baseline, ["extract_dependencies", "report"])
    graph.add_conditional_edges("extract_dependencies", after_extraction, ["next_dependency", "report"])
    graph.add_conditional_edges("next_dependency", after_next_dependency, ["find_versions", "build", "report"])
    graph.add_conditional_edges("find_versions", after_find_versions, ["apply_version", "next_dependency"])
    graph.add_edge("apply_version", "next_dependency")
    graph.add_conditional_edges("build", after_build, ["report", "classify_failure"])
    graph.add_conditional_edges("classify_failure", after_classify, ["repair_agent", "defer_to_user"])
    graph.add_edge("repair_agent", "build")
    graph.add_edge("defer_to_user", "report")
    graph.add_edge("report", END)

    return graph.compile()


if __name__ == "__main__":
    app = build_graph()
    app.get_graph().print_ascii()

    print("\nStub run:")
    for update in app.stream({"project_dir": ".", "max_retries": 3}, stream_mode="updates"):
        for node, changes in update.items():
            print(f"  {node:22} {changes or ''}")
