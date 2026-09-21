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
from tests.integration.factories import make_award_row, make_shift, make_site, make_worker

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
