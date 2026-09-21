"""Async task queue (arq) task functions.

Each function here wraps one DB-facing service call from ``app/services/``
and is registered on ``WorkerSettings.functions`` below, so an ``arq``
worker process (``arq app.workers.tasks.WorkerSettings``) can pick jobs off
the Redis queue and run them. Per ARCHITECTURE.md's "FastAPI service layer
+ async task queue" section, Tier 1 (rostering) and Tier 2 (dispatch)
solves are dispatched onto this queue rather than run inline on a request;
Tier 3 (re-optimization) events are also queued here for consistency (per
the task description) even though they are meant to resolve "on the order
of seconds" -- an ``arq`` job starts near-instantly whenever a worker
process is running, so queuing costs it essentially nothing.

**Job chaining -- escalation.** Per
``app/services/reoptimization/service.py``'s module docstring, the service
layer never calls the rostering service directly on escalation (that would
bypass the queue). Instead, ``handle_reoptimization_event_task`` below
checks the outcome after calling the service and, on
``OutcomeType.ESCALATED_TO_TIER1``, enqueues a ``solve_roster_task`` itself
via ``ctx["redis"]`` (the same ``ArqRedis`` pool the worker uses for every
job, always present in ``ctx`` -- see ``arq.worker.Worker.main``). This is
the one place a rostering re-solve gets triggered automatically rather than
via an explicit ``POST /rostering/solve``.

FLAG FOR REVIEW -- **what period does an escalation-triggered re-solve
cover?** Tier 1 solves a ``period_start..period_end`` range (a whole
roster), but an escalation event is scoped to a single shift, which has
only one ``date``. Neither ARCHITECTURE.md nor the task brief says what
period an automatic escalation re-solve should span (the shift's whole
roster period? A wider window?). This module uses the escalating shift's
own ``date`` as both ``period_start`` and ``period_end`` -- the smallest
period guaranteed to at least re-cover the shift that triggered the
escalation -- rather than guessing at a wider window with no basis for its
size.

The task functions themselves are plain ``async def`` callables (arq calls
them with a leading ``ctx`` dict), so they can also be called directly and
awaited in tests without a real queue/worker (see ``tests/test_workers.py``).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from arq.connections import RedisSettings

from app.core.db import async_session_factory
from app.core.settings import settings
from app.models.enums import ReoptimizationEventType
from app.services.dispatch.service import DispatchOutcome, solve_and_persist_route
from app.services.reoptimization.service import (
    IncomingReoptimizationEvent,
    ReoptimizationOutcome,
    handle_and_persist_event,
)
from app.services.reoptimization.solver import OutcomeType
from app.services.rostering.service import RosterSolveOutcome, solve_and_persist_roster


def _roster_outcome_to_dict(outcome: RosterSolveOutcome) -> dict[str, Any]:
    return {
        "roster_id": outcome.roster_id,
        "status": outcome.status.value,
        "total_cost": outcome.total_cost,
        "failure_reason": outcome.failure_reason,
        "unfilled_shift_ids": outcome.unfilled_shift_ids,
    }


def _dispatch_outcome_to_dict(outcome: DispatchOutcome) -> dict[str, Any]:
    return {
        "roster_assignment_id": outcome.roster_assignment_id,
        "mode": outcome.mode,
        "route_id": outcome.route_id,
        "site_assignment_id": outcome.site_assignment_id,
        "status": outcome.status,
        "stops_count": outcome.stops_count,
        "infeasible_job_ids": outcome.infeasible_job_ids,
        "reason": outcome.reason,
    }


def _reopt_outcome_to_dict(outcome: ReoptimizationOutcome) -> dict[str, Any]:
    return {
        "event_id": outcome.event_id,
        "event_type": outcome.event_type.value,
        "outcome": outcome.outcome.value,
        "shift_id": outcome.shift_id,
        "worker_id": outcome.worker_id,
        "route_id": outcome.route_id,
        "escalation_reason": outcome.escalation_reason,
        "detail": outcome.detail,
    }


async def solve_roster_task(
    ctx: dict[str, Any], period_start: str, period_end: str
) -> dict[str, Any]:
    """Tier 1: solve and persist a Roster for `period_start..period_end` (ISO dates)."""
    async with async_session_factory() as session:
        outcome = await solve_and_persist_roster(
            session, date.fromisoformat(period_start), date.fromisoformat(period_end)
        )
    return _roster_outcome_to_dict(outcome)


async def solve_route_task(ctx: dict[str, Any], roster_assignment_id: int) -> dict[str, Any]:
    """Tier 2: solve/bypass and persist a Route or SiteAssignment for one
    RosterAssignment.
    """
    async with async_session_factory() as session:
        outcome = await solve_and_persist_route(session, roster_assignment_id)
    return _dispatch_outcome_to_dict(outcome)


async def handle_reoptimization_event_task(
    ctx: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any]:
    """Tier 3: handle one re-optimization event and persist the outcome.

    `event` is the JSON-serializable payload built by ``POST /events`` (see
    ``app/schemas/events.py``) -- one of the three Tier 3 event shapes,
    already discriminated on ``event_type`` by the API layer's Pydantic
    validation before this job was ever enqueued.

    On escalation, chains a ``solve_roster_task`` job onto the same queue
    (see module docstring) rather than calling the rostering service
    directly.
    """
    incoming = IncomingReoptimizationEvent(
        event_type=ReoptimizationEventType(event["event_type"]),
        shift_id=event["shift_id"],
        worker_id=event["worker_id"],
        occurred_at=(
            datetime.fromisoformat(event["occurred_at"]) if event.get("occurred_at") else None
        ),
        cancelled_job_id=event.get("cancelled_job_id"),
        current_site_id=event.get("current_site_id"),
        current_time=(
            datetime.fromisoformat(event["current_time"]) if event.get("current_time") else None
        ),
        planned_time=(
            datetime.fromisoformat(event["planned_time"]) if event.get("planned_time") else None
        ),
        completed_job_ids=event.get("completed_job_ids", []),
    )

    async with async_session_factory() as session:
        outcome = await handle_and_persist_event(session, incoming)

    result = _reopt_outcome_to_dict(outcome)

    if outcome.outcome is OutcomeType.ESCALATED_TO_TIER1:
        period = outcome.shift_date.isoformat()
        chained_job = await ctx["redis"].enqueue_job("solve_roster_task", period, period)
        result["escalation_roster_job_id"] = chained_job.job_id if chained_job is not None else None

    return result


class WorkerSettings:
    """``arq`` worker configuration -- run with ``arq app.workers.tasks.WorkerSettings``."""

    functions = [solve_roster_task, solve_route_task, handle_reoptimization_event_task]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
