from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


Outcome = Literal["resolved", "not_resolved"]
DiagnosticStatus = Literal["in_progress", "resolved", "unresolved"]


class DiagnosticDecisionState(TypedDict, total=False):
    current_position: int
    step_positions: list[int]
    outcome: Outcome
    status: DiagnosticStatus
    resolved: bool | None
    next_position: int | None


def _validate_and_route(state: DiagnosticDecisionState) -> Literal["resolved", "advance", "unresolved"]:
    current_position = state["current_position"]
    step_positions = sorted(set(state["step_positions"]))
    if current_position not in step_positions:
        raise ValueError("current_position must exist in step_positions")
    if state["outcome"] == "resolved":
        return "resolved"
    if state["outcome"] != "not_resolved":
        raise ValueError("outcome must be resolved or not_resolved")
    return "advance" if any(position > current_position for position in step_positions) else "unresolved"


def _mark_resolved(_: DiagnosticDecisionState) -> DiagnosticDecisionState:
    return {"status": "resolved", "resolved": True, "next_position": None}


def _advance(state: DiagnosticDecisionState) -> DiagnosticDecisionState:
    next_position = min(position for position in state["step_positions"] if position > state["current_position"])
    return {"status": "in_progress", "resolved": None, "next_position": next_position}


def _mark_unresolved(_: DiagnosticDecisionState) -> DiagnosticDecisionState:
    return {"status": "unresolved", "resolved": False, "next_position": None}


def build_feedback_decision_graph():
    graph = StateGraph(DiagnosticDecisionState)
    graph.add_conditional_edges(
        START,
        _validate_and_route,
        {
            "resolved": "mark_resolved",
            "advance": "advance",
            "unresolved": "mark_unresolved",
        },
    )
    graph.add_node("mark_resolved", _mark_resolved)
    graph.add_node("advance", _advance)
    graph.add_node("mark_unresolved", _mark_unresolved)
    graph.add_edge("mark_resolved", END)
    graph.add_edge("advance", END)
    graph.add_edge("mark_unresolved", END)
    return graph.compile()


feedback_decision_graph = build_feedback_decision_graph()
