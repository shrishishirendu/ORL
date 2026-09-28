"""Integration tests for ``app.services.rostering.service`` -- Tier 1's
DB-facing boundary -- against a real Postgres, bypassing the async task
queue entirely (``solve_and_persist_roster`` is called directly).

Covers: a feasible solve persisting a ``Roster`` + ``RosterAssignment``
rows, and an infeasible solve persisting a ``Roster`` alone with
``failure_reason`` populated and no assignment rows.
"""

from __future__ import annotations

from datetime import date, time

import pytest
from sqlalchemy import select

from app.models.enums import RosterStatus
from app.models.roster import Roster, RosterAssignment
from app.services.rostering.service import solve_and_persist_roster
from tests.integration.factories import (
    make_award_row,
    make_shift,
    make_site,
    make_travel_entry,
    make_worker,
)

pytestmark = pytest.mark.asyncio

SHIFT_DATE = date(2026, 3, 2)


async def test_feasible_solve_persists_roster_and_assignments(db_session) -> None:
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
    await make_award_row(db_session, worker, shift, pay_cost=120.50)
    await db_session.commit()

    outcome = await solve_and_persist_roster(db_session, SHIFT_DATE, SHIFT_DATE)

    assert outcome.status is RosterStatus.SOLVED
    assert outcome.total_cost == pytest.approx(120.50)
    assert outcome.failure_reason is None
    assert outcome.unfilled_shift_ids == []

    roster = (
        await db_session.execute(select(Roster).where(Roster.id == outcome.roster_id))
    ).scalar_one()
    assert roster.status is RosterStatus.SOLVED
    assert float(roster.total_cost) == pytest.approx(120.50)

    assignments = (
        (
            await db_session.execute(
                select(RosterAssignment).where(RosterAssignment.roster_id == roster.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(assignments) == 1
    assert assignments[0].worker_id == worker.id
    assert assignments[0].shift_id == shift.id
    assert assignments[0].day == SHIFT_DATE


async def test_infeasible_solve_persists_failure_reason_and_no_assignments(db_session) -> None:
    home = await make_site(db_session, "HOME2", region="North")
    # No worker has the "nurse" skill and there is no AwardCostMatrix row at
    # all -- every shift is unfillable from the first eligibility pass.
    await make_worker(db_session, home, skills=["driver"])
    shift = await make_shift(
        db_session,
        home,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="nurse",
    )
    await db_session.commit()

    outcome = await solve_and_persist_roster(db_session, SHIFT_DATE, SHIFT_DATE)

    assert outcome.status is RosterStatus.FAILED
    assert outcome.total_cost is None
    assert outcome.failure_reason is not None
    assert str(shift.id) in outcome.failure_reason
    assert outcome.unfilled_shift_ids == [shift.id]

    roster = (
        await db_session.execute(select(Roster).where(Roster.id == outcome.roster_id))
    ).scalar_one()
    assert roster.status is RosterStatus.FAILED
    assert roster.failure_reason == outcome.failure_reason
    assert roster.total_cost is None

    assignments = (
        (
            await db_session.execute(
                select(RosterAssignment).where(RosterAssignment.roster_id == roster.id)
            )
        )
        .scalars()
        .all()
    )
    assert assignments == []


async def test_excluded_workers_are_not_rostered(db_session) -> None:
    """A worker excluded from the solve (e.g. reported sick) is never a
    candidate, even when they are the cheapest option."""
    home = await make_site(db_session, "HOME3", region="North")
    sick = await make_worker(db_session, home, name="Sick", skills=["driver"])
    cover = await make_worker(db_session, home, name="Cover", skills=["driver"])
    shift = await make_shift(
        db_session,
        home,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="driver",
    )
    await make_award_row(db_session, sick, shift, pay_cost=100.00)
    await make_award_row(db_session, cover, shift, pay_cost=150.00)
    await db_session.commit()

    baseline = await solve_and_persist_roster(db_session, SHIFT_DATE, SHIFT_DATE)
    excluded = await solve_and_persist_roster(
        db_session, SHIFT_DATE, SHIFT_DATE, excluded_worker_ids=frozenset({sick.id})
    )

    assert baseline.total_cost == pytest.approx(100.00)
    assert excluded.status is RosterStatus.SOLVED
    assert excluded.total_cost == pytest.approx(150.00)
    workers = (
        (
            await db_session.execute(
                select(RosterAssignment.worker_id).where(
                    RosterAssignment.roster_id == excluded.roster_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert workers == [cover.id]


async def test_travel_time_between_sites_is_loaded_and_enforced(db_session) -> None:
    """Two shifts 30 minutes apart at sites 45 minutes' travel apart. The
    cheap worker can't do both, so the solve splits them (100 + 150), using
    the travel matrix loaded from the database."""
    site_a = await make_site(db_session, "TRAVEL-A", region="North")
    site_b = await make_site(db_session, "TRAVEL-B", region="North")
    cheap = await make_worker(db_session, site_a, name="Cheap", skills=["driver"])
    other = await make_worker(db_session, site_a, name="Other", skills=["driver"])
    morning = await make_shift(
        db_session,
        site_a,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(12, 0),
        required_skill="driver",
    )
    afternoon = await make_shift(
        db_session,
        site_b,
        date=SHIFT_DATE,
        start_time=time(12, 30),
        end_time=time(16, 30),
        required_skill="driver",
    )
    for shift in (morning, afternoon):
        await make_award_row(db_session, cheap, shift, pay_cost=50.00)
        await make_award_row(db_session, other, shift, pay_cost=100.00)
    await make_travel_entry(db_session, site_a, site_b, 45)
    await db_session.commit()

    outcome = await solve_and_persist_roster(db_session, SHIFT_DATE, SHIFT_DATE)

    assert outcome.status is RosterStatus.SOLVED
    assert outcome.total_cost == pytest.approx(150.00)
    assert outcome.warnings == []
