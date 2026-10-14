"""Load the fictional sample runs and the one historical close.

Severity on the open 1.1.0 failures is applied here, after ingest, because
ingest does not classify a case. TC_SERVICE_009 is the exception: it was
triaged as CRITICAL / P3, closed on 1.0.5, and the 1.1.0 failure reopens it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from tracker.db import commit_transition, get_case_by_test, set_classification
from tracker.domain import DEFAULT_ACTOR
from tracker.ingest import ingest_run, load_sample_runs
from tracker.models import CaseRow

_ORIGIN = datetime(2026, 3, 18, 9, 0, tzinfo=timezone.utc)

# Impact recorded for failures that are still waiting on triage.
# Priority stays unset until a person sets it.
_OPEN_SEVERITY = {
    "TC_VLAN_042": "HIGH",
    "TC_LINK_013": "HIGH",
    "TC_COUNTER_021": "LOW",
}


def seed_demo(session: Session) -> None:
    runs = {run.run_id: run for run in load_sample_runs()}

    ingest_run(session, runs["vlan-1.0.0"], now=datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc))

    ingest_run(session, runs["service-1.0.4"], now=datetime(2026, 2, 2, 9, 0, tzinfo=timezone.utc))
    service = get_case_by_test(session, "TC_SERVICE_009")
    if service is None:
        raise RuntimeError("TC_SERVICE_009 was not opened by the 1.0.4 failure.")
    set_classification(session, service, severity="CRITICAL", priority="P3")
    _move(session, service, "triaged", "switchd failed; severity critical, priority P3", "2026-02-02T11:00:00+00:00")
    _move(session, service, "verify", "candidate fix is in 1.0.5", "2026-02-02T11:30:00+00:00")

    ingest_run(session, runs["service-1.0.5"], now=datetime(2026, 2, 20, 9, 0, tzinfo=timezone.utc))
    _move(session, service, "closed", "rerun passed on 1.0.5", "2026-02-20T15:00:00+00:00")

    ingest_run(session, runs["vlan-1.1.0"], now=_ORIGIN)
    ingest_run(session, runs["link-1.1.0"], now=_ORIGIN + timedelta(minutes=1))
    ingest_run(session, runs["service-1.1.0"], now=_ORIGIN + timedelta(minutes=2))
    ingest_run(session, runs["counter-1.1.0"], now=_ORIGIN + timedelta(minutes=3))
    ingest_run(session, runs["boot-1.1.0"], now=_ORIGIN + timedelta(minutes=4))

    for test_case, severity in _OPEN_SEVERITY.items():
        case = get_case_by_test(session, test_case)
        if case is None:
            raise RuntimeError(f"{test_case} was not opened by its 1.1.0 failure.")
        set_classification(session, case, severity=severity, priority=None)

    session.flush()


def _move(session: Session, case: CaseRow, to_state: str, reason: str, moment: str) -> None:
    outcome = commit_transition(
        session,
        case,
        to_state,
        reason=reason,
        actor=DEFAULT_ACTOR,
        now=datetime.fromisoformat(moment),
    )
    if not outcome.accepted:
        raise RuntimeError(outcome.audit.message)
