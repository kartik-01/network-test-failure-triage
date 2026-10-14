"""Ingest rules shared by the CLI and the UI."""

from sqlalchemy import func, select

from tracker.db import commit_transition, get_case_by_test, list_audits, list_runs_for_test, set_classification
from tracker.ingest import IngestEffect, RunInput, ingest_run, load_sample_runs
from tracker.models import CaseRow


def _failure(
    test_case: str = "TC_LINK_013",
    build: str = "1.1.0",
    run_id: str | None = None,
    **overrides: str,
) -> RunInput:
    payload = {
        "run_id": run_id or f"{test_case}-{build}-fail",
        "build": build,
        "test_case": test_case,
        "result": "fail",
        "component": "link",
        "title": "Link operational state down",
        "detail": "eth3 oper state DOWN, previous build was UP",
        "evidence": "eth3 oper state DOWN",
    }
    payload.update(overrides)
    return RunInput.model_validate(payload)


def _pass(test_case: str, build: str, run_id: str | None = None) -> RunInput:
    return RunInput(
        run_id=run_id or f"{test_case}-{build}-pass",
        build=build,
        test_case=test_case,
        result="pass",
        component="service",
        title="Switch daemon failed",
        detail="switchd state RUNNING",
        evidence="switchd status RUNNING",
    )


def _classify_and_move_to_verify(session, case: CaseRow) -> None:
    set_classification(session, case, severity="CRITICAL", priority="P3")
    triaged = commit_transition(session, case, "triaged", reason="classified", actor="qa.engineer")
    assert triaged.accepted
    verified = commit_transition(session, case, "verify", reason="fix is ready to rerun", actor="qa.engineer")
    assert verified.accepted


def test_sample_runs_cover_the_seeded_history():
    runs = {(run.test_case, run.build, run.result): run for run in load_sample_runs()}

    vlan_fail = runs[("TC_VLAN_042", "1.1.0", "fail")]
    assert vlan_fail.detail == "eth1 operational VLAN is 1, expected 20"
    assert vlan_fail.component == "vlan"

    link_fail = runs[("TC_LINK_013", "1.1.0", "fail")]
    assert link_fail.detail == "eth3 oper state DOWN, previous build was UP"
    assert link_fail.component == "link"

    service_fail = runs[("TC_SERVICE_009", "1.1.0", "fail")]
    assert service_fail.detail == "switchd state FAILED"
    assert service_fail.component == "service"

    counter_fail = runs[("TC_COUNTER_021", "1.1.0", "fail")]
    assert counter_fail.detail == "crc_errors on eth2 is 4, threshold 0"
    assert counter_fail.component == "counters"

    assert ("TC_VLAN_042", "1.0.0", "pass") in runs
    assert ("TC_SERVICE_009", "1.0.4", "fail") in runs
    assert ("TC_SERVICE_009", "1.0.5", "pass") in runs
    assert runs[("TC_BOOT_001", "1.1.0", "pass")].result == "pass"


def test_first_failure_creates_one_case(db_session):
    sample = next(
        run for run in load_sample_runs() if run.test_case == "TC_VLAN_042" and run.result == "fail"
    )
    result = ingest_run(db_session, sample)
    db_session.commit()

    case = get_case_by_test(db_session, "TC_VLAN_042")
    assert result.effect is IngestEffect.opened
    assert case is not None
    assert case.state == "new"
    assert case.detail == "eth1 operational VLAN is 1, expected 20"
    assert case.latest_build == "1.1.0"
    assert len(list_runs_for_test(db_session, "TC_VLAN_042")) == 1
    assert list_audits(db_session, case.id) == []
    assert result.case_id == case.id


def test_second_failure_of_an_open_test_does_not_open_another_case(db_session):
    ingest_run(db_session, _failure(build="1.1.0", run_id="link-a"))
    second = ingest_run(
        db_session,
        _failure(
            build="1.1.1",
            run_id="link-b",
            detail="eth3 oper state DOWN, still down on the next build",
        ),
    )
    db_session.commit()

    cases = list(db_session.scalars(select(CaseRow)).all())
    assert len(cases) == 1
    assert second.effect is IngestEffect.attached
    assert second.case_id == cases[0].id
    assert cases[0].state == "new"
    assert cases[0].latest_build == "1.1.1"
    assert len(list_runs_for_test(db_session, "TC_LINK_013")) == 2
    assert list_audits(db_session, cases[0].id) == []


def test_failure_after_close_reopens_the_same_case(db_session):
    opened = ingest_run(db_session, _failure(test_case="TC_SERVICE_009", build="1.0.4", run_id="svc-104"))
    case = get_case_by_test(db_session, "TC_SERVICE_009")
    assert case is not None
    assert opened.effect is IngestEffect.opened
    _classify_and_move_to_verify(db_session, case)

    ready = ingest_run(db_session, _pass("TC_SERVICE_009", "1.0.5", run_id="svc-105"))
    assert ready.effect is IngestEffect.ready_to_close
    assert case.state == "verify"

    closed = commit_transition(db_session, case, "closed", reason="rerun passed on 1.0.5", actor="qa.engineer")
    assert closed.accepted
    assert case.state == "closed"

    again = ingest_run(
        db_session,
        _failure(
            test_case="TC_SERVICE_009",
            build="1.1.0",
            run_id="svc-110",
            detail="switchd state FAILED",
            component="service",
        ),
    )
    db_session.commit()

    assert again.effect is IngestEffect.reopened
    assert again.case_id == case.id
    assert case.state == "reopened"
    assert case.detail == "switchd state FAILED"
    reopened = [row for row in list_audits(db_session, case.id) if row.to_state == "reopened"]
    assert len(reopened) == 1
    assert reopened[0].from_state == "closed"
    assert reopened[0].accepted is True
    assert reopened[0].reason == "failed again on 1.1.0"
    assert reopened[0].actor == "ingest"
    assert db_session.scalar(select(func.count()).select_from(CaseRow)) == 1


def test_pass_in_verify_does_not_auto_close(db_session):
    ingest_run(db_session, _failure(test_case="TC_SERVICE_009", build="1.0.4", run_id="svc-104"))
    case = get_case_by_test(db_session, "TC_SERVICE_009")
    assert case is not None
    _classify_and_move_to_verify(db_session, case)

    result = ingest_run(db_session, _pass("TC_SERVICE_009", "1.0.5", run_id="svc-105"))
    db_session.commit()

    assert result.effect is IngestEffect.ready_to_close
    assert case.state == "verify"
    assert case.rerun_recorded is True
    assert not any(row.to_state == "closed" and row.accepted for row in list_audits(db_session, case.id))


def test_pass_with_no_case_stores_history_only(db_session):
    boot = next(run for run in load_sample_runs() if run.test_case == "TC_BOOT_001")
    result = ingest_run(db_session, boot)
    db_session.commit()

    assert result.effect is IngestEffect.recorded
    assert result.case_id is None
    assert get_case_by_test(db_session, "TC_BOOT_001") is None
    assert len(list_runs_for_test(db_session, "TC_BOOT_001")) == 1
