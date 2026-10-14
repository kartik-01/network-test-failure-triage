"""Table-driven coverage of the workflow YAML.

Allowed rows come from rules/workflow.yaml. Forbidden rows are every other
pair among those states, so a new edge has to be added deliberately.
"""

from datetime import datetime, timezone

import pytest

from tracker.domain import CaseState, apply_transition, legal_targets, load_workflow

WORKFLOW = load_workflow()
ALLOWED = [(edge.source, edge.target) for edge in WORKFLOW.transitions]
ALLOWED_SET = set(ALLOWED)
FORBIDDEN = [
    (source, target)
    for source in WORKFLOW.states
    for target in WORKFLOW.states
    if (source, target) not in ALLOWED_SET
]

DOCUMENTED_STATES = ["new", "triaged", "verify", "closed", "reopened"]
DOCUMENTED_MOVES = {
    ("new", "triaged"),
    ("triaged", "verify"),
    ("verify", "closed"),
    ("verify", "triaged"),
    ("closed", "reopened"),
    ("reopened", "triaged"),
}


def _ready(state: str) -> CaseState:
    """A case that already has severity, priority, and a recorded rerun."""
    return CaseState(state=state, severity="HIGH", priority="P2", rerun_recorded=True)


def test_shipped_workflow_matches_the_documented_default():
    assert WORKFLOW.states == DOCUMENTED_STATES
    assert set(ALLOWED) == DOCUMENTED_MOVES
    assert ("new", "closed") not in ALLOWED_SET
    assert WORKFLOW.close_state == "closed"
    assert "triaged" in WORKFLOW.classification_required_for


@pytest.mark.parametrize(
    ("source", "target"),
    ALLOWED,
    ids=[f"{source}_to_{target}" for source, target in ALLOWED],
)
def test_allowed_transition_changes_state_and_writes_one_audit_row(source: str, target: str):
    moment = datetime(2026, 3, 1, 15, 0, tzinfo=timezone.utc)
    outcome = apply_transition(
        _ready(source),
        target,
        reason="checked the failure",
        actor="qa.engineer",
        workflow=WORKFLOW,
        now=moment,
    )

    assert outcome.accepted is True
    assert outcome.case.state == target
    audits = [outcome.audit]
    assert len(audits) == 1
    row = audits[0]
    assert row.from_state == source
    assert row.to_state == target
    assert row.actor == "qa.engineer"
    assert row.reason == "checked the failure"
    assert row.timestamp == moment
    assert row.accepted is True


@pytest.mark.parametrize(
    ("source", "target"),
    FORBIDDEN,
    ids=[f"{source}_to_{target}" for source, target in FORBIDDEN],
)
def test_forbidden_transition_leaves_state_unchanged(source: str, target: str):
    case = _ready(source)
    outcome = apply_transition(
        case,
        target,
        reason="trying to skip",
        actor="qa.engineer",
        workflow=WORKFLOW,
    )

    assert outcome.accepted is False
    assert outcome.case.state == source
    assert outcome.case == case
    assert outcome.audit.accepted is False
    assert outcome.audit.from_state == source
    assert outcome.audit.to_state == target


def test_new_to_closed_is_forbidden():
    case = _ready("new")
    outcome = apply_transition(
        case,
        "closed",
        reason="skip the path",
        actor="qa.engineer",
        workflow=WORKFLOW,
    )

    assert outcome.accepted is False
    assert outcome.case.state == "new"
    assert outcome.audit.message == "new -> closed is not a legal move."
    assert "closed" not in legal_targets(case, WORKFLOW)


@pytest.mark.parametrize(
    ("severity", "priority"),
    [
        (None, None),
        ("HIGH", None),
        (None, "P1"),
        ("", "P1"),
        ("HIGH", ""),
        ("urgent", "P1"),
        ("HIGH", "P0"),
    ],
)
def test_triaged_forbidden_until_severity_and_priority_are_set(
    severity: str | None,
    priority: str | None,
):
    case = CaseState(state="new", severity=severity, priority=priority, rerun_recorded=True)
    outcome = apply_transition(
        case,
        "triaged",
        reason="classifying",
        actor="qa.engineer",
        workflow=WORKFLOW,
    )

    assert outcome.accepted is False
    assert outcome.case.state == "new"
    assert legal_targets(case, WORKFLOW) == []


def test_triaged_is_offered_once_severity_and_priority_are_set():
    case = CaseState(state="new", severity="LOW", priority="P1")
    outcome = apply_transition(
        case,
        "triaged",
        reason="release blocker",
        actor="qa.engineer",
        workflow=WORKFLOW,
    )

    assert outcome.accepted is True
    assert outcome.case.state == "triaged"
    assert legal_targets(case, WORKFLOW) == ["triaged"]


def test_close_requires_a_recorded_rerun():
    case = CaseState(state="verify", severity="CRITICAL", priority="P3", rerun_recorded=False)
    outcome = apply_transition(
        case,
        "closed",
        reason="looks fine",
        actor="qa.engineer",
        workflow=WORKFLOW,
    )

    assert outcome.accepted is False
    assert outcome.case.state == "verify"
    assert "closed" not in legal_targets(case, WORKFLOW)
    assert "triaged" in legal_targets(case, WORKFLOW)


def test_blank_reason_is_rejected_and_state_stays():
    case = _ready("triaged")
    outcome = apply_transition(
        case,
        "verify",
        reason="   ",
        actor="qa.engineer",
        workflow=WORKFLOW,
    )

    assert outcome.accepted is False
    assert outcome.case.state == "triaged"
    assert outcome.audit.accepted is False
