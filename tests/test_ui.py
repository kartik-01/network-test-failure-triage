"""Server-rendered pages over the same rules the domain tests pin down.

Browser tests are marked ``selenium``. ``pytest -m 'not selenium'`` runs the
rest without Chrome. The default run includes the browser.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tracker.db import (
    commit_transition,
    get_case_by_test,
    init_db,
    list_audits,
    make_engine,
    session_factory,
    set_classification,
)
from tracker.ingest import IngestEffect, ingest_run, load_sample_runs
from tracker.seed import seed_demo
from tracker.web import create_app, format_age

_MAC_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


@pytest.fixture
def engine(tmp_path):
    database = make_engine(tmp_path / "ui.db")
    init_db(database)
    factory = session_factory(database)
    session = factory()
    try:
        seed_demo(session)
        session.commit()
    finally:
        session.close()
    yield database
    database.dispose()


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    with TestClient(create_app(engine)) as test_client:
        yield test_client


@pytest.fixture
def session(engine) -> Iterator[Session]:
    factory = session_factory(engine)
    db = factory()
    try:
        yield db
    finally:
        db.close()


def test_queue_orders_seeded_cases_and_counts_from_the_database(client, session):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert 'data-testid="open-count">4 open' in body
    assert body.index("open-TC_SERVICE_009") < body.index("open-TC_VLAN_042")
    assert body.index("open-TC_VLAN_042") < body.index("open-TC_LINK_013")
    assert body.index("open-TC_LINK_013") < body.index("open-TC_COUNTER_021")
    assert "TC_BOOT_001" not in body
    assert 'data-testid="cell-priority-TC_VLAN_042">unset' in body
    assert 'data-testid="cell-severity-TC_SERVICE_009"' in body
    assert ">CRITICAL<" in body
    assert 'data-testid="cell-priority-TC_SERVICE_009">P3' in body
    assert 'data-testid="cell-state-TC_SERVICE_009">reopened' in body

    vlan = get_case_by_test(session, "TC_VLAN_042")
    assert vlan is not None
    assert f'data-testid="cell-age-TC_VLAN_042">{format_age(vlan.opened_at)}' in body

    style = client.get("/static/style.css")
    assert style.status_code == 200
    assert "animation" not in style.text
    assert "gradient" not in style.text


def test_filters_limit_the_queue(client):
    by_component = client.get("/", params={"component": "vlan"})
    assert by_component.status_code == 200
    assert 'data-testid="open-count">1 open' in by_component.text
    assert 'data-testid="open-TC_VLAN_042"' in by_component.text
    assert 'data-testid="open-TC_LINK_013"' not in by_component.text

    by_state = client.get("/", params={"state": "reopened"})
    assert 'data-testid="open-TC_SERVICE_009"' in by_state.text
    assert 'data-testid="open-TC_VLAN_042"' not in by_state.text

    by_severity = client.get("/", params={"severity": "LOW"})
    assert 'data-testid="open-TC_COUNTER_021"' in by_severity.text
    assert 'data-testid="open-TC_SERVICE_009"' not in by_severity.text


def test_new_case_hides_close_and_posting_it_keeps_the_state(client, session):
    vlan = get_case_by_test(session, "TC_VLAN_042")
    assert vlan is not None
    page = client.get(f"/cases/{vlan.id}")
    assert page.status_code == 200
    assert 'data-testid="case-state">new' in page.text
    assert 'data-testid="case-detail">eth1 operational VLAN is 1, expected 20' in page.text
    assert 'data-testid="move-closed"' not in page.text
    assert 'data-testid="move-triaged"' not in page.text
    assert 'data-testid="no-moves"' in page.text

    rejected = client.post(
        f"/cases/{vlan.id}/transitions",
        data={"to_state": "closed", "reason": "skip the path", "actor": "qa.engineer"},
    )
    assert rejected.status_code == 400
    assert 'data-testid="form-error">new -&gt; closed is not a legal move.' in rejected.text
    assert 'data-testid="case-state">new' in rejected.text

    session.rollback()
    fresh = get_case_by_test(session, "TC_VLAN_042")
    assert fresh is not None
    assert fresh.state == "new"
    rows = list_audits(session, fresh.id)
    assert len(rows) == 1
    assert rows[0].accepted is False
    assert rows[0].from_state == "new"
    assert rows[0].to_state == "closed"
    assert rows[0].reason == "skip the path"


def test_history_shows_whether_a_case_was_opened_or_reopened(client):
    vlan = client.get("/history/TC_VLAN_042")
    assert vlan.status_code == 200
    assert 'data-testid="history-result-vlan-1.0.0"' in vlan.text
    assert 'data-testid="history-effect-vlan-1.0.0">recorded' in vlan.text
    assert 'data-testid="history-effect-vlan-1.1.0">case opened' in vlan.text
    assert 'data-testid="history-build-vlan-1.1.0">1.1.0' in vlan.text

    service = client.get("/history/TC_SERVICE_009")
    assert 'data-testid="history-case-state">reopened' in service.text
    assert 'data-testid="history-effect-service-1.0.4">case opened' in service.text
    assert 'data-testid="history-effect-service-1.0.5">passing rerun recorded' in service.text
    assert 'data-testid="history-effect-service-1.1.0">case reopened' in service.text
    assert 'data-testid="history-result-service-1.1.0"' in service.text

    boot = client.get("/history/TC_BOOT_001")
    assert 'data-testid="history-case-state">No case.' in boot.text
    assert 'data-testid="history-effect-boot-1.1.0">recorded' in boot.text


class RunningApp:
    def __init__(self, base_url: str, engine: Engine) -> None:
        self.base_url = base_url
        self.engine = engine


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_http(url: str) -> None:
    deadline = time.monotonic() + 10
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
    raise RuntimeError(f"UI did not start: {last_error}")


@pytest.fixture
def driver() -> Iterator[WebDriver]:
    browser = _build_driver()
    try:
        yield browser
    finally:
        browser.quit()


@pytest.fixture
def demo_app(tmp_path: Path) -> Iterator[RunningApp]:
    yield from _serve(tmp_path, seed_demo)


@pytest.fixture
def ready_to_close_app(tmp_path: Path) -> Iterator[RunningApp]:
    yield from _serve(tmp_path, _seed_service_awaiting_close)


def _serve(tmp_path: Path, seed: Callable[[Session], None]) -> Iterator[RunningApp]:
    engine = make_engine(tmp_path / "live.db")
    init_db(engine)
    factory = session_factory(engine)
    session = factory()
    try:
        seed(session)
        session.commit()
    finally:
        session.close()

    app = create_app(engine)
    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_http(base_url + "/")
        yield RunningApp(base_url, engine)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        engine.dispose()


def _build_driver() -> WebDriver:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    binary = os.environ.get("CHROME_BIN")
    if not binary and _MAC_CHROME.is_file():
        binary = str(_MAC_CHROME)
    if binary:
        options.binary_location = binary
    driver_path = os.environ.get("CHROMEDRIVER")
    if driver_path:
        browser = webdriver.Chrome(service=Service(executable_path=driver_path), options=options)
    else:
        browser = webdriver.Chrome(options=options)
    browser.set_page_load_timeout(15)
    return browser


def _seed_service_awaiting_close(session: Session) -> None:
    runs = {run.run_id: run for run in load_sample_runs()}
    ingest_run(session, runs["service-1.0.4"])
    case = get_case_by_test(session, "TC_SERVICE_009")
    assert case is not None
    set_classification(session, case, severity="CRITICAL", priority="P3")
    assert commit_transition(
        session, case, "triaged", reason="classified", actor="qa.engineer"
    ).accepted
    assert commit_transition(
        session, case, "verify", reason="fix is ready to rerun", actor="qa.engineer"
    ).accepted
    ready = ingest_run(session, runs["service-1.0.5"])
    assert ready.effect is IngestEffect.ready_to_close
    assert case.state == "verify"
    assert case.rerun_recorded is True


def _wait(browser: WebDriver) -> WebDriverWait:
    return WebDriverWait(browser, 10)


@pytest.mark.selenium
def test_selenium_queue_triage_records_an_audit_row(demo_app: RunningApp, driver: WebDriver):
    wait = _wait(driver)
    driver.get(demo_app.base_url + "/")
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='queue']")))

    rows = driver.find_elements(By.CSS_SELECTOR, "[data-testid^='case-row-']")
    assert [row.get_attribute("data-testid") for row in rows][0] == "case-row-TC_SERVICE_009"
    assert driver.find_element(By.CSS_SELECTOR, "[data-testid='cell-priority-TC_VLAN_042']").text == "unset"

    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='open-TC_VLAN_042']"))).click()
    wait.until(EC.text_to_be_present_in_element((By.CSS_SELECTOR, "[data-testid='case-state']"), "new"))
    assert driver.find_elements(By.CSS_SELECTOR, "[data-testid='move-closed']") == []
    assert driver.find_elements(By.CSS_SELECTOR, "[data-testid='move-triaged']") == []

    Select(driver.find_element(By.CSS_SELECTOR, "[data-testid='severity']")).select_by_value("HIGH")
    Select(driver.find_element(By.CSS_SELECTOR, "[data-testid='priority']")).select_by_value("P2")
    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='save-classification']"))).click()

    move = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='move-triaged']")))
    assert driver.find_elements(By.CSS_SELECTOR, "[data-testid='move-closed']") == []
    reason = driver.find_element(By.CSS_SELECTOR, "[data-testid='reason']")
    reason.send_keys("vlan mismatch confirmed")
    move.click()

    wait.until(
        EC.text_to_be_present_in_element(
            (By.CSS_SELECTOR, "[data-testid='audit-log']"),
            "vlan mismatch confirmed",
        )
    )
    assert driver.find_element(By.CSS_SELECTOR, "[data-testid='case-state']").text == "triaged"
    row = driver.find_element(By.CSS_SELECTOR, "[data-to='triaged']")
    assert row.get_attribute("data-from") == "new"
    assert row.get_attribute("data-accepted") == "true"
    assert "qa.engineer" in row.text
    assert "vlan mismatch confirmed" in row.text


@pytest.mark.selenium
def test_selenium_new_case_does_not_offer_close(demo_app: RunningApp, driver: WebDriver):
    wait = _wait(driver)
    driver.get(demo_app.base_url + "/")
    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='open-TC_VLAN_042']"))).click()
    wait.until(EC.text_to_be_present_in_element((By.CSS_SELECTOR, "[data-testid='case-state']"), "new"))
    assert driver.find_elements(By.CSS_SELECTOR, "[data-testid='move-closed']") == []
    assert driver.find_element(By.CSS_SELECTOR, "[data-testid='no-moves']").text


@pytest.mark.selenium
def test_selenium_close_then_later_failure_reopens(ready_to_close_app: RunningApp, driver: WebDriver):
    wait = _wait(driver)
    driver.get(ready_to_close_app.base_url + "/")
    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='open-TC_SERVICE_009']"))).click()
    wait.until(EC.text_to_be_present_in_element((By.CSS_SELECTOR, "[data-testid='case-state']"), "verify"))
    assert driver.find_element(By.CSS_SELECTOR, "[data-testid='rerun-status']").text == "Passing rerun recorded."

    driver.find_element(By.CSS_SELECTOR, "[data-testid='reason']").send_keys("rerun passed on 1.0.5")
    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='move-closed']"))).click()
    wait.until(EC.text_to_be_present_in_element((By.CSS_SELECTOR, "[data-testid='case-state']"), "closed"))
    closed = driver.find_element(By.CSS_SELECTOR, "[data-to='closed']")
    assert closed.get_attribute("data-from") == "verify"
    assert closed.get_attribute("data-accepted") == "true"
    assert "rerun passed on 1.0.5" in closed.text

    runs = {run.run_id: run for run in load_sample_runs()}
    factory = session_factory(ready_to_close_app.engine)
    session = factory()
    try:
        reopened = ingest_run(session, runs["service-1.1.0"])
        session.commit()
    finally:
        session.close()
    assert reopened.effect is IngestEffect.reopened

    driver.refresh()
    wait.until(EC.text_to_be_present_in_element((By.CSS_SELECTOR, "[data-testid='case-state']"), "reopened"))
    again = driver.find_element(By.CSS_SELECTOR, "[data-to='reopened']")
    assert again.get_attribute("data-from") == "closed"
    assert again.get_attribute("data-accepted") == "true"
    assert "failed again on 1.1.0" in again.text
    assert driver.find_element(By.CSS_SELECTOR, "[data-to='closed']").get_attribute("data-accepted") == "true"
