"""Read-only route/site-assignment lookup: `GET /routes/{roster_assignment_id}`.

Mirrors the mode split ``app/services/dispatch/service.py`` already uses:
a multi-stop shift's ``RosterAssignment`` resolves to a ``Route`` (+ ordered
``RouteStop`` rows), a single-site shift's resolves to a ``SiteAssignment``
instead. See ``app/schemas/routes.py`` for the response shapes and the note
on why ``return_to_home_time`` is recomputed here rather than read from a
persisted column.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_session
from app.models.job import Job
from app.models.roster import RosterAssignment
from app.models.route import Route, RouteStop
from app.models.site_assignment import SiteAssignment
from app.models.travel_matrix import TravelMatrixEntry
from app.models.worker import Worker
from app.schemas.common import SiteSummary
from app.schemas.routes import RouteOrSiteAssignment, RouteRead, RouteStopRead, SiteAssignmentRead

router = APIRouter(tags=["routes"])


async def _return_to_home_time(
    session: AsyncSession, route: Route, home_site_id: int
):
    """Recompute the closed-round-trip's return-to-home arrival.

    Mirrors ``app.services.dispatch.solver``'s own arithmetic (last stop's
    planned departure + the cached travel time back to home) -- see
    ``app/schemas/routes.py``'s module docstring for why this isn't read
    from a persisted column. Returns ``None`` when the route has no stops
    (a 0-job route never leaves home -- same convention the solver uses) or
    when the cached travel matrix is missing the required pair.
    """
    if not route.stops:
        return None
    last_stop = route.stops[-1]
    last_site_id = last_stop.job.site_id
    if last_site_id == home_site_id:
        return last_stop.planned_departure
    entry = (
        await session.execute(
            select(TravelMatrixEntry).where(
                TravelMatrixEntry.from_site_id == last_site_id,
                TravelMatrixEntry.to_site_id == home_site_id,
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        return None
    return last_stop.planned_departure + timedelta(minutes=entry.travel_minutes)


@router.get("/routes/{roster_assignment_id}", response_model=RouteOrSiteAssignment)
async def get_route(
    roster_assignment_id: int, session: AsyncSession = Depends(get_session)
) -> RouteOrSiteAssignment:
    """The `Route` (multi-stop) or `SiteAssignment` (single-site) for one
    `RosterAssignment`, whichever exists.

    404s when the `RosterAssignment` itself doesn't exist, or when it exists
    but has not yet been dispatched (no `Route`/`SiteAssignment` persisted
    -- i.e. `POST /dispatch/solve` hasn't run for it yet).
    """
    roster_assignment = (
        await session.execute(
            select(RosterAssignment).where(RosterAssignment.id == roster_assignment_id)
        )
    ).scalar_one_or_none()
    if roster_assignment is None:
        raise HTTPException(
            status_code=404, detail=f"RosterAssignment {roster_assignment_id} not found"
        )

    route = (
        await session.execute(
            select(Route)
            .options(
                selectinload(Route.stops)
                .selectinload(RouteStop.job)
                .selectinload(Job.site)
            )
            .where(Route.roster_assignment_id == roster_assignment_id)
        )
    ).scalar_one_or_none()
    if route is not None:
        worker = (
            await session.execute(select(Worker).where(Worker.id == roster_assignment.worker_id))
        ).scalar_one()
        return_to_home_time = await _return_to_home_time(session, route, worker.home_site_id)
        return RouteRead(
            roster_assignment_id=roster_assignment_id,
            route_id=route.id,
            status=route.status.value,
            stops=[
                RouteStopRead(
                    job_id=s.job_id,
                    site=SiteSummary.model_validate(s.job.site),
                    sequence_no=s.sequence_no,
                    planned_arrival=s.planned_arrival,
                    planned_departure=s.planned_departure,
                )
                for s in route.stops
            ],
            return_to_home_time=return_to_home_time,
        )

    site_assignment = (
        await session.execute(
            select(SiteAssignment)
            .options(selectinload(SiteAssignment.site))
            .where(SiteAssignment.roster_assignment_id == roster_assignment_id)
        )
    ).scalar_one_or_none()
    if site_assignment is not None:
        return SiteAssignmentRead(
            roster_assignment_id=roster_assignment_id,
            site_assignment_id=site_assignment.id,
            site=SiteSummary.model_validate(site_assignment.site),
            arrival=site_assignment.arrival,
            departure=site_assignment.departure,
        )

    raise HTTPException(
        status_code=404,
        detail=(
            f"RosterAssignment {roster_assignment_id} has not been dispatched yet "
            "(no Route or SiteAssignment) -- POST /dispatch/solve first"
        ),
    )
