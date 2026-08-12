"""Unit tests for app.scheduler.runner's heartbeat instrumentation.

_run_job_loop is the single choke point every job in every service passes
through (app/main.py, app/scout/main.py) - these tests drive it directly
with a fake job rather than through a real service entrypoint, and
monkeypatch record_success/record_failure (imported names on the runner
module) so no database is touched, matching test_healthz.py's pattern of
patching at the point of use rather than mocking a whole DB layer.
"""

import asyncio

import pytest

from app.scheduler import runner


def _stopping_job(stop_event: asyncio.Event, *, fail: bool) -> runner.PeriodicJob:
    """A PeriodicJob whose run() sets stop_event after its first call, so
    _run_job_loop exits after exactly one iteration instead of looping
    forever.
    """

    async def run() -> None:
        stop_event.set()
        if fail:
            raise ValueError("boom")

    return runner.PeriodicJob(name="test_job", run=run, interval_seconds=60)


@pytest.mark.asyncio
async def test_successful_job_records_success_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(runner, "record_success", lambda *a: calls.append(("success", *a)))
    monkeypatch.setattr(runner, "record_failure", lambda *a: calls.append(("failure", *a)))

    stop_event = asyncio.Event()
    job = _stopping_job(stop_event, fail=False)

    await runner._run_job_loop(job, stop_event, "collectors")

    assert calls == [("success", "collectors", "test_job", 60)]


@pytest.mark.asyncio
async def test_failing_job_records_failure_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(runner, "record_success", lambda *a: calls.append(("success", *a)))
    monkeypatch.setattr(runner, "record_failure", lambda *a: calls.append(("failure", *a)))

    stop_event = asyncio.Event()
    job = _stopping_job(stop_event, fail=True)

    await runner._run_job_loop(job, stop_event, "scout")

    assert len(calls) == 1
    kind, service, job_name, interval_seconds, exc = calls[0]
    assert kind == "failure"
    assert service == "scout"
    assert job_name == "test_job"
    assert interval_seconds == 60
    assert isinstance(exc, ValueError)
