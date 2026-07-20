import pytest

from app.diagnostic_graph import feedback_decision_graph


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"current_position": 1, "step_positions": [1, 2, 3], "outcome": "resolved"},
            {"status": "resolved", "resolved": True, "next_position": None},
        ),
        (
            {"current_position": 1, "step_positions": [3, 1, 2], "outcome": "not_resolved"},
            {"status": "in_progress", "resolved": None, "next_position": 2},
        ),
        (
            {"current_position": 3, "step_positions": [1, 2, 3], "outcome": "not_resolved"},
            {"status": "unresolved", "resolved": False, "next_position": None},
        ),
    ],
)
def test_feedback_decision_graph_branches(payload, expected):
    result = feedback_decision_graph.invoke(payload)
    assert {key: result[key] for key in expected} == expected


def test_feedback_decision_graph_rejects_unknown_current_position():
    with pytest.raises(ValueError, match="current_position"):
        feedback_decision_graph.invoke(
            {"current_position": 9, "step_positions": [1, 2, 3], "outcome": "not_resolved"}
        )
