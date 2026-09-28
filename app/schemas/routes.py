"""Response schemas for `GET /routes/{roster_assignment_id}`.

Mirrors the mode split ``app/services/dispatch/service.py``'s
``DispatchOutcome`` already uses: a multi-stop shift resolves to a ``Route``
(+ ordered ``RouteStop`` rows), a single-site shift resolves to a
``SiteAssignment`` instead. ``RouteOrSiteAssignment`` is a discriminated
union on ``mode`` so the one endpoint can return either shape with a schema
FastAPI can validate/document.

**Return-to-home time.** ``app.services.dispatch.solver.RouteSolveResult``
computes a ``return_to_home_arrival`` at solve time (see that module's
docstring), but nothing persists it: ``Route``/``RouteStop`` have no column
for it (see ``app/models/route.py``). Rather than adding a migration for a
column the task brief's "Schema additions" list didn't name, this endpoint
**recomputes** it from already-persisted data -- the last stop's
``planned_departure`` plus the cached ``TravelMatrixEntry`` from that stop's
site back to the worker's ``home_site_id`` -- the same arithmetic the solver
itself uses. FLAG FOR REVIEW: this is a decision the brief didn't pin down;
persisting the value on ``Route`` instead (via a migration) is an equally
defensible alternative if a caller needs it without a live travel-matrix
lookup.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import SiteSummary


class RouteStopRead(BaseModel):
    """One ordered `RouteStop`, with its job's site resolved."""

    model_config = ConfigDict(from_attributes=True)

    job_id: int
    site: SiteSummary
    sequence_no: int
    planned_arrival: datetime
    planned_departure: datetime


class RouteRead(BaseModel):
    """The Tier 2 VRPTW path: a `Route` plus its ordered stops."""

    mode: Literal["routed"] = "routed"
    roster_assignment_id: int
    route_id: int
    status: str
    stops: list[RouteStopRead]
    return_to_home_time: datetime | None = None


class SiteAssignmentRead(BaseModel):
    """The single-site bypass path: a `SiteAssignment`, no routing."""

    mode: Literal["site_assignment"] = "site_assignment"
    roster_assignment_id: int
    site_assignment_id: int
    site: SiteSummary
    arrival: datetime
    departure: datetime


RouteOrSiteAssignment = Annotated[RouteRead | SiteAssignmentRead, Field(discriminator="mode")]
