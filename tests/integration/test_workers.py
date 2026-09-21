"""Tests for ``app.workers.tasks`` -- the ``arq`` task functions.

**Coverage note (see the task report):** these tests call the task
functions **directly as plain async functions** (``await
solve_roster_task(ctx, ...)``), bypassing ``arq``/Redis entirely, against a
real Postgres. This is direct/mocked coverage of the task-wrapper layer
(does it call the right service and shape its result correctly?), not proof
that a real queued job round-trips through Redis and a real worker process
-- see ``test_e2e_queue.py`` for that.

The escalation job-chaining test below uses a **mocked** ``ctx["redis"]``
(an ``AsyncMock``) to verify ``handle_reoptimization_event_task`` enqueues a
``solve_roster_task`` on escalation, without needing a real queue for this
one assertion.
"""

from __future__ import annotations

from datetime import date, datetime, time
from unittest.mock import AsyncMock

import pytest

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

SHIFT_DATE = date(2026, 3, 5)


async def test_solve_roster_task_persists_and_returns_a_json_safe_dict(db_session) -> None:
    home = await make_site(db_session, "HOME", region="North")
    worker = await make_worker(db_session, home, skills=["driver"])
    shift = await make_shift(
        db_session,
        home,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="driver",
    )
    await make_award_row(db_session, worker, shift, pay_cost=75.0)
    await db_session.commit()

    result = await solve_roster_task(
        {}, SHIFT_DATE.isoformat(), SHIFT_DATE.isoformat()
    )

    assert result["status"] == "solved"
    assert result["total_cost"] == pytest.approx(75.0)
    assert result["failure_reason"] is None
    assert result["unfilled_shift_ids"] == []
    assert isinstance(result["roster_id"], int)


async def test_solve_route_task_persists_and_returns_a_json_safe_dict(db_session) -> None:
    home = await make_site(db_session, "HOME2", region="North")
    job_site = await make_site(db_session, "SITE-B")
    worker = await make_worker(db_session, home, skills=["driver"])
    shift = await make_shift(
        db_session,
        job_site,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="driver",
        is_multi_stop=False,
    )
    roster = await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    roster_assignment = await make_roster_assignment(db_session, roster, worker, shift)
    await db_session.commit()

    result = await solve_route_task({}, roster_assignment.id)

    assert result["mode"] == "site_assignment"
    assert result["site_assignment_id"] is not None
    assert result["route_id"] is None


async def test_reoptimization_task_chains_a_roster_job_on_escalation(db_session) -> None:
    home = await make_site(db_session, "HOME3", region="North")
    site_a = await make_site(db_session, "SITE-A3")
    worker = await make_worker(db_session, home, skills=["driver"])
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
        window_start=datetime(2026, 3, 5, 9, 0),
        window_end=datetime(2026, 3, 5, 10, 0),
        duration_minutes=15,
    )
    await make_travel_entry(db_session, home, site_a, 10)
    roster = await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    await make_roster_assignment(db_session, roster, worker, shift)
    await db_session.commit()

    fake_job = AsyncMock()
    fake_job.job_id = "chained-job-123"
    mock_redis = AsyncMock()
    mock_redis.enqueue_job.return_value = fake_job

    result = await handle_reoptimization_event_task(
        {"redis": mock_redis},
        {
            "event_type": "worker_sick",
            "shift_id": shift.id,
            "worker_id": worker.id,
        },
    )

    assert result["outcome"] == "escalated_to_tier1"
    mock_redis.enqueue_job.assert_awaited_once_with(
        "solve_roster_task", SHIFT_DATE.isoformat(), SHIFT_DATE.isoformat()
    )
    assert result["escalation_roster_job_id"] == "chained-job-123"


async def test_reoptimization_task_does_not_chain_on_scoped_resolve(db_session) -> None:
    home = await make_site(db_session, "HOME4", region="North")
    site_a = await make_site(db_session, "SITE-A4")
    worker = await make_worker(db_session, home, skills=["driver"])
    shift = await make_shift(
        db_session,
        home,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="driver",
        is_multi_stop=True,
    )
    job = await make_job(
        db_session,
        shift,
        site_a,
        window_start=datetime(2026, 3, 5, 9, 0),
        window_end=datetime(2026, 3, 5, 10, 0),
        duration_minutes=15,
    )
    await make_travel_entry(db_session, home, site_a, 10)
    roster = await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    await make_roster_assignment(db_session, roster, worker, shift)
    await db_session.commit()

    mock_redis = AsyncMock()

    result = await handle_reoptimization_event_task(
        {"redis": mock_redis},
        {
            "event_type": "job_cancelled",
            "shift_id": shift.id,
            "worker_id": worker.id,
            "cancelled_job_id": job.id,
        },
    )

    assert result["outcome"] == "scoped_resolve"
    mock_redis.enqueue_job.assert_not_awaited()
    assert "escalation_roster_job_id" not in result
