"""DB-facing boundary for Tier 3 event-driven re-optimization.

Given an incoming event (type + shift/worker context + payload), this
module loads the current route/remaining-jobs state from the DB, calls
``app.services.reoptimization.solver.handle_event``, and persists the
outcome:

- a ``ReoptimizationEvent`` row is **always** created (``event_type``,
  ``occurred_at``, ``resolution``, ``status`` per the enums in
  ``app/models/enums.py``).
- on a scoped resolve, the affected ``Route`` (+ its ``RouteStop`` rows) is
  updated to the new plan, reusing
  ``app.services.dispatch.service.replace_route_stops`` -- the exact same
  "re-solved in place" persistence Tier 2's own solve uses (see that
  module's docstring).
- on escalation, only the event row is written (``resolution =
  ESCALATED_TO_TIER1``, ``status = ESCALATED``); no ``Route``/``Roster`` row
  is touched here.

**Deliberately not done here**: this module never calls
``app.services.rostering.service.solve_and_persist_roster`` (or
``solve_roster`` directly) on escalation. Per ARCHITECTURE.md's Tier
3 section and ``app/services/reoptimization/solver.py``'s module docstring,
escalating "up to Tier 1" means handing a rostering re-solve to the async
task queue as its own job, not invoking Tier 1 synchronously from inside an
event handler -- that boundary is what keeps a slow/queued batch solve from
ever running inline on a "seconds"-tempo Tier 3 request. The job-chaining
(escalation -> enqueue ``solve_roster_task``) happens one layer up, in
``app/workers/tasks.py``'s ``handle_reoptimization_event_task``, which is
why ``ReoptimizationOutcome`` below carries ``shift_date`` -- the period a
chained Tier 1 re-solve needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import date as date_

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import (
    ReoptimizationEventType,
    ReoptimizationResolution,
    ReoptimizationStatus,
    RouteStatus,
)
from app.models.reoptimization import ReoptimizationEvent as ReoptimizationEventRow
from app.models.roster import RosterAssignment
from app.models.route import Route
from app.models.shift import Shift
from app.models.worker import Worker
from app.services.dispatch.service import as_utc, load_travel_matrix, replace_route_stops
from app.services.dispatch.solver import JobSpec
from app.services.reoptimization.solver import (
    EventType,
    OutcomeType,
    handle_event,
)
from app.services.reoptimization.solver import (
    ReoptimizationEvent as SolverEvent,
)

_EVENT_TYPE_TO_SOLVER: dict[ReoptimizationEventType, EventType] = {
    ReoptimizationEventType.JOB_CANCELLED: EventType.JOB_CANCELLED,
    ReoptimizationEventType.WORKER_SICK: EventType.WORKER_SICK,
    ReoptimizationEventType.VISIT_OVERRAN: EventType.VISIT_OVERRAN,
}
_OUTCOME_TO_RESOLUTION: dict[OutcomeType, ReoptimizationResolution] = {
    OutcomeType.SCOPED_RESOLVE: ReoptimizationResolution.SCOPED_RESOLVE,
    OutcomeType.ESCALATED_TO_TIER1: ReoptimizationResolution.ESCALATED_TO_TIER1,
}
_OUTCOME_TO_STATUS: dict[OutcomeType, ReoptimizationStatus] = {
    OutcomeType.SCOPED_RESOLVE: ReoptimizationStatus.RESOLVED,
    OutcomeType.ESCALATED_TO_TIER1: ReoptimizationStatus.ESCALATED,
}


@dataclass
class IncomingReoptimizationEvent:
    """The event shape the API/task layer builds from an inbound request.

    ``completed_job_ids`` -- jobs on this shift already visited before this
    event fired -- has no home in the schema: ``RouteStop`` has no
    "actual"/"completed" tracking column (see ``app/models/route.py``), only
    a *planned* arrival/departure. Rather than inventing a persisted
    completion flag out of scope for this task, the caller supplies it
    explicitly; "remaining jobs" is then computed as the shift's ``Job``
    rows minus this set. The cancelled job (for ``job_cancelled``) is
    deliberately *not* excluded here even if also listed as completed --
    the Tier 3 solver's own contract requires it present in
    ``remaining_jobs`` (it removes it itself). FLAG FOR REVIEW: this is a
    schema-shape decision the task brief did not pin down.
    """

    event_type: ReoptimizationEventType
    shift_id: int
    worker_id: int
    occurred_at: datetime | None = None
    cancelled_job_id: int | None = None
    current_site_id: int | None = None
    current_time: datetime | None = None
    planned_time: datetime | None = None
    completed_job_ids: list[int] = field(default_factory=list)


@dataclass
class ReoptimizationOutcome:
    """What the API/task layer needs to report back -- and, on escalation,
    what ``app/workers/tasks.py`` needs to chain a Tier 1 job.
    """

    event_id: int
    event_type: ReoptimizationEventType
    outcome: OutcomeType
    shift_id: int
    worker_id: int
    shift_date: date_
    route_id: int | None = None
    escalation_reason: str | None = None
    detail: str | None = None


async def handle_and_persist_event(
    session: AsyncSession, event: IncomingReoptimizationEvent
) -> ReoptimizationOutcome:
    """Load Tier 3 inputs for `event`, call `handle_event`, persist the result.

    Raises ``ValueError`` when ``shift_id``/``worker_id`` don't resolve to
    real rows, or (mirroring ``handle_event`` itself) when the event payload
    is malformed for its ``event_type`` -- a caller data problem, not a
    re-optimization outcome.
    """
    shift = (
        await session.execute(
            select(Shift).options(selectinload(Shift.jobs)).where(Shift.id == event.shift_id)
        )
    ).scalar_one_or_none()
    if shift is None:
        raise ValueError(f"Shift {event.shift_id} not found")

    worker = (
        await session.execute(select(Worker).where(Worker.id == event.worker_id))
    ).scalar_one_or_none()
    if worker is None:
        raise ValueError(f"Worker {event.worker_id} not found")

    # "Current" roster assignment for this worker/shift: the one from the
    # most recently created Roster. Multiple Roster batches could in
    # principle contain an assignment for the same worker/day/shift (the
    # unique constraint is scoped per-roster, not globally) -- ARCHITECTURE.md
    # doesn't define a "current roster" notion explicitly, so this is a
    # judgment call. FLAG FOR REVIEW.
    roster_assignment = (
        await session.execute(
            select(RosterAssignment)
            .where(
                RosterAssignment.shift_id == event.shift_id,
                RosterAssignment.worker_id == event.worker_id,
            )
            .order_by(RosterAssignment.roster_id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    route = None
    if roster_assignment is not None:
        route = (
            await session.execute(
                select(Route).where(Route.roster_assignment_id == roster_assignment.id)
            )
        ).scalar_one_or_none()

    completed = set(event.completed_job_ids)
    remaining_jobs = [
        JobSpec(
            job_id=j.id,
            site_id=j.site_id,
            window_start=as_utc(j.window_start),
            window_end=as_utc(j.window_end),
            duration_minutes=j.duration_minutes,
        )
        for j in shift.jobs
        if j.id not in completed
    ]

    site_ids = {worker.home_site_id} | {j.site_id for j in remaining_jobs}
    if event.current_site_id is not None:
        site_ids.add(event.current_site_id)
    travel_matrix = await load_travel_matrix(session, site_ids)

    solver_event = SolverEvent(
        event_type=_EVENT_TYPE_TO_SOLVER[event.event_type],
        shift_id=event.shift_id,
        worker_id=event.worker_id,
        home_site_id=worker.home_site_id,
        remaining_jobs=remaining_jobs,
        travel_matrix=travel_matrix,
        current_site_id=event.current_site_id,
        current_time=as_utc(event.current_time) if event.current_time is not None else None,
        cancelled_job_id=event.cancelled_job_id,
        planned_time=as_utc(event.planned_time) if event.planned_time is not None else None,
    )
    result = handle_event(solver_event)

    occurred_at = event.occurred_at or datetime.now(UTC)
    event_row = ReoptimizationEventRow(
        event_type=event.event_type,
        occurred_at=occurred_at,
        shift_id=event.shift_id,
        worker_id=event.worker_id,
        route_id=route.id if route is not None else None,
        resolution=_OUTCOME_TO_RESOLUTION[result.outcome],
        resolved_at=datetime.now(UTC),
        status=_OUTCOME_TO_STATUS[result.outcome],
    )
    session.add(event_row)
    await session.flush()  # assign event_row.id

    if result.outcome is OutcomeType.SCOPED_RESOLVE:
        if roster_assignment is None:
            raise ValueError(
                f"no RosterAssignment found for worker {event.worker_id} on shift "
                f"{event.shift_id}; cannot persist a scoped-resolve route without one"
            )
        is_new_route = route is None
        if is_new_route:
            route = Route(roster_assignment_id=roster_assignment.id)
        # Set every NOT NULL column before the first flush of a brand-new
        # Route -- see app/services/dispatch/service.py's identical note.
        route.status = RouteStatus.SOLVED
        route.generated_at = datetime.now(UTC)
        if is_new_route:
            session.add(route)
            await session.flush()
        assert result.route is not None  # SCOPED_RESOLVE always carries a route
        await replace_route_stops(session, route, result.route.stops)
        event_row.route_id = route.id

    await session.commit()

    # NOTE: there is no free-text "reason" column on ReoptimizationEvent
    # (see app/models/enums.py / app/models/reoptimization.py) -- only
    # `resolution`/`status`/`resolved_at`. The escalation reason is
    # therefore returned in `ReoptimizationOutcome` for the caller/job
    # result to see, but is NOT persisted on the row itself. The task
    # brief's schema-additions section named exactly two additions
    # (TravelMatrixEntry, Roster.failure_reason); adding a third
    # (a reason column here) wasn't in scope, so this is flagged rather
    # than silently bolted on.
    return ReoptimizationOutcome(
        event_id=event_row.id,
        event_type=event.event_type,
        outcome=result.outcome,
        shift_id=event.shift_id,
        worker_id=event.worker_id,
        shift_date=shift.date,
        route_id=event_row.route_id,
        escalation_reason=result.escalation_reason,
        detail=result.detail,
    )
