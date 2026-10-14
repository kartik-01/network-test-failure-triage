"""Priority is urgency. It sorts ahead of severity."""

from datetime import datetime, timezone

from tracker.db import create_case, get_case_by_test, list_audits, list_queue, list_runs_for_test
from tracker.models import CaseRow
from tracker.seed import seed_demo


def _open(
    session,
    test_case: str,
    *,
    severity: str | None,
    priority: str | None,
    opened_at: datetime,
    component: str = "link",
) -> CaseRow:
    return create_case(
        session,
        test_case=test_case,
        title=test_case,
        detail="seeded for sort",
        component=component,
        build="9.9.9",
        opened_at=opened_at,
        severity=severity,
        priority=priority,
    )


def test_low_p1_sorts_ahead_of_critical_p3(db_session):
    older = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    _open(
        db_session,
        "TC_CRIT",
        severity="CRITICAL",
        priority="P3",
        opened_at=older,
    )
    _open(
        db_session,
        "TC_LOW",
        severity="LOW",
        priority="P1",
        opened_at=newer,
    )

    ordered = [case.test_case for case in list_queue(db_session)]
    assert ordered == ["TC_LOW", "TC_CRIT"]


def test_unset_priority_sorts_after_a_lower_severity_case_that_has_one(db_session):
    _open(
        db_session,
        "TC_UNSET",
        severity="CRITICAL",
        priority=None,
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _open(
        db_session,
        "TC_P4",
        severity="LOW",
        priority="P4",
        opened_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    ordered = [case.test_case for case in list_queue(db_session)]
    assert ordered == ["TC_P4", "TC_UNSET"]


def test_seed_demo_queue_shows_reopened_service_ahead_of_untriaged_failures(db_session):
    seed_demo(db_session)
    db_session.commit()

    ordered = list_queue(db_session)
    assert [case.test_case for case in ordered] == [
        "TC_SERVICE_009",
        "TC_VLAN_042",
        "TC_LINK_013",
        "TC_COUNTER_021",
    ]
    assert get_case_by_test(db_session, "TC_BOOT_001") is None

    vlan = ordered[1]
    assert vlan.state == "new"
    assert vlan.severity == "HIGH"
    assert vlan.priority is None
    assert vlan.detail == "eth1 operational VLAN is 1, expected 20"
    assert vlan.latest_build == "1.1.0"

    link = ordered[2]
    assert link.severity == "HIGH"
    assert link.priority is None
    assert link.component == "link"
    assert link.detail == "eth3 oper state DOWN, previous build was UP"

    counter = ordered[3]
    assert counter.severity == "LOW"
    assert counter.priority is None
    assert counter.component == "counters"

    service = ordered[0]
    assert service.state == "reopened"
    assert service.severity == "CRITICAL"
    assert service.priority == "P3"
    assert service.detail == "switchd state FAILED"
    assert service.component == "service"

    service_runs = list_runs_for_test(db_session, "TC_SERVICE_009")
    assert [(run.build, run.result, run.effect) for run in service_runs] == [
        ("1.0.4", "fail", "opened"),
        ("1.0.5", "pass", "ready_to_close"),
        ("1.1.0", "fail", "reopened"),
    ]
    vlan_runs = list_runs_for_test(db_session, "TC_VLAN_042")
    assert [(run.build, run.result, run.effect) for run in vlan_runs] == [
        ("1.0.0", "pass", "recorded"),
        ("1.1.0", "fail", "opened"),
    ]

    accepted = [
        (row.from_state, row.to_state, row.reason)
        for row in list_audits(db_session, service.id)
        if row.accepted
    ]
    assert accepted == [
        ("new", "triaged", "switchd failed; severity critical, priority P3"),
        ("triaged", "verify", "candidate fix is in 1.0.5"),
        ("verify", "closed", "rerun passed on 1.0.5"),
        ("closed", "reopened", "failed again on 1.1.0"),
    ]
