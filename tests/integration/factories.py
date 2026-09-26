"""Tiny row-building helpers shared by the integration tests.

Each function ``add``s and ``flush``es (never commits -- the calling test
commits once its whole fixture scenario is built) one row, returning it with
its id populated.
"""

from __future__ import annotations

from datetime import date as date_
from datetime import datetime, time

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.award_cost_matrix import AwardCostMatrix
from app.models.enums import (
    EmploymentType,
    ReoptimizationEventType,
    ReoptimizationResolution,
    ReoptimizationStatus,
    RosterStatus,
)
from app.models.job import Job
from app.models.reoptimization import ReoptimizationEvent
from app.models.roster import Roster, RosterAssignment
from app.models.shift import Shift
from app.models.site import Site
from app.models.travel_matrix import TravelMatrixEntry
from app.models.worker import Worker


async def make_site(
    session: AsyncSession, code: str, name: str | None = None, region: str | None = None
) -> Site:
    site = Site(code=code, name=name or code, region=region)
    session.add(site)
    await session.flush()
    return site


async def make_worker(
    session: AsyncSession,
    home_site: Site,
    *,
    name: str = "Worker",
    skills: list[str] | None = None,
    region: str | None = None,
    active: bool = True,
    award_code: str | None = None,
    classification_level: str | None = None,
    employment_type: EmploymentType | None = None,
) -> Worker:
    worker = Worker(
        name=name,
        skills=list(skills or []),
        region=region,
        home_site_id=home_site.id,
        active=active,
        award_code=award_code,
        classification_level=classification_level,
        employment_type=employment_type,
    )
    session.add(worker)
    await session.flush()
    return worker


async def make_shift(
    session: AsyncSession,
    site: Site,
    *,
    date: date_,
    start_time: time,
    end_time: time,
    required_skill: str,
    is_multi_stop: bool = False,
) -> Shift:
    shift = Shift(
        date=date,
        start_time=start_time,
        end_time=end_time,
        required_skill=required_skill,
        site_id=site.id,
        is_multi_stop=is_multi_stop,
    )
    session.add(shift)
    await session.flush()
    return shift


async def make_job(
    session: AsyncSession,
    shift: Shift,
    site: Site,
    *,
    window_start: datetime,
    window_end: datetime,
    duration_minutes: int,
) -> Job:
    job = Job(
        shift_id=shift.id,
        site_id=site.id,
        window_start=window_start,
        window_end=window_end,
        duration_minutes=duration_minutes,
    )
    session.add(job)
    await session.flush()
    return job


async def make_award_row(
    session: AsyncSession,
    worker: Worker,
    shift: Shift,
    *,
    pay_cost: float,
    eligible: bool = True,
    min_hours: float | None = None,
    max_hours: float | None = None,
    is_placeholder: bool = False,
) -> AwardCostMatrix:
    row = AwardCostMatrix(
        worker_id=worker.id,
        day=shift.date,
        shift_id=shift.id,
        pay_cost=pay_cost,
        eligible=eligible,
        min_hours=min_hours,
        max_hours=max_hours,
        is_placeholder=is_placeholder,
    )
    session.add(row)
    await session.flush()
    return row


async def make_travel_entry(
    session: AsyncSession, from_site: Site, to_site: Site, minutes: int, *, symmetric: bool = True
) -> None:
    session.add(
        TravelMatrixEntry(from_site_id=from_site.id, to_site_id=to_site.id, travel_minutes=minutes)
    )
    if symmetric:
        session.add(
            TravelMatrixEntry(
                from_site_id=to_site.id, to_site_id=from_site.id, travel_minutes=minutes
            )
        )
    await session.flush()


async def make_roster(
    session: AsyncSession,
    *,
    period_start: date_,
    period_end: date_,
    status: RosterStatus = RosterStatus.SOLVED,
) -> Roster:
    roster = Roster(
        period_start=period_start,
        period_end=period_end,
        generated_at=datetime.now(),
        status=status,
    )
    session.add(roster)
    await session.flush()
    return roster


async def make_roster_assignment(
    session: AsyncSession, roster: Roster, worker: Worker, shift: Shift
) -> RosterAssignment:
    assignment = RosterAssignment(
        roster_id=roster.id, worker_id=worker.id, day=shift.date, shift_id=shift.id
    )
    session.add(assignment)
    await session.flush()
    return assignment


async def make_reoptimization_event(
    session: AsyncSession,
    *,
    event_type: ReoptimizationEventType,
    occurred_at: datetime,
    shift_id: int | None = None,
    worker_id: int | None = None,
    route_id: int | None = None,
    resolution: ReoptimizationResolution | None = None,
    resolved_at: datetime | None = None,
    status: ReoptimizationStatus = ReoptimizationStatus.OPEN,
) -> ReoptimizationEvent:
    event = ReoptimizationEvent(
        event_type=event_type,
        occurred_at=occurred_at,
        shift_id=shift_id,
        worker_id=worker_id,
        route_id=route_id,
        resolution=resolution,
        resolved_at=resolved_at,
        status=status,
    )
    session.add(event)
    await session.flush()
    return event
