"""
Research as a background job.

The behaviour under test is not "async is nicer". It is that the deliverable
no longer depends on an HTTP request surviving a 10-20 minute silence, which
no proxy in the path is willing to promise — Railway cuts at 5 minutes of
silence, Heroku-style routers at 30 seconds, nginx at 60. Every one of those
failures used to look identical from outside: the run completed, the brief was
stored, and the caller got a 502.

So the assertions are about the contract that replaced it. Submit returns
immediately with an id; progress is observable per node while the run is in
flight; the finished result has exactly the shape the old blocking endpoint
returned; and a second submit for a city already running does not start a
duplicate pass over it.

Runs in MOCK mode, so no keys, no internet and no Neo4j.
"""
import asyncio
import os

# Assigned rather than setdefault: an inherited RUN_MODE=LIVE silently points
# the whole suite at the live internet, the LLM provider and Neo4j, where it
# hangs on connect instead of failing, and the docstring above stops being
# true. The mode has to be ours to claim "no keys, no internet, no Neo4j".
os.environ["RUN_MODE"] = "MOCK"
os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test_cardio4cities.db")

import pytest

from app.jobs import QUEUED, RUNNING, SUCCEEDED, TERMINAL_STATES, JobRegistry


async def _drain(job, timeout: float = 120.0) -> None:
    """Wait for a job to reach a terminal state, or fail the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while job.state not in TERMINAL_STATES:
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(f"job stuck in {job.state} after {timeout}s")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_submit_returns_before_the_run_finishes():
    """The whole point: the caller gets an id, not a brief. If this ever
    blocks, every proxy deadline in docs/DEPLOYMENT.md §4.1 applies again."""
    registry = JobRegistry()

    job, _ = await registry.submit("Testopolis", "Testland")

    # Not yet finished at the instant submit returned — the run is on the
    # event loop behind us, and has had no chance to progress.
    assert job.state in (QUEUED, RUNNING)
    assert job.result is None
    assert job.job_id

    await _drain(job)


@pytest.mark.asyncio
async def test_a_finished_job_carries_the_old_endpoint_s_payload():
    """`result` is the body the blocking POST used to return, unchanged, so a
    client migrates by reading one extra level of nesting rather than by
    relearning the response."""
    registry = JobRegistry()
    job, _ = await registry.submit("Testopolis", None)
    await _drain(job)

    assert job.state == SUCCEEDED, job.error
    result = job.result
    assert result["city"] == "Testopolis"
    assert result["report_markdown"], "the brief is the deliverable"
    assert set(result["counts"]) >= {
        "candidates_considered",
        "sources_allowed",
        "sources_denied",
        "verified_facts",
        "gaps",
    }


@pytest.mark.asyncio
async def test_progress_is_reported_per_node_not_as_one_lump():
    """A run in progress used to be indistinguishable from a hung one. Each
    node now reports its own state, duration and what it produced — the same
    measurement the server logs, so the two cannot disagree."""
    registry = JobRegistry()
    job, _ = await registry.submit("Testopolis", None)
    await _drain(job)

    payload = job.as_dict()
    by_name = {stage["name"]: stage for stage in payload["stages"]}

    # Every node in the compiled graph appears, and the ones that ran say so.
    assert {"planner", "search", "extraction", "fact_check", "report"} <= set(by_name)
    assert by_name["report"]["state"] == SUCCEEDED
    assert by_name["report"]["passes"] >= 1
    assert by_name["search"]["summary"], "a finished node should say what it produced"
    assert payload["current_stage"] is None, "nothing should still be running"


@pytest.mark.asyncio
async def test_a_second_submit_for_a_running_city_is_not_a_second_run():
    """Two concurrent passes over one city would write the same facts twice
    and spend the overlap contending for the SQLite write lock. The usual way
    to trigger it is a double-click on Run."""
    registry = JobRegistry()

    first, _ = await registry.submit("Testopolis", None)
    existing = await registry.active_for_city("Testopolis")

    assert existing is not None
    assert existing.job_id == first.job_id

    # Case-insensitively, since the city is typed by a human.
    assert (await registry.active_for_city("testOPOLIS")) is not None

    await _drain(first)
    assert (await registry.active_for_city("Testopolis")) is None


@pytest.mark.asyncio
async def test_cancelling_a_queued_job_does_not_leave_it_stuck(monkeypatch):
    """A job cancelled before it ever got a concurrency slot must still reach
    a terminal state. Left at `queued` it would block every later request for
    that city and never expire — and cancelling a task that has not taken its
    first step throws into the coroutine before its handler can run, so the
    registry has to mark this itself."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "MAX_CONCURRENT_RESEARCH", 1)
    registry = JobRegistry()

    first, _ = await registry.submit("Testopolis", None)
    queued, _ = await registry.submit("Otherville", None)

    await registry.cancel(queued.job_id)

    assert queued.state in TERMINAL_STATES
    assert queued.state == "cancelled"
    assert (await registry.active_for_city("Otherville")) is None

    await _drain(first)


