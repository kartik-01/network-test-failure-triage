"""Turn a simulated run into case and history changes.

The CLI and the UI both call ingest_run. A repeat failure never opens a
second case, and a pass never closes one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.db import add_run, commit_transition, create_case, get_case_by_test, utcnow
from tracker.domain import Workflow, load_workflow, reopen_target, repo_root, states_ready_for_close
from tracker.models import CaseRow, RunRow

INGEST_ACTOR = "ingest"


class IngestError(ValueError):
    """The run could not be stored."""


class IngestEffect(StrEnum):
    opened = "opened"
    attached = "attached"
    reopened = "reopened"
    ready_to_close = "ready_to_close"
    recorded = "recorded"


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    build: str
    test_case: str
    result: str = Field(pattern="^(pass|fail)$")
    component: str
    title: str
    detail: str
    evidence: str


class IngestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    effect: IngestEffect
    case_id: int | None
    run_id: str


def sample_runs_dir() -> Path:
    return repo_root() / "sample_runs"


def load_sample_runs(directory: Path | None = None) -> list[RunInput]:
    folder = directory or sample_runs_dir()
    paths = sorted(folder.glob("*.json"))
    return [RunInput.model_validate_json(path.read_text(encoding="utf-8")) for path in paths]


def ingest_run(
    session: Session,
    run: RunInput,
    *,
    actor: str = INGEST_ACTOR,
    workflow: Workflow | None = None,
    now: datetime | None = None,
) -> IngestResult:
    """Store one run and apply the open, attach, reopen, or ready rule."""
    rules = workflow or load_workflow()
    moment = now or utcnow()
    existing = session.scalar(select(RunRow.id).where(RunRow.run_id == run.run_id))
    if existing is not None:
        raise IngestError(f"Run '{run.run_id}' was already ingested.")

    stored = RunRow(
        run_id=run.run_id,
        build=run.build,
        test_case=run.test_case,
        result=run.result,
        component=run.component,
        title=run.title,
        detail=run.detail,
        evidence=run.evidence,
        effect=IngestEffect.recorded.value,
        case_id=None,
        ingested_at=moment,
    )
    add_run(session, stored)

    case = get_case_by_test(session, run.test_case)
    if run.result == "fail":
        effect = _ingest_failure(session, stored, case, rules, actor=actor, now=moment)
    else:
        effect = _ingest_pass(stored, case, rules)

    stored.effect = effect.value
    session.flush()
    return IngestResult(effect=effect, case_id=stored.case_id, run_id=run.run_id)


def _ingest_failure(
    session: Session,
    stored: RunRow,
    case: CaseRow | None,
    rules: Workflow,
    *,
    actor: str,
    now: datetime,
) -> IngestEffect:
    if case is not None and rules.is_open(case.state):
        _apply_failure_evidence(case, stored)
        if case.state in states_ready_for_close(rules):
            case.rerun_recorded = False
        stored.case_id = case.id
        return IngestEffect.attached

    if case is not None and not rules.is_open(case.state):
        outcome = commit_transition(
            session,
            case,
            reopen_target(rules),
            reason=f"failed again on {stored.build}",
            actor=actor,
            workflow=rules,
            now=now,
        )
        if not outcome.accepted:
            raise IngestError(outcome.audit.message)
        _apply_failure_evidence(case, stored)
        stored.case_id = case.id
        return IngestEffect.reopened

    created = create_case(
        session,
        test_case=stored.test_case,
        title=stored.title,
        detail=stored.detail,
        component=stored.component,
        build=stored.build,
        opened_at=now,
        workflow=rules,
    )
    stored.case_id = created.id
    return IngestEffect.opened


def _ingest_pass(stored: RunRow, case: CaseRow | None, rules: Workflow) -> IngestEffect:
    if case is None:
        return IngestEffect.recorded
    stored.case_id = case.id
    if case.state in states_ready_for_close(rules):
        case.rerun_recorded = True
        return IngestEffect.ready_to_close
    return IngestEffect.recorded


def _apply_failure_evidence(case: CaseRow, stored: RunRow) -> None:
    case.title = stored.title
    case.detail = stored.detail
    case.latest_build = stored.build
