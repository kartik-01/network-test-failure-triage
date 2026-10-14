"""Server-rendered triage UI.

Routes call the domain and database functions. They do not decide which
move is legal.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tracker.db import (
    case_snapshot,
    commit_transition,
    default_database_path,
    get_case,
    get_case_by_test,
    init_db,
    list_audits,
    list_queue,
    list_runs_for_test,
    make_engine,
    session_factory,
    set_classification,
)
from tracker.domain import legal_targets, load_workflow
from tracker.models import AuditRow, CaseRow, RunRow

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

EFFECT_LABELS = {
    "opened": "case opened",
    "reopened": "case reopened",
    "attached": "attached to open case",
    "ready_to_close": "passing rerun recorded",
    "recorded": "recorded",
}


def format_age(opened_at: datetime, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    seconds = max(0, int((moment - opened_at).total_seconds()))
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def create_app(engine: Engine | None = None) -> FastAPI:
    database = engine or make_engine(default_database_path())
    init_db(database)
    sessions = session_factory(database)
    app = FastAPI(title="Network test failure triage")
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    def get_session() -> Iterator[Session]:
        session = sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @app.get("/")
    def queue(
        request: Request,
        session: Session = Depends(get_session),
        state: str = "",
        severity: str = "",
        component: str = "",
    ) -> object:
        rules = load_workflow()
        cases = list_queue(
            session,
            state=state or None,
            severity=severity or None,
            component=component or None,
        )
        components = list(
            session.scalars(select(CaseRow.component).distinct().order_by(CaseRow.component)).all()
        )
        return TEMPLATES.TemplateResponse(
            request,
            "queue.html",
            {
                "cases": [_case_fields(case) for case in cases],
                "open_states": [name for name in rules.states if rules.is_open(name)],
                "severities": rules.severities,
                "components": components,
                "selected_state": state,
                "selected_severity": severity,
                "selected_component": component,
                "open_count": len(cases),
            },
        )

    @app.get("/cases/{case_id}")
    def case_page(
        case_id: int,
        request: Request,
        session: Session = Depends(get_session),
    ) -> object:
        case = _require_case(session, case_id)
        return _case_response(request, session, case)

    @app.post("/cases/{case_id}/classification")
    def save_classification(
        case_id: int,
        request: Request,
        session: Session = Depends(get_session),
        severity: str = Form(""),
        priority: str = Form(""),
    ) -> object:
        case = _require_case(session, case_id)
        try:
            set_classification(
                session,
                case,
                severity=_blank(severity),
                priority=_blank(priority),
            )
        except ValueError as exc:
            return _case_response(request, session, case, error=str(exc), status_code=400)
        return RedirectResponse(url=f"/cases/{case.id}", status_code=303)

    @app.post("/cases/{case_id}/transitions")
    def move_case(
        case_id: int,
        request: Request,
        session: Session = Depends(get_session),
        to_state: str = Form(...),
        reason: str = Form(""),
        actor: str = Form("qa.engineer"),
    ) -> object:
        case = _require_case(session, case_id)
        outcome = commit_transition(
            session,
            case,
            to_state,
            reason=reason,
            actor=actor,
        )
        if not outcome.accepted:
            return _case_response(
                request,
                session,
                case,
                error=outcome.audit.message,
                status_code=400,
            )
        return RedirectResponse(url=f"/cases/{case.id}", status_code=303)

    @app.get("/history/{test_case}")
    def history(
        test_case: str,
        request: Request,
        session: Session = Depends(get_session),
    ) -> object:
        runs = list_runs_for_test(session, test_case)
        if not runs:
            raise HTTPException(status_code=404, detail=f"No runs for {test_case}.")
        case = get_case_by_test(session, test_case)
        return TEMPLATES.TemplateResponse(
            request,
            "history.html",
            {
                "test_case": test_case,
                "case_id": case.id if case else None,
                "case_state": case.state if case else None,
                "runs": [_run_fields(run) for run in runs],
            },
        )

    return app


def _require_case(session: Session, case_id: int) -> CaseRow:
    case = get_case(session, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id} was not found.")
    return case


def _case_response(
    request: Request,
    session: Session,
    case: CaseRow,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> object:
    rules = load_workflow()
    offered = legal_targets(case_snapshot(case), rules)
    audits = list_audits(session, case.id)
    evidence = [run for run in list_runs_for_test(session, case.test_case) if run.case_id == case.id]
    return TEMPLATES.TemplateResponse(
        request,
        "case.html",
        {
            "case": _case_fields(case),
            "severities": rules.severities,
            "priorities": rules.priorities,
            "legal": offered,
            "audits": [_audit_fields(row) for row in audits],
            "evidence": [_run_fields(run) for run in evidence],
            "error": error,
        },
        status_code=status_code,
    )


def _case_fields(case: CaseRow) -> dict[str, object]:
    return {
        "id": case.id,
        "test_case": case.test_case,
        "title": case.title,
        "detail": case.detail,
        "component": case.component,
        "state": case.state,
        "severity": case.severity,
        "priority": case.priority,
        "rerun_recorded": case.rerun_recorded,
        "latest_build": case.latest_build,
        "opened_at": format_timestamp(case.opened_at),
        "age": format_age(case.opened_at),
    }


def _audit_fields(row: AuditRow) -> dict[str, object]:
    return {
        "id": row.id,
        "from_state": row.from_state,
        "to_state": row.to_state,
        "actor": row.actor,
        "reason": row.reason,
        "accepted": row.accepted,
        "message": row.message,
        "timestamp": format_timestamp(row.timestamp),
    }


def _run_fields(run: RunRow) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "build": run.build,
        "result": run.result,
        "detail": run.detail,
        "evidence": run.evidence,
        "effect": run.effect,
        "effect_label": EFFECT_LABELS.get(run.effect, run.effect),
        "ingested_at": format_timestamp(run.ingested_at),
    }


def _blank(value: str) -> str | None:
    stripped = value.strip()
    return stripped or None
