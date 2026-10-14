"""Triage rules: which moves are legal, and how the queue is ordered.

The web layer and the ingest path both call this module. They do not
reimplement transitions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_ACTOR = "qa.engineer"


def repo_root() -> Path:
    """Directory that contains rules/ and sample_runs/."""
    here = Path(__file__).resolve().parent
    candidate = here.parent
    if (candidate / "rules" / "workflow.yaml").is_file():
        return candidate
    return here


def default_workflow_path() -> Path:
    return repo_root() / "rules" / "workflow.yaml"


class TransitionSpec(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")


class Workflow(BaseModel):
    model_config = ConfigDict(frozen=True)

    states: list[str]
    severities: list[str]
    priorities: list[str]
    transitions: list[TransitionSpec]
    classification_required_for: list[str]
    close_state: str
    close_requires_rerun: bool = True

    @model_validator(mode="after")
    def _edges_match_states(self) -> Workflow:
        if len(set(self.states)) != len(self.states):
            raise ValueError("workflow states must be unique")
        if len(set(self.severities)) != len(self.severities):
            raise ValueError("workflow severities must be unique")
        if len(set(self.priorities)) != len(self.priorities):
            raise ValueError("workflow priorities must be unique")
        known = set(self.states)
        if self.close_state not in known:
            raise ValueError("close_state must be one of the workflow states")
        unknown_gates = [name for name in self.classification_required_for if name not in known]
        if unknown_gates:
            raise ValueError(f"classification_required_for has unknown states: {unknown_gates}")
        seen: set[tuple[str, str]] = set()
        for edge in self.transitions:
            if edge.source not in known or edge.target not in known:
                raise ValueError(f"transition {edge.source} -> {edge.target} uses an unknown state")
            pair = (edge.source, edge.target)
            if pair in seen:
                raise ValueError(f"duplicate transition {edge.source} -> {edge.target}")
            seen.add(pair)
        return self

    def allows(self, source: str, target: str) -> bool:
        return any(edge.source == source and edge.target == target for edge in self.transitions)

    def is_open(self, state: str) -> bool:
        return state != self.close_state

    def priority_rank(self, priority: str | None) -> int:
        """Lower ranks sort first. An unset priority sorts after every set priority."""
        if priority is None:
            return len(self.priorities)
        try:
            return self.priorities.index(priority)
        except ValueError:
            return len(self.priorities)

    def severity_rank(self, severity: str | None) -> int:
        """Lower ranks sort first. CRITICAL is first when it is first in the YAML."""
        if severity is None:
            return len(self.severities)
        try:
            return self.severities.index(severity)
        except ValueError:
            return len(self.severities)


class CaseState(BaseModel):
    """The fields the transition function is allowed to see."""

    model_config = ConfigDict(frozen=True)

    state: str
    severity: str | None = None
    priority: str | None = None
    rerun_recorded: bool = False

    @field_validator("severity", "priority", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    def is_classified(self, workflow: Workflow) -> bool:
        return self.severity in workflow.severities and self.priority in workflow.priorities


class AuditRecord(BaseModel):
    """One insert-only audit row produced by an attempted move."""

    model_config = ConfigDict(frozen=True)

    from_state: str
    to_state: str
    actor: str
    reason: str
    timestamp: datetime
    accepted: bool
    message: str = ""


class TransitionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted: bool
    case: CaseState
    audit: AuditRecord


class QueueEntry(BaseModel):
    """Sortable view of an open case. Identity is only a tiebreaker."""

    model_config = ConfigDict(frozen=True)

    key: str
    severity: str | None = None
    priority: str | None = None
    opened_at: datetime


def load_workflow(path: Path | None = None) -> Workflow:
    rules_path = path or default_workflow_path()
    loaded = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{rules_path} must contain a mapping")
    return Workflow.model_validate(loaded)


def move_block_reason(
    case: CaseState,
    to_state: str,
    workflow: Workflow,
    *,
    reason: str,
    actor: str,
) -> str | None:
    """Return why a move is illegal, or None when it is allowed."""
    cleaned_reason = reason.strip()
    cleaned_actor = actor.strip()
    if not cleaned_reason:
        return "A reason is required."
    if not cleaned_actor:
        return "An actor is required."
    if case.state not in workflow.states:
        return f"Unknown state '{case.state}'."
    if to_state not in workflow.states:
        return f"Unknown state '{to_state}'."
    if not workflow.allows(case.state, to_state):
        return f"{case.state} -> {to_state} is not a legal move."
    if to_state in workflow.classification_required_for and not case.is_classified(workflow):
        return f"Severity and priority must both be set before moving to {to_state}."
    if workflow.close_requires_rerun and to_state == workflow.close_state and not case.rerun_recorded:
        return "Closing requires a recorded rerun of the same test."
    return None


def apply_transition(
    case: CaseState,
    to_state: str,
    *,
    reason: str,
    actor: str,
    workflow: Workflow | None = None,
    now: datetime | None = None,
) -> TransitionOutcome:
    """Attempt one move.

    On rejection the returned case is the original case. Either way the
    outcome carries exactly one audit record for the attempt.
    """
    rules = workflow or load_workflow()
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    blocked = move_block_reason(case, to_state, rules, reason=reason, actor=actor)
    cleaned_reason = reason.strip()
    cleaned_actor = actor.strip()
    if blocked is not None:
        return TransitionOutcome(
            accepted=False,
            case=case,
            audit=AuditRecord(
                from_state=case.state,
                to_state=to_state,
                actor=cleaned_actor or actor,
                reason=cleaned_reason or reason,
                timestamp=timestamp,
                accepted=False,
                message=blocked,
            ),
        )

    # A recorded rerun applies only to the state the case is leaving.
    # Ingest sets the flag again when a later pass arrives in verify.
    updated = case.model_copy(update={"state": to_state, "rerun_recorded": False})

    return TransitionOutcome(
        accepted=True,
        case=updated,
        audit=AuditRecord(
            from_state=case.state,
            to_state=to_state,
            actor=cleaned_actor,
            reason=cleaned_reason,
            timestamp=timestamp,
            accepted=True,
            message="",
        ),
    )


def legal_targets(case: CaseState, workflow: Workflow | None = None) -> list[str]:
    """States the UI may offer as buttons.

    Structural rules are applied with a stand-in reason. The person's reason
    is still required when the form is submitted. This function does not write
    an audit row.
    """
    rules = workflow or load_workflow()
    offered: list[str] = []
    for edge in rules.transitions:
        if edge.source != case.state:
            continue
        blocked = move_block_reason(
            case,
            edge.target,
            rules,
            reason="offered",
            actor=DEFAULT_ACTOR,
        )
        if blocked is None:
            offered.append(edge.target)
    return offered


def states_ready_for_close(workflow: Workflow) -> set[str]:
    """States in which a passing rerun marks the case ready for a human to close."""
    if not workflow.close_requires_rerun:
        return set()
    return {edge.source for edge in workflow.transitions if edge.target == workflow.close_state}


def initial_state(workflow: Workflow) -> str:
    """The state a brand-new failure starts in. It has no inbound transition."""
    targets = {edge.target for edge in workflow.transitions}
    starts = [state for state in workflow.states if state not in targets]
    if len(starts) != 1:
        raise ValueError("workflow must have exactly one state with no inbound transition")
    return starts[0]


def reopen_target(workflow: Workflow) -> str:
    """Where a closed case goes when the same test fails again."""
    targets = [edge.target for edge in workflow.transitions if edge.source == workflow.close_state]
    if len(targets) != 1:
        raise ValueError("close_state must have exactly one outbound transition")
    return targets[0]


def sort_queue(entries: Sequence[QueueEntry], workflow: Workflow | None = None) -> list[QueueEntry]:
    """Order open cases by priority, then severity, then oldest opened_at.

    Priority is urgency and wins over severity. A LOW/P1 case sorts ahead of
    a CRITICAL/P3 case. Cases with no priority sort after every prioritized case.
    """
    rules = workflow or load_workflow()

    def sort_key(entry: QueueEntry) -> tuple[int, int, datetime, str]:
        return (
            rules.priority_rank(entry.priority),
            rules.severity_rank(entry.severity),
            entry.opened_at,
            entry.key,
        )

    return sorted(entries, key=sort_key)

