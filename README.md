A tracker that turns simulated networking test failures into prioritized cases, allows only legal triage moves, and keeps an audit log of who moved them and why.

Severity is impact. Priority is urgency. They are stored separately, and the queue sorts by priority, then severity, then oldest open case. It does not sort by severity alone.

The seeded queue is the example. TC_SERVICE_009 is CRITICAL and was prioritized P3 when it was triaged on build 1.0.4, then closed, then reopened by the 1.1.0 failure. TC_VLAN_042 is HIGH with no priority yet, so it sits in the untriaged group under that P3 case. TC_COUNTER_021 is LOW, also with no priority. Set the counter case to P1 and it moves above the CRITICAL / P3 service case: a low-severity failure can block a release, and a critical failure can wait when it is not the most urgent item.

## States

The path lives in `rules/workflow.yaml`. The engine reads that file.

`new → triaged → verify → closed`, and `closed → reopened → triaged`. From verify, a failed rerun goes back to triaged. Every move needs a non-empty reason.

A brand-new failure cannot be closed. `new → closed` is rejected, the case stays in `new`, and the attempt is written to the audit log. Closing is allowed only from verify, and only after a passing rerun of the same test has been recorded. The pass does not close the case. A person still submits verify → closed with a reason.

triaged is refused until both severity and priority are set. The buttons on the case page are the legal next states only. A direct POST of any other move returns an error and does not change the state.

Severity is CRITICAL, HIGH, MEDIUM, or LOW. Priority is P1, P2, P3, or P4.

## What ingest does

Runs are JSON files (`run_id`, `build`, `test_case`, `result`, `component`, `title`, `detail`, `evidence`). Nothing here talks to hardware. The same `ingest_run` function serves the demo loader and any later caller.

- A failure with no open case for that test opens one case in `new`. The run is the evidence.
- A second failure while that case is still open attaches the run. It does not open another case. If that case is in verify, the new failure also clears the ready-to-close mark, so an older pass cannot be used to close it.
- A failure whose case is closed moves it to `reopened` and writes an audit row. The reason is `failed again on <build>`.
- A pass while the case is in verify marks it ready for a person to close. The state stays verify.
- A pass in any other state is stored on the test's run history and does not move the case.

TC_BOOT_001 passes on 1.1.0 and never becomes a case. TC_VLAN_042 passed on 1.0.0 and failed on 1.1.0, so the history page shows a pass with no case event, then a failure that opened one. TC_SERVICE_009 failed on 1.0.4, passed on 1.0.5, was closed, and failed again on 1.1.0, which reopened it.

Audit rows are insert-only. The table records `from_state`, `to_state`, `actor`, `reason`, and the timestamp. There is no update and no delete. The actor is a name typed into the form. It defaults to `qa.engineer`. Ingest writes `ingest` when it reopens a case. There is no login.

## Run the demo

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
tracker-demo
```

`python -m tracker` does the same thing. It loads `sample_runs/` into `var/triage.db` and serves the UI at http://127.0.0.1:8000. Delete `var/triage.db` to load the sample runs again.

## Tests

```bash
pytest
```

Transition tests are table-driven from `rules/workflow.yaml`: one row per allowed move, and one row per forbidden pair among the five states. They include `new → closed` and the rule that triaged is illegal before severity and priority are set. Ingest tests cover the first failure, a repeat failure, a reopen after close, and a pass in verify that does not auto-close. `test_low_p1_sorts_ahead_of_critical_p3` locks the sort. The audit test checks that two moves produce two rows, in order, with the stored `from_state` and reason, and that no public method can update a row.

```bash
pytest -m "not selenium"
```

That skips the browser. The default run, including CI, does not.

## Selenium

Selenium drives the real pages in headless Chrome. Elements it needs have stable `data-testid` attributes. The tests use explicit waits.

1. Open the queue, open TC_VLAN_042, set HIGH and P2, move `new → triaged` with a reason, and see the audit row.
2. On that new case, confirm there is no close action. A separate client POST of `new → closed` still gets an error, and the case stays `new`.
3. Start the service case in verify with a passing rerun already recorded, close it with a reason, ingest a later failure, and see `reopened` plus both audit rows.

## Docker and CI

The image is `python:3.12-slim` plus Debian Chromium and Chromedriver. It installs this package, and the default command is pytest. Local and CI run that same image, so the browser tests do not depend on whatever Chromium the GitHub runner happens to have.

```bash
docker build -t triage .
docker run --rm triage
```

GitHub Actions runs on push and pull request. The workflow checks out the repo, builds this image, and runs it.

## What this tool does not do

It does not talk to real devices, and it does not SSH anywhere. It does not log people in. It does not close a case because a run passed. A person closes it from verify, with a reason, after the rerun is on record.

## Two-minute demo

The queue page shows these same three steps, with the next action on each row.

1. Open the queue. TC_VLAN_042 is HIGH with priority unset, so it is not sorted by severity: TC_SERVICE_009 (CRITICAL / P3) is above it. Open TC_COUNTER_021, set LOW and P1, save, and go back to the queue. That LOW / P1 case is now above the CRITICAL / P3 service case.
2. Open TC_VLAN_042. Set HIGH and P2, save, then move `new → triaged` with a reason. The audit row shows `new → triaged`, `qa.engineer`, and that reason. Close is not offered on the new case.
3. Open TC_SERVICE_009. The state is `reopened`. The audit log has the close (`verify → closed`, reason `rerun passed on 1.0.5`) and the reopen (`closed → reopened`, reason `failed again on 1.1.0`). Run history shows the 1.0.4 failure opening the case, the 1.0.5 pass recording a rerun, and the 1.1.0 failure reopening it.
