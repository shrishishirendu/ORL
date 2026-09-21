"""Integration tests for ``app.services.reoptimization.service`` -- Tier 3's
DB-facing boundary -- against a real Postgres, bypassing the async task
queue (``handle_and_persist_event`` is called directly).

Covers: a ``job_cancelled`` scoped resolve that creates a ``Route`` (+ its
``RouteStop`` rows) for a ``RosterAssignment`` that didn't have one yet, and
a ``worker_sick`` event that always escalates -- persisting a
``ReoptimizationEvent`` row with ``resolution = escalated_to_tier1`` and
touching no ``Route`` row at all.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import select

from app.models.enums import ReoptimizationEventType, ReoptimizationResolution, ReoptimizationStatus
from app.models.reoptimization import ReoptimizationEvent
from app.models.route import Route, RouteStop
from app.services.reoptimization.service import (
    IncomingReoptimizationEvent,
    handle_and_persist_event,
)
from app.services.reoptimization.solver import OutcomeType
from tests.integration.factories import (
    make_job,
    make_roster,
    make_roster_assignment,
    make_shift,
    make_site,
    make_travel_entry,
    make_worker,
)

pytestmark = pytest.mark.asyncio

SHIFT_DATE = date(2026, 3, 4)


async def _seed_multi_stop_shift(db_session):
    home = await make_site(db_session, "HOME", region="North")
    site_a = await make_site(db_session, "SITE-A")
    site_b = await make_site(db_session, "SITE-B")
    worker = await make_worker(db_session, home, skills=["driver"])
    shift = await make_shift(
        db_session,
        home,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(18, 0),
        required_skill="driver",
        is_multi_stop=True,
    )
    job1 = await make_job(
        db_session,
        shift,
        site_a,
        window_start=datetime(2026, 3, 4, 9, 0),
        window_end=datetime(2026, 3, 4, 10, 0),
        duration_minutes=15,
    )
    job2 = await make_job(
        db_session,
        shift,
        site_b,
        window_start=datetime(2026, 3, 4, 10, 30),
        window_end=datetime(2026, 3, 4, 11, 30),
        duration_minutes=20,
    )
    await make_travel_entry(db_session, home, site_a, 10)
    await make_travel_entry(db_session, home, site_b, 20)
    await make_travel_entry(db_session, site_a, site_b, 15)
    roster = await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    roster_assignment = await make_roster_assignment(db_session, roster, worker, shift)
    await db_session.commit()
    return worker, shift, job1, job2, roster_assignment


async def test_job_cancelled_scoped_resolve_creates_route_and_stop(db_session) -> None:
    worker, shift, job1, job2, roster_assignment = await _seed_multi_stop_shift(db_session)

    event = IncomingReoptimizationEvent(
        event_type=ReoptimizationEventType.JOB_CANCELLED,
        shift_id=shift.id,
        worker_id=worker.id,
        cancelled_job_id=job2.id,
    )
    outcome = await handle_and_persist_event(db_session, event)

    assert outcome.outcome is OutcomeType.SCOPED_RESOLVE
    assert outcome.route_id is not None
    assert outcome.shift_date == SHIFT_DATE
    assert outcome.escalation_reason is None

    event_row = (
        await db_session.execute(
            select(ReoptimizationEvent).where(ReoptimizationEvent.id == outcome.event_id)
        )
    ).scalar_one()
    assert event_row.event_type is ReoptimizationEventType.JOB_CANCELLED
    assert event_row.resolution is ReoptimizationResolution.SCOPED_RESOLVE
    assert event_row.status is ReoptimizationStatus.RESOLVED
    assert event_row.route_id == outcome.route_id

    route = (
        await db_session.execute(select(Route).where(Route.id == outcome.route_id))
    ).scalar_one()
    assert route.roster_assignment_id == roster_assignment.id

    stops = (
        (await db_session.execute(select(RouteStop).where(RouteStop.route_id == route.id)))
        .scalars()
        .all()
    )
    # job2 was cancelled -- only job1 remains on the re-solved route.
    assert [s.job_id for s in stops] == [job1.id]


async def test_worker_sick_always_escalates_and_touches_no_route(db_session) -> None:
    worker, shift, _job1, _job2, _roster_assignment = await _seed_multi_stop_shift(db_session)

    event = IncomingReoptimizationEvent(
        event_type=ReoptimizationEventType.WORKER_SICK,
        shift_id=shift.id,
        worker_id=worker.id,
    )
    outcome = await handle_and_persist_event(db_session, event)

    assert outcome.outcome is OutcomeType.ESCALATED_TO_TIER1
    assert outcome.escalation_reason is not None
    assert outcome.shift_date == SHIFT_DATE

    event_row = (
        await db_session.execute(
            select(ReoptimizationEvent).where(ReoptimizationEvent.id == outcome.event_id)
        )
    ).scalar_one()
    assert event_row.resolution is ReoptimizationResolution.ESCALATED_TO_TIER1
    assert event_row.status is ReoptimizationStatus.ESCALATED

    # No Route was ever created for this shift's RosterAssignment -- worker
    # escalation never touches Tier 2 at all.
    routes = (await db_session.execute(select(Route))).scalars().all()
    assert routes == []
