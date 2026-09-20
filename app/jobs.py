"""
Research runs as background jobs, with progress you can poll.

`POST /research` used to run the whole pipeline inside the HTTP request and
answer with the finished brief. That is the simplest possible design and it
works on a laptop, but it makes the deliverable depend on a single TCP
connection surviving a 10-20 minute silence, and nothing in the path between
a browser and uvicorn is willing to promise that:

  * Railway closes a request after 5 minutes with no bytes transferred, and
    caps even a chatty one at 15.
  * Heroku-style routers cut at 30 seconds of silence.
  * Nginx and most corporate proxies default to 60s.
  * Browsers and `fetch` impose their own ceilings.

Every one of those failures looks identical from the outside — the run keeps
going, the container is healthy, and the user sees a 502 — which is the worst
available shape for a bug. Worse, the brief *was* produced and stored; only
the response was lost.

So the request now registers a job and returns its id, and the run happens on
the event loop behind it. Three things follow, and the second is the one that
justifies the change beyond deployability:

  1. No request is ever long, so no proxy deadline applies.
  2. Progress becomes observable. The pipeline already logged each node's
     start, duration and output; that stream now reaches the client, so the
     UI can show which of the nine nodes is running instead of nine
     simultaneously-spinning placeholders.
  3. A closed laptop no longer abandons a run.

The registry is in-process and deliberately so: a job is a handle on a
running asyncio task, which cannot be shared across replicas, and this
deployment is one container by construction (SQLite and Milvus Lite both
assume a single writer — see docs/DEPLOYMENT.md §4.1). The consequence to
know is that a restart forgets in-flight jobs. It does not lose finished work:
the report, facts, sources and gaps are written to the relational store by the
pipeline itself, so `GET /report/{city}` answers with or without the job.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from app.config import settings
from app.graph.workflow import NODES, WORKFLOW_CONFIG, workflow
from app.llm.usage import UsageSnapshot, ledger
from app.models.schemas import ConfidenceTier, CrawlVerdict
from app.telemetry import span

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = (SUCCEEDED, FAILED, CANCELLED)

# The execution order, which is what the UI renders as a checklist. Taken from
# the workflow rather than written out again, so a node added to the graph
# appears in the progress view without a second edit.
STAGE_NAMES: list[str] = list(NODES)


@dataclass
class Stage:
    """One node's progress. `passes` counts re-entries rather than replacing
    the record, because the planner retry loop legitimately runs every node
    more than once and a view that overwrote would make a second pass look
    like the first having restarted."""

    name: str
    state: str = QUEUED
    passes: int = 0
    seconds: float = 0.0
    summary: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "passes": self.passes,
            "seconds": round(self.seconds, 1),
            "summary": self.summary,
        }


@dataclass
class Job:
    job_id: str
    city: str
    country: str | None
    state: str = QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    stages: dict[str, Stage] = field(
        default_factory=lambda: {name: Stage(name) for name in STAGE_NAMES}
    )
    result: dict | None = None
    error: str | None = None
    usage: dict | None = None
    _usage_before: UsageSnapshot | None = None
    _task: asyncio.Task | None = None

    # -- progress, called by the workflow's node wrapper ---------------
    def stage_started(self, name: str) -> None:
        stage = self.stages.setdefault(name, Stage(name))
        stage.state = RUNNING
        stage.passes += 1

    def stage_finished(self, name: str, seconds: float, summary: str) -> None:
        stage = self.stages.setdefault(name, Stage(name))
        stage.state = SUCCEEDED
        # Accumulated, not replaced: on a retry pass the interesting number is
        # what the node has cost in total, which is what the log already shows
        # per pass.
        stage.seconds += seconds
        stage.summary = summary

    def abandon_running_stages(self) -> None:
        """Mark any stage still `running` as failed.

        A node that raises never reaches `stage_finished`, so without this the
        stage it died in stays `running` for ever. Two things then misreport:
        `as_dict`'s `current_stage` keeps naming a node on a job that has
        already failed, and the browser's progress checklist spins on a run
        that ended minutes ago. Called from the terminal paths in `_run`, so
        it covers a node failing anywhere rather than only inside the timing
        wrapper.
        """
        for stage in self.stages.values():
            if stage.state == RUNNING:
                stage.state = FAILED

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return end - (self.started_at or self.created_at)

    def as_dict(self, *, include_result: bool = True) -> dict:
        payload = {
            "job_id": self.job_id,
            "city": self.city,
            "country": self.country,
            "status": self.state,
            "elapsed_seconds": round(self.elapsed, 1),
            "stages": [self.stages[name].as_dict() for name in STAGE_NAMES],
            "current_stage": next(
                (n for n in STAGE_NAMES if self.stages[n].state == RUNNING), None
            ),
            "llm_usage": self.usage,
            "error": self.error,
        }
        if include_result:
            payload["result"] = self.result
        return payload


class JobRegistry:
    """Jobs by id, with a concurrency cap and expiry of old results."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._slots: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        # Built on first use rather than in __init__: constructing an
        # asyncio primitive at import time binds it to whichever loop happens
        # to exist then, which under pytest-asyncio is not the loop the app
        # later runs on.
        if self._slots is None:
            self._slots = asyncio.Semaphore(max(settings.MAX_CONCURRENT_RESEARCH, 1))
        return self._slots

    async def submit(self, city: str, country: str | None) -> tuple[Job, bool]:
        """Register a run for `city`, or return the one already in flight.

        Returns `(job, created)`. The de-duplication happens here, under the
        same lock that inserts, rather than in the caller: checking with
        `active_for_city` and then calling `submit` releases the lock in
        between, so two requests for one city could both find nothing and both
        start a run — the exact double-click this is meant to make idempotent.
        Callers get `created` because they cannot otherwise tell an accepted
        submission from a rejected duplicate.
        """
        async with self._lock:
            self._purge_expired()
            existing = self._active_for_city_locked(city)
            if existing is not None:
                return existing, False
            job = Job(job_id=uuid.uuid4().hex[:16], city=city, country=country)
            self._jobs[job.job_id] = job
        # The task reference is held on the job, because asyncio only keeps a
        # weak reference to a running task: without a strong one somewhere, a
        # garbage collection mid-run can cancel the research.
        job._task = asyncio.create_task(self._run(job), name=f"research:{job.job_id}")
        return job, True

    async def _run(self, job: Job) -> None:
        # The root span for the whole run. It has to be created here rather
        # than in the request handler, because the request returns a job id
        # in milliseconds and the work outlives it by minutes — tracing it
        # from the handler would produce a trace that ends before the run
        # starts. Everything downstream nests under this: ten nodes, the
        # model calls inside them, and the store round trips inside those.
        with span(
            f"research {job.city}",
            **{
                "cardio4cities.city": job.city,
                "cardio4cities.country": job.country,
                "cardio4cities.job_id": job.job_id,
            },
        ) as run_span:
            await self._run_traced(job, run_span)

    async def _run_traced(self, job: Job, run_span) -> None:
        try:
            # Queued until a slot frees up, and visibly so — a job waiting
            # behind another run is a normal state, not a stall, and the
            # client can say which it is.
            async with self._semaphore():
                job.state = RUNNING
                job.started_at = time.time()
                job._usage_before = ledger.snapshot()
                logger.info("[%s] research job %s started", job.city, job.job_id)

                config = dict(WORKFLOW_CONFIG)
                # LangGraph hands `configurable` to each node, which is how
                # the timing wrapper in app/graph/workflow.py reaches this job
                # without the workflow having to know jobs exist.
                config["configurable"] = {
                    **config.get("configurable", {}),
                    "job": job,
                }

                try:
                    final_state = await workflow.ainvoke(
                        {
                            "city": job.city,
                            "country": job.country,
                            "retry_count": 0,
                        },
                        config=config,
                    )
                    # Inside the `try` on purpose. Summarising reads a dozen
                    # keys off the final state, so it can raise on a shape it
                    # did not expect — and outside a handler that stranded the
                    # job at `running` with no `finished_at`: never purged,
                    # blocking the city for ever, and polled by a browser that
                    # would never be told it had stopped.
                    job.result = summarise_run(job.city, final_state)
                except Exception as exc:
                    job.state = FAILED
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.finished_at = time.time()
                    job.abandon_running_stages()
                    run_span.record_exception(exc)
                    run_span.set_attribute("cardio4cities.job.state", FAILED)
                    logger.exception(
                        "[%s] research job %s failed", job.city, job.job_id
                    )
                    return
                finally:
                    if job._usage_before is not None:
                        job.usage = (
                            ledger.snapshot() - job._usage_before
                        ).as_dict()
                        # Put on the root span so the cost of a run is
                        # readable without opening its children. This is the
                        # number anyone asks for first.
                        run_span.set_attributes(
                            {
                                f"cardio4cities.usage.{key}": value
                                for key, value in job.usage.items()
                                if isinstance(value, (int, float, str, bool))
                            }
                        )

                job.state = SUCCEEDED
                job.finished_at = time.time()
                run_span.set_attribute("cardio4cities.job.state", SUCCEEDED)
                logger.info(
                    "[%s] research job %s finished in %.1fs",
                    job.city,
                    job.job_id,
                    job.elapsed,
                )
        except asyncio.CancelledError:
            # Outside the semaphore, because a job can be cancelled while it
            # is still queued behind another run — in which case it never
            # entered the block above. Marked terminal either way: left as
            # `queued`, it would block every later request for that city
            # (see active_for_city) and never become eligible for expiry.
            job.state = CANCELLED
            job.error = "Cancelled before the brief was written."
            job.finished_at = time.time()
            job.abandon_running_stages()
            raise

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def cancel(self, job_id: str) -> Job | None:
        job = await self.get(job_id)
        if job is None or job.state in TERMINAL_STATES:
            return job
        if job._task is not None:
            job._task.cancel()
        # Marked here rather than left to the task's own handler. Cancelling a
        # task that has not taken its first step throws into the coroutine
        # before the body runs, so the handler in `_run` never executes — and
        # a job stuck at `queued` blocks every later request for that city
        # (see active_for_city) and never becomes eligible for expiry. The
        # handler still exists for cancellation that arrives from elsewhere,
        # such as loop shutdown, and setting the same values twice is
        # harmless.
        job.state = CANCELLED
        job.error = "Cancelled before the brief was written."
        job.finished_at = time.time()
        return job

    def _active_for_city_locked(self, city: str) -> Job | None:
        """`active_for_city` without taking the lock. Callers must hold it.

        Split out so `submit` can check and insert without releasing the lock
        in between — see the note there.
        """
        return next(
            (
                job
                for job in self._jobs.values()
                if job.state not in TERMINAL_STATES
                and job.city.casefold() == city.casefold()
            ),
            None,
        )

    async def active_for_city(self, city: str) -> Job | None:
        """An unfinished job for this city, if one exists.

        Used to make a second click on Run idempotent rather than starting a
        duplicate run: two concurrent passes over the same city would write
        the same facts twice and contend for the SQLite write lock while doing
        it. Matched case-insensitively, since the city is user-typed.
        """
        async with self._lock:
            return self._active_for_city_locked(city)

    async def list_jobs(self) -> list[dict]:
        async with self._lock:
            self._purge_expired()
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [job.as_dict(include_result=False) for job in jobs]

    def _purge_expired(self) -> None:
        """Drop finished jobs past their retention window. Called under the
        lock from the paths that already take it."""
        cutoff = time.time() - max(settings.JOB_RETENTION_SECONDS, 60)
        for job_id, job in list(self._jobs.items()):
            if job.state in TERMINAL_STATES and (job.finished_at or 0) < cutoff:
                del self._jobs[job_id]


