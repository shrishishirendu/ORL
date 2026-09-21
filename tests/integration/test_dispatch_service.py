"""Integration tests for ``app.services.dispatch.service`` -- Tier 2's
DB-facing boundary -- against a real Postgres, bypassing the async task
queue (``solve_and_persist_route`` is called directly).

Covers all three outcomes the task description calls out:

- a single-site ``RosterAssignment`` bypasses Tier 2 entirely and gets a
  ``SiteAssignment`` row instead of a ``Route``.
- a feasible multi-stop solve persists a ``Route`` (status solved) + its
  ``RouteStop`` rows.
- an infeasible multi-stop solve persists a ``Route`` (status failed) with
  no stops.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest
from sqlalchemy import select

from app.models.enums import RouteStatus
from app.models.route import Route, RouteStop
from app.models.site_assignment import SiteAssignment
from app.services.dispatch.service import solve_and_persist_route
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

SHIFT_DATE = date(2026, 3, 3)


async def _seed_roster_assignment(db_session, shift, worker):
    roster = await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    return await make_roster_assignment(db_session, roster, worker, shift)


async def test_single_site_shift_bypasses_tier2_and_writes_site_assignment(db_session) -> None:
    home = await make_site(db_session, "HOME", region="North")
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
    roster_assignment = await _seed_roster_assignment(db_session, shift, worker)
    await db_session.commit()

    outcome = await solve_and_persist_route(db_session, roster_assignment.id)

    assert outcome.mode == "site_assignment"
    assert outcome.site_assignment_id is not None
    assert outcome.route_id is None

    site_assignment = (
        await db_session.execute(
            select(SiteAssignment).where(
                SiteAssignment.roster_assignment_id == roster_assignment.id
            )
        )
    ).scalar_one()
    assert site_assignment.site_id == job_site.id
    assert site_assignment.arrival == datetime(2026, 3, 3, 8, 0, tzinfo=UTC)
    assert site_assignment.departure == datetime(2026, 3, 3, 16, 0, tzinfo=UTC)

    # No Route was ever created for this RosterAssignment.
    route = (
        await db_session.execute(
            select(Route).where(Route.roster_assignment_id == roster_assignment.id)
        )
    ).scalar_one_or_none()
    assert route is None


async def test_feasible_multi_stop_shift_persists_route_and_stops(db_session) -> None:
    home = await make_site(db_session, "HOME2", region="North")
    site_a = await make_site(db_session, "SITE-A2")
    site_b = await make_site(db_session, "SITE-B2")
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
        window_start=datetime(2026, 3, 3, 9, 0),
        window_end=datetime(2026, 3, 3, 10, 0),
        duration_minutes=15,
    )
    job2 = await make_job(
        db_session,
        shift,
        site_b,
        window_start=datetime(2026, 3, 3, 10, 30),
        window_end=datetime(2026, 3, 3, 11, 30),
        duration_minutes=20,
    )
    await make_travel_entry(db_session, home, site_a, 10)
    await make_travel_entry(db_session, home, site_b, 20)
    await make_travel_entry(db_session, site_a, site_b, 15)
    roster_assignment = await _seed_roster_assignment(db_session, shift, worker)
    await db_session.commit()

    outcome = await solve_and_persist_route(db_session, roster_assignment.id)

    assert outcome.mode == "routed"
    assert outcome.status == RouteStatus.SOLVED.value
    assert outcome.stops_count == 2
    assert outcome.route_id is not None

    route = (
        await db_session.execute(select(Route).where(Route.id == outcome.route_id))
    ).scalar_one()
    assert route.status is RouteStatus.SOLVED

    stops = (
        (await db_session.execute(select(RouteStop).where(RouteStop.route_id == route.id)))
        .scalars()
        .all()
    )
    assert {s.job_id for s in stops} == {job1.id, job2.id}
    assert sorted(s.sequence_no for s in stops) == [1, 2]


async def test_infeasible_multi_stop_shift_persists_failed_route_with_no_stops(
    db_session,
) -> None:
    home = await make_site(db_session, "HOME3", region="North")
    site_a = await make_site(db_session, "SITE-A3")
    site_b = await make_site(db_session, "SITE-B3")
    worker = await make_worker(db_session, home, skills=["driver"])
    shift = await make_shift(
        db_session,
        home,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(9, 0),
        required_skill="driver",
        is_multi_stop=True,
    )
    # Two jobs with overlapping, mutually-unreachable windows given travel
    # time -- no sequence can satisfy both.
    await make_job(
        db_session,
        shift,
        site_a,
        window_start=datetime(2026, 3, 3, 9, 0),
        window_end=datetime(2026, 3, 3, 9, 5),
        duration_minutes=30,
    )
    await make_job(
        db_session,
        shift,
        site_b,
        window_start=datetime(2026, 3, 3, 9, 0),
        window_end=datetime(2026, 3, 3, 9, 5),
        duration_minutes=30,
    )
    await make_travel_entry(db_session, home, site_a, 60)
    await make_travel_entry(db_session, home, site_b, 60)
    await make_travel_entry(db_session, site_a, site_b, 60)
    roster_assignment = await _seed_roster_assignment(db_session, shift, worker)
    await db_session.commit()

    outcome = await solve_and_persist_route(db_session, roster_assignment.id)

    assert outcome.mode == "routed"
    assert outcome.status == RouteStatus.FAILED.value
    assert outcome.stops_count == 0
    assert outcome.infeasible_job_ids != []

    route = (
        await db_session.execute(select(Route).where(Route.id == outcome.route_id))
    ).scalar_one()
    assert route.status is RouteStatus.FAILED

    stops = (
        (await db_session.execute(select(RouteStop).where(RouteStop.route_id == route.id)))
        .scalars()
        .all()
    )
    assert stops == []
