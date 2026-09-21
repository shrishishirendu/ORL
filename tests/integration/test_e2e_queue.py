"""**True end-to-end** tests: enqueue a real job onto real Redis, run a real
``arq`` ``Worker`` loop (in ``burst`` mode -- it processes everything queued
and then returns rather than polling forever) to actually process it, and
confirm the result was persisted to a real Postgres. Nothing here is
mocked: this is the one place in this task's test suite that exercises the
full ``API enqueue -> Redis -> arq worker -> DB-facing service -> Postgres``
path for real (the router tests hit real Redis for the enqueue half only;
``test_workers.py`` calls the task functions directly, skipping the queue
entirely).

Requires a live Redis at ``app.core.settings.settings.redis_url`` in
addition to the Postgres every other file under ``tests/integration/``
needs -- skipped automatically if Redis isn't reachable.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job
from arq.worker import Worker as ArqWorker
from redis.exceptions import RedisError
from sqlalchemy import select

from app.core.settings import settings
from app.models.enums import ReoptimizationResolution, RosterStatus
from app.models.reoptimization import ReoptimizationEvent
from app.models.roster import Roster
from app.workers.tasks import (
    handle_reoptimization_event_task,
    solve_roster_task,
    solve_route_task,
)
from tests.integration.factories import (
    make_award_row,
    make_job,
    make_roster,
    make_roster_assignment,
    make_shift,
    make_site,
    make_travel_entry,
    make_worker,
)

pytestmark = pytest.mark.asyncio

SHIFT_DATE = date(2026, 3, 6)


async def _redis_reachable() -> bool:
    try:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await pool.ping()
        await pool.aclose(close_connection_pool=True)
        return True
    except (RedisError, OSError):
        return False


@pytest_asyncio.fixture(autouse=True)
async def _require_redis():
    if not await _redis_reachable():
        pytest.skip("Redis is not reachable at settings.redis_url")
    yield


async def _run_burst_worker() -> None:
    """Process every job currently on the queue, then return."""
    worker = ArqWorker(
        functions=[solve_roster_task, solve_route_task, handle_reoptimization_event_task],
        redis_settings=RedisSettings.from_dsn(settings.redis_url),
        burst=True,
        handle_signals=False,
    )
    try:
        await worker.async_run()
    finally:
        await worker.close()


async def test_solve_roster_task_round_trips_through_redis_and_a_real_worker(
    db_session,
) -> None:
    home = await make_site(db_session, "HOME", region="North")
    worker_row = await make_worker(db_session, home, skills=["driver"])
    shift = await make_shift(
        db_session,
        home,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="driver",
    )
    await make_award_row(db_session, worker_row, shift, pay_cost=99.0)
    await db_session.commit()

    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        enqueued = await redis.enqueue_job(
            "solve_roster_task", SHIFT_DATE.isoformat(), SHIFT_DATE.isoformat()
        )
        assert enqueued is not None

        await _run_burst_worker()

        result = await Job(enqueued.job_id, redis).result(timeout=10)
    finally:
        await redis.aclose(close_connection_pool=True)

    assert result["status"] == "solved"
    assert result["total_cost"] == pytest.approx(99.0)

    roster = (
        await db_session.execute(select(Roster).where(Roster.id == result["roster_id"]))
    ).scalar_one()
    assert roster.status is RosterStatus.SOLVED


async def test_worker_sick_escalation_chains_a_real_roster_job_through_the_queue(
    db_session,
) -> None:
    """The flagship job-chaining behaviour, exercised for real: a
    ``worker_sick`` event queued and processed by a real ``arq`` worker
    enqueues a *second*, real ``solve_roster_task`` job onto the same
    queue -- and, because ``burst`` mode keeps polling until the queue is
    actually empty (not just "empty when it started"), that chained job
    gets picked up and processed in the same worker run.
    """
    home = await make_site(db_session, "HOME2", region="North")
    site_a = await make_site(db_session, "SITE-A2")
    worker_row = await make_worker(db_session, home, skills=["driver"])
    shift = await make_shift(
        db_session,
        home,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="driver",
        is_multi_stop=True,
    )
    await make_job(
        db_session,
        shift,
        site_a,
        window_start=datetime(2026, 3, 6, 9, 0),
        window_end=datetime(2026, 3, 6, 10, 0),
        duration_minutes=15,
    )
    await make_travel_entry(db_session, home, site_a, 10)
    roster = await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    await make_roster_assignment(db_session, roster, worker_row, shift)
    await db_session.commit()

    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        enqueued = await redis.enqueue_job(
            "handle_reoptimization_event_task",
            {
                "event_type": "worker_sick",
                "shift_id": shift.id,
                "worker_id": worker_row.id,
            },
        )
        assert enqueued is not None

        await _run_burst_worker()

        reopt_result = await Job(enqueued.job_id, redis).result(timeout=10)
        assert reopt_result["outcome"] == "escalated_to_tier1"
        chained_job_id = reopt_result["escalation_roster_job_id"]
        assert chained_job_id is not None

        chained_result = await Job(chained_job_id, redis).result(timeout=10)
    finally:
        await redis.aclose(close_connection_pool=True)

    # The chained Tier 1 job actually ran (inside the same burst) and
    # persisted its own Roster.
    assert chained_result["status"] in ("solved", "failed")

    event_row = (
        await db_session.execute(
            select(ReoptimizationEvent).where(ReoptimizationEvent.id == reopt_result["event_id"])
        )
    ).scalar_one()
    assert event_row.resolution is ReoptimizationResolution.ESCALATED_TO_TIER1
