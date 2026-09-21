"""DB-facing boundary for Tier 2 dispatch/routing.

Given a ``RosterAssignment`` (one worker's shift, per ARCHITECTURE.md's Tier
1 output), this module loads that shift's ``Job`` rows, the worker's
``home_site_id``, and the cached ``TravelMatrixEntry`` rows covering the
relevant sites, and either:

- calls ``app.services.dispatch.solver.solve_shift_route`` and persists a
  ``Route`` (+ ``RouteStop`` rows on a feasible solve), for a multi-stop
  shift (``Shift.is_multi_stop is True``), or
- bypasses Tier 2 entirely and writes a ``SiteAssignment`` directly, for a
  single-site shift (``Shift.is_multi_stop is False``) -- per
  ARCHITECTURE.md's Tier 2 section: "Single-site roles skip this tier
  entirely via a simple 'Site Assignment' path."

``load_travel_matrix``, ``as_utc`` and ``replace_route_stops`` are exported
(not module-private) because Tier 3's re-optimization service
(``app/services/reoptimization/service.py``) needs the exact same
DB-loading/route-rewriting logic for its scoped re-solves -- both tiers
persist "a fresh set of ``RouteStop`` rows for this ``Route``" the same way,
per ``Route``'s own docstring ("one row, re-solved in place").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import RouteStatus
from app.models.roster import RosterAssignment
from app.models.route import Route, RouteStop
from app.models.shift import Shift
from app.models.site_assignment import SiteAssignment
from app.models.travel_matrix import TravelMatrixEntry
from app.models.worker import Worker
from app.services.dispatch.solver import (
    JobSpec,
    RouteSolveResult,
    RouteStopPlan,
    TravelTimeMatrix,
    solve_shift_route,
)


@dataclass
class DispatchOutcome:
    """What the API layer needs to report back after a Tier 2 solve/bypass.

    ``mode`` is one of ``"site_assignment"`` (the single-site bypass path),
    ``"routed"`` (a real VRPTW solve was attempted, feasible or not).
    ``status`` mirrors ``Route.status`` and is only set for ``"routed"``.
    """

    roster_assignment_id: int
    mode: str
    route_id: int | None = None
    site_assignment_id: int | None = None
    status: str | None = None
    stops_count: int = 0
    infeasible_job_ids: list[int] = field(default_factory=list)
    reason: str | None = None


def as_utc(dt: datetime) -> datetime:
    """Normalize a possibly-naive datetime to UTC-aware.

    ``Job.window_start``/``window_end`` (see ``app/models/job.py``) are
    plain ``DateTime`` columns (no explicit timezone), whereas
    ``Route``/``RouteStop``/``SiteAssignment`` columns are
    ``DateTime(timezone=True)``. Rather than silently letting a naive
    datetime hit a tz-aware column (asyncpg is strict about this), every
    value this service reads from ``Job`` or writes back to a tz-aware
    column is normalized here. Not specified anywhere in ARCHITECTURE.md or
    the schema -- flagging this as a decision: naive datetimes are assumed
    to already be UTC (the only timezone this codebase uses), not the
    server's local time.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def load_travel_matrix(session: AsyncSession, site_ids: set[int]) -> TravelTimeMatrix:
    """Query cached ``TravelMatrixEntry`` rows covering every pair within
    ``site_ids`` and wrap them in the solver's in-memory ``TravelTimeMatrix``.

    A single-site set (0 or 1 ids) needs no travel data at all -- same-site
    lookups resolve to 0 without a cached entry (see
    ``TravelTimeMatrix.get``) -- so this short-circuits without a query.
    """
    if len(site_ids) < 2:
        return TravelTimeMatrix({})
    rows = (
        await session.execute(
            select(TravelMatrixEntry).where(
                TravelMatrixEntry.from_site_id.in_(site_ids),
                TravelMatrixEntry.to_site_id.in_(site_ids),
            )
        )
    ).scalars().all()
    return TravelTimeMatrix({(r.from_site_id, r.to_site_id): r.travel_minutes for r in rows})


async def replace_route_stops(
    session: AsyncSession, route: Route, stops: list[RouteStopPlan]
) -> None:
    """Replace every ``RouteStop`` row for ``route`` with ``stops``.

    Used both for a fresh Tier 2 solve and for a Tier 3 scoped re-solve of
    an existing ``Route`` (see ``Route``'s docstring: re-solves happen "in
    place", not as a new ``Route`` row). The old rows are deleted with an
    explicit ``DELETE`` (and flushed) before the new ones are added, rather
    than relying on ORM collection-replacement cascade ordering, so this
    never risks a transient unique-constraint clash on
    ``(route_id, sequence_no)``/``(route_id, job_id)`` within the same flush.
    """
    await session.execute(delete(RouteStop).where(RouteStop.route_id == route.id))
    await session.flush()
    for stop in stops:
        session.add(
            RouteStop(
                route_id=route.id,
                job_id=stop.job_id,
                sequence_no=stop.sequence_no,
                planned_arrival=as_utc(stop.planned_arrival),
                planned_departure=as_utc(stop.planned_departure),
            )
        )