def summarise_run(city: str, final_state: dict) -> dict:
    """The finished-run payload.

    Lifted out of the endpoint unchanged so the polled result has exactly the
    shape the old blocking `POST /research` returned. Anything reading that
    response — the UI, a script, the README's curl example — keeps working by
    reading `result` instead of the body.
    """
    fact_checked = final_state.get("fact_checked", [])
    crawl_results = final_state.get("crawl_results", [])

    def tier_count(tier: ConfidenceTier) -> int:
        return sum(1 for f in fact_checked if f.tier == tier)

    return {
        "city": city,
        "dimensions": final_state.get("dimensions", []),
        "covered_dimensions": final_state.get("covered_dimensions", []),
        "uncovered_dimensions": final_state.get("uncovered_dimensions", []),
        "research_passes": final_state.get("retry_count", 0) + 1,
        "report_markdown": final_state.get("report_markdown"),
        "warnings": final_state.get("warnings", []),
        "counts": {
            "candidates_considered": len(crawl_results),
            "sources_allowed": sum(
                1 for r in crawl_results if r.verdict == CrawlVerdict.ALLOWED
            ),
            "sources_denied": sum(
                1 for r in crawl_results if r.verdict == CrawlVerdict.DENIED
            ),
            "sources_read": len(final_state.get("passages", [])),
            "verified_facts": tier_count(ConfidenceTier.VERIFIED),
            "single_source_facts": tier_count(ConfidenceTier.SINGLE_SOURCE),
            "conflicting_facts": tier_count(ConfidenceTier.CONFLICTING),
            "unsupported_claims": tier_count(ConfidenceTier.UNSUPPORTED),
            "gaps": len(final_state.get("gaps", [])),
        },
    }


registry = JobRegistry()
