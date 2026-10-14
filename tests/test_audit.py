"""Audit rows are appended in order and cannot be rewritten."""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from tracker import db, models
from tracker.db import commit_transition, get_case_by_test, list_audits, set_classification
from tracker.ingest import ingest_run
from tracker.models import AuditRow
from tests.test_ingest import _failure


def test_two_successful_moves_append_two_rows_in_order(db_session):
    ingest_run(db_session, _failure())
    case = get_case_by_test(db_session, "TC_LINK_013")
    assert case is not None
    set_classification(db_session, case, severity="HIGH", priority="P2")

    first_at = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    second_at = datetime(2026, 4, 1, 12, 5, tzinfo=timezone.utc)
    first = commit_transition(
        db_session,
        case,
        "triaged",
        reason="ranked P2",
        actor="qa.engineer",
        now=first_at,
    )
    second = commit_transition(
        db_session,
        case,
        "verify",
        reason="ready for a rerun",
        actor="qa.engineer",
        now=second_at,
    )
    db_session.commit()

    assert first.accepted and second.accepted
    rows = list_audits(db_session, case.id)
    assert len(rows) == 2
    assert rows[0].id < rows[1].id
    assert rows[0].from_state == "new"
    assert rows[0].to_state == "triaged"
    assert rows[0].reason == "ranked P2"
    assert rows[0].actor == "qa.engineer"
    assert rows[0].timestamp == first_at
    assert rows[1].from_state == "triaged"
    assert rows[1].to_state == "verify"
    assert rows[1].reason == "ready for a rerun"
    assert rows[1].timestamp == second_at
    assert case.state == "verify"


def test_public_api_has_no_method_to_update_or_delete_an_audit_row():
    for module in (db, models):
        exported = [name for name in dir(module) if not name.startswith("_")]
        assert "update_audit" not in exported
        assert "delete_audit" not in exported


def test_stored_audit_row_rejects_update_and_delete(db_session):
    ingest_run(db_session, _failure())
    case = get_case_by_test(db_session, "TC_LINK_013")
    assert case is not None
    set_classification(db_session, case, severity="HIGH", priority="P1")
    commit_transition(db_session, case, "triaged", reason="blocks tomorrow", actor="qa.engineer")
    db_session.commit()

    row = list_audits(db_session, case.id)[0]
    assert row.from_state == "new"
    assert row.reason == "blocks tomorrow"
    original_reason = row.reason

    row.reason = "rewritten after the fact"
    with pytest.raises(IntegrityError, match="insert-only"):
        db_session.commit()
    db_session.rollback()

    stored = db_session.get(AuditRow, row.id)
    assert stored is not None
    assert stored.reason == original_reason
    assert stored.from_state == "new"

    db_session.delete(stored)
    with pytest.raises(IntegrityError, match="insert-only"):
        db_session.commit()
    db_session.rollback()

    still_there = db_session.get(AuditRow, row.id)
    assert still_there is not None
    assert still_there.reason == "blocks tomorrow"