async def solve_and_persist_route(
    session: AsyncSession, roster_assignment_id: int
) -> DispatchOutcome:
    """Load Tier 2 inputs for `roster_assignment_id`, solve/bypass, persist.

    Raises ``ValueError`` if no such ``RosterAssignment`` exists -- a
    caller data problem, not a routing outcome.
    """
    roster_assignment = (
        await session.execute(
            select(RosterAssignment)
            .options(
                selectinload(RosterAssignment.shift).selectinload(Shift.jobs),
                selectinload(RosterAssignment.worker),
            )
            .where(RosterAssignment.id == roster_assignment_id)
        )
    ).scalar_one_or_none()
    if roster_assignment is None:
        raise ValueError(f"RosterAssignment {roster_assignment_id} not found")

    shift = roster_assignment.shift
    worker = roster_assignment.worker

    if not shift.is_multi_stop:
        return await _persist_site_assignment(session, roster_assignment, shift)
    return await _persist_routed(session, roster_assignment, shift, worker)


async def _persist_site_assignment(
    session: AsyncSession, roster_assignment: RosterAssignment, shift: Shift
) -> DispatchOutcome:
    """The Tier 2 bypass path for a single-site shift: write a
    ``SiteAssignment`` directly, no VRPTW solve at all.

    ``arrival``/``departure`` are derived from the shift's own
    ``date``/``start_time``/``end_time`` -- the worker is at the shift's one
    site for its whole duration, so there is nothing else to derive them
    from. Handles an overnight shift (``end_time <= start_time``) the same
    way the rostering solver does (see
    ``app/services/rostering/solver.py``'s module docstring point 3):
    treated as ending the following day.
    """
    arrival = as_utc(datetime.combine(shift.date, shift.start_time))
    departure = as_utc(datetime.combine(shift.date, shift.end_time))
    if departure <= arrival:
        departure += timedelta(days=1)

    site_assignment = (
        await session.execute(
            select(SiteAssignment).where(
                SiteAssignment.roster_assignment_id == roster_assignment.id
            )
        )
    ).scalar_one_or_none()
    if site_assignment is None:
        site_assignment = SiteAssignment(
            roster_assignment_id=roster_assignment.id, site_id=shift.site_id
        )
        session.add(site_assignment)
    site_assignment.site_id = shift.site_id
    site_assignment.arrival = arrival
    site_assignment.departure = departure

    await session.flush()
    await session.commit()

    return DispatchOutcome(
        roster_assignment_id=roster_assignment.id,
        mode="site_assignment",
        site_assignment_id=site_assignment.id,
    )


async def _persist_routed(
    session: AsyncSession,
    roster_assignment: RosterAssignment,
    shift: Shift,
    worker: Worker,
) -> DispatchOutcome:
    """The real Tier 2 path for a multi-stop shift: solve and persist a
    ``Route`` (+ ``RouteStop`` rows on success).
    """
    jobs = [
        JobSpec(
            job_id=j.id,
            site_id=j.site_id,
            window_start=as_utc(j.window_start),
            window_end=as_utc(j.window_end),
            duration_minutes=j.duration_minutes,
        )
        for j in shift.jobs
    ]
    site_ids = {worker.home_site_id} | {j.site_id for j in jobs}
    travel_matrix = await load_travel_matrix(session, site_ids)
    shift_start = as_utc(datetime.combine(shift.date, shift.start_time))

    result: RouteSolveResult = solve_shift_route(
        jobs,
        travel_matrix,
        home_site_id=worker.home_site_id,
        shift_start=shift_start,
    )

    route = (
        await session.execute(
            select(Route).where(Route.roster_assignment_id == roster_assignment.id)
        )
    ).scalar_one_or_none()
    is_new_route = route is None
    if is_new_route:
        route = Route(roster_assignment_id=roster_assignment.id)

    # Set every NOT NULL column before the first flush of a brand-new Route
    # -- flushing a bare `Route(roster_assignment_id=...)` first (to obtain
    # its id) would insert a row with `generated_at` still unset.
    route.generated_at = datetime.now(UTC)
    route.status = RouteStatus.SOLVED if result.feasible else RouteStatus.FAILED

    if is_new_route:
        session.add(route)
        await session.flush()

    if result.feasible:
        await replace_route_stops(session, route, result.stops)
    else:
        # An infeasible solve leaves the route with no stops -- see the
        # task description: "just a Route with status reflecting
        # infeasibility, no stops, on failure".
        await replace_route_stops(session, route, [])

    await session.commit()

    return DispatchOutcome(
        roster_assignment_id=roster_assignment.id,
        mode="routed",
        route_id=route.id,
        status=route.status.value,
        stops_count=len(result.stops) if result.feasible else 0,
        infeasible_job_ids=list(result.infeasible_job_ids),
        reason=result.reason,
    )
