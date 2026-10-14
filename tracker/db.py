"""Engine, schema, and the write operations the web layer is allowed to call."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tracker.domain import (
    AuditRecord,
    CaseState,
    QueueEntry,
    TransitionOutcome,
    Workflow,
    apply_transition,
    initial_state,
    load_workflow,
    repo_root,
    sort_queue,
)
from tracker.models import AuditRow, Base, CaseRow, RunRow

_AUDIT_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS audit_log_no_update
    BEFORE UPDATE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit log is insert-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
    BEFORE DELETE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit log is insert-only');
    END
    """,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_database_path() -> Path:
    return repo_root() / "var" / "triage.db"


def make_engine(database: str | Path) -> Engine:
    if str(database) == ":memory:":
        url = "sqlite:///:memory:"
    else:
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path.resolve()}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 5},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for statement in _AUDIT_TRIGGERS:
            connection.exec_driver_sql(statement)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def case_snapshot(case: CaseRow) -> CaseState:
    return CaseState(
        state=case.state,
        severity=case.severity,
        priority=case.priority,
        rerun_recorded=case.rerun_recorded,
    )


def create_case(
    session: Session,
    *,
    test_case: str,
    title: str,
    detail: str,
    component: str,
    build: str,
    opened_at: datetime | None = None,
    severity: str | None = None,
    priority: str | None = None,
    workflow: Workflow | None = None,
) -> CaseRow:
    rules = workflow or load_workflow()
    moment = opened_at or utcnow()
    row = CaseRow(
        test_case=test_case,
        title=title,
        detail=detail,
        component=component,
        state=initial_state(rules),
        severity=severity,
        priority=priority,
        rerun_recorded=False,
        latest_build=build,
        created_at=moment,
        opened_at=moment,
    )
    session.add(row)
    session.flush()
    return row


def set_classification(
    session: Session,
    case: CaseRow,
    *,
    severity: str | None,
    priority: str | None,
    workflow: Workflow | None = None,
) -> None:
    """Record impact and urgency. This is not a state change and writes no audit row."""
    rules = workflow or load_workflow()
    if severity is not None and severity not in rules.severities:
        raise ValueError(f"Unknown severity '{severity}'.")
    if priority is not None and priority not in rules.priorities:
        raise ValueError(f"Unknown priority '{priority}'.")
    case.severity = severity
    case.priority = priority
    session.flush()


def commit_transition(
    session: Session,
    case: CaseRow,
    to_state: str,
    *,
    reason: str,
    actor: str,
    workflow: Workflow | None = None,
    now: datetime | None = None,
) -> TransitionOutcome:
    """Apply one attempted move and insert exactly one audit row.

    A rejected move leaves the case state as it was. The audit row is still inserted.
    """
    rules = workflow or load_workflow()
    previous = case.state
    outcome = apply_transition(
        case_snapshot(case),
        to_state,
        reason=reason,
        actor=actor,
        workflow=rules,
        now=now,
    )
    _append_audit(session, case.id, outcome.audit)
    if outcome.accepted:
        case.state = outcome.case.state
        case.rerun_recorded = outcome.case.rerun_recorded
        if previous == rules.close_state:
            case.opened_at = outcome.audit.timestamp
    session.flush()
    return outcome


def list_audits(session: Session, case_id: int) -> list[AuditRow]:
    statement = select(AuditRow).where(AuditRow.case_id == case_id).order_by(AuditRow.id)
    return list(session.scalars(statement).all())


def get_case(session: Session, case_id: int) -> CaseRow | None:
    return session.get(CaseRow, case_id)


def get_case_by_test(session: Session, test_case: str) -> CaseRow | None:
    statement = select(CaseRow).where(CaseRow.test_case == test_case)
    return session.scalar(statement)


def list_runs_for_test(session: Session, test_case: str) -> list[RunRow]:
    statement = (
        select(RunRow)
        .where(RunRow.test_case == test_case)
        .order_by(RunRow.ingested_at, RunRow.id)
    )
    return list(session.scalars(statement).all())


def list_queue(
    session: Session,
    *,
    state: str | None = None,
    severity: str | None = None,
    component: str | None = None,
    workflow: Workflow | None = None,
) -> list[CaseRow]:
    """Open cases only, ordered by the domain sort (priority, severity, age)."""
    rules = workflow or load_workflow()
    statement = select(CaseRow).where(CaseRow.state != rules.close_state)
    if state:
        statement = statement.where(CaseRow.state == state)
    if severity:
        statement = statement.where(CaseRow.severity == severity)
    if component:
        statement = statement.where(CaseRow.component == component)
    rows = list(session.scalars(statement).all())
    entries = [
        QueueEntry(
            key=str(row.id),
            severity=row.severity,
            priority=row.priority,
            opened_at=row.opened_at,
        )
        for row in rows
    ]
    by_id = {str(row.id): row for row in rows}
    return [by_id[entry.key] for entry in sort_queue(entries, rules)]


def add_run(session: Session, row: RunRow) -> RunRow:
    session.add(row)
    session.flush()
    return row


def _append_audit(session: Session, case_id: int, audit: AuditRecord) -> AuditRow:
    row = AuditRow(
        case_id=case_id,
        from_state=audit.from_state,
        to_state=audit.to_state,
        actor=audit.actor,
        reason=audit.reason,
        accepted=audit.accepted,
        message=audit.message,
        timestamp=audit.timestamp,
    )
    session.add(row)
    session.flush()
    return row