@pytest.mark.asyncio
async def test_a_failed_run_reports_why_rather_than_vanishing():
    """A job that dies must say so. Silence here is the failure mode the whole
    design exists to remove — the caller cannot distinguish a dead run from a
    slow one."""
    import app.jobs as jobs_module

    registry = JobRegistry()

    class _Boom:
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("graph exploded")

    # Swapped on the module rather than on the compiled graph, which is a
    # pydantic model and refuses attribute assignment. `_run` looks the name
    # up in module globals at call time, so this is the seam.
    original = jobs_module.workflow
    jobs_module.workflow = _Boom()
    try:
        job, _ = await registry.submit("Doomsville", None)
        await _drain(job)
    finally:
        jobs_module.workflow = original

    assert job.state == "failed"
    assert "RuntimeError" in job.error and "graph exploded" in job.error
    assert job.result is None

    # The stage it died in must not be left claiming to be running. Otherwise
    # `current_stage` keeps naming a node on a job that has already failed and
    # the browser's checklist spins on a run that ended.
    assert job.as_dict()["current_stage"] is None
    assert not any(s.state == RUNNING for s in job.stages.values())


@pytest.mark.asyncio
async def test_a_summary_that_raises_still_finishes_the_job():
    """Summarising runs after the graph and reads a dozen keys off the final
    state, so it can raise on a shape it did not expect. Outside a handler
    that stranded the job at `running` with no `finished_at`: never purged,
    blocking that city for ever, and polled by a browser that would never be
    told it had stopped."""
    import app.jobs as jobs_module

    registry = JobRegistry()

    def _explode(city, final_state):
        raise KeyError("report_markdown")

    original = jobs_module.summarise_run
    jobs_module.summarise_run = _explode
    try:
        job, _ = await registry.submit("Summaryville", None)
        await _drain(job)
    finally:
        jobs_module.summarise_run = original

    assert job.state in TERMINAL_STATES, "a job must never be left running"
    assert job.state == "failed"
    assert "KeyError" in job.error
    assert job.finished_at is not None, "an unfinished job is never purged"


@pytest.mark.asyncio
async def test_submit_itself_refuses_a_duplicate_city():
    """The check and the insert have to happen under one lock. Done as two
    awaits in the caller, two clicks could both find nothing and both start a
    run — which is the case the de-duplication exists for."""
    registry = JobRegistry()

    first, created_first = await registry.submit("Testopolis", None)
    second, created_second = await registry.submit("testOPOLIS", None)

    assert created_first is True
    assert created_second is False, "the duplicate must not start a second run"
    assert second.job_id == first.job_id

    await _drain(first)

    # Once the first run is terminal the city is free again.
    third, created_third = await registry.submit("Testopolis", None)
    assert created_third is True
    assert third.job_id != first.job_id
    await _drain(third)
