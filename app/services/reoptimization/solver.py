"""Tier 3 — event-driven intra-day re-optimization.

Per ``ARCHITECTURE.md``'s Tier 3 section, this module reacts to three
triggers -- **job cancelled**, **worker sick**, **visit overran** -- and
decides, for each, whether the disruption can be absorbed with a **scoped
re-solve back into Tier 2** (re-run routing for the affected worker only) or
whether it must be **escalated up to Tier 1** (a rostering re-solve, because
the shift itself is no longer coverable at the routing level).

This module orchestrates Tiers 1 and 2; it does not reimplement either.
Concretely:

- It calls ``app.services.dispatch.solver.solve_shift_route`` directly for
  every scoped re-solve.
- It never calls ``app.services.rostering.solver.solve_roster``. Escalating
  "up to Tier 1" here means producing a clear, structured
  ``ReoptimizationResult`` naming *why* Tier 2 alone can't fix this --
  something a later task (the API/DB layer this module has no dependency
  on) hands to Tier 1. Actually invoking Tier 1's solver from inside an
  event handler would blur Tier 1's batch/period-level scope with Tier 3's
  single-event scope; that wiring belongs one layer up, where a real
  rostering re-solve can be scheduled/queued rather than run inline on an
  event-handling request.

As with the other two tiers' solvers, this module touches no DB and no API
routes -- pure input -> output over the same plain dataclasses
``app.services.dispatch.solver`` already defines (``JobSpec``,
``TravelTimeMatrix``, ``RouteSolveResult``), so it is unit-testable in
isolation (see ``tests/test_reoptimization.py``) and reusable unchanged once
a real event-handling API wires it up.

**Per-trigger policy** (this is a product decision, implemented here exactly
as specified rather than invented) --

- ``job_cancelled``: drop the cancelled job from the shift's remaining job
  list and re-solve the worker's route from their current position (home, if
  the shift hasn't started yet) with the reduced list. Removing a job cannot
  make a previously-feasible route infeasible *at the original start
  position*, but this module never assumes that: it always re-solves and
  branches on the real result, because a *mid-shift* cancellation re-solves
  from the worker's current position/time, not the original start, and
  feasibility from home is not proof of feasibility from wherever the worker
  now stands. If the re-solve is somehow infeasible anyway, escalate.
- ``visit_overran``: an already-completed visit ran long, pushing the
  worker's actual current time later than the plan assumed. Re-solve the
  *remaining* (not-yet-visited) jobs from the worker's actual current
  position and actual current time. If every remaining job can still be fit
  (in whatever order/timing works) within its time window and the route
  still gets the worker home, that is a scoped resolve; otherwise escalate.
- ``worker_sick``: the worker themselves is unavailable for the rest of the
  shift. There is no Tier-2-only fix for this -- Tier 2 only resequences a
  worker's own jobs, it does not reassign jobs to a *different* worker (see
  ``app/services/dispatch/solver.py``'s module docstring: this is
  deliberately a single-vehicle VRPTW, not a multi-vehicle
  reassignment/swap problem). Per ARCHITECTURE.md's boundary between "who's
  assigned" (Tier 1) and "how their day is sequenced" (Tier 2), this is
  modeled as an **automatic, unconditional escalation to Tier 1** -- this
  module never attempts a Tier 2 scoped resolve for the sick worker's own
  remaining jobs, and ``solve_shift_route`` is never called for this event
  type at all.

  FLAG FOR REVIEW: this reading treats "worker sick" as *always* Tier-1-only,
  even for the trivial case where the sick worker's remaining jobs list is
  already empty (nothing left to reassign). That trivial case is still
  escalated rather than special-cased into a no-op, on the theory that
  *something* downstream (the roster) still needs to know this worker is
  down for the rest of the shift even if there is nothing left to re-route
  today -- but this module does not attempt to distinguish "trivial" from
  "material" sick escalations; it flags the disruption every time and lets
  Tier 1 (or a human) decide it's a non-event. A more elaborate policy
  (e.g. skip escalation when nothing remains) was deliberately not built --
  see also the module-level flag on not building multi-worker job-swapping,
  which would be the *other* natural way to attempt a Tier-2-only fix here.

**Time budget**: ``_SCOPED_RESOLVE_TIME_LIMIT_SECONDS`` (2.0s, see below) is
used for every scoped re-solve, well under Tier 2's own 5-second full-day
default, and comfortably inside ARCHITECTURE.md's "on the order of seconds"
tempo for the whole event-handling round trip.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.services.dispatch.solver import (
    JobSpec,
    RouteSolveResult,
    TravelTimeMatrix,
    solve_shift_route,
)

# ARCHITECTURE.md pins Tier 3's tempo at "event-driven, on the order of
# seconds" for the *whole* reaction (API receives the event -> Tier 3 decides
# -> a route is back in front of the worker), not just the OR-tools search
# inside one scoped re-solve. 2.0 seconds is this module's exact choice for
# that inner search budget: it leaves headroom in the "seconds" tempo for
# everything else in the round trip, while still being generous for the
# small remaining-job counts a scoped re-solve operates on (ARCHITECTURE.md
# scopes Tier 3 to "the affected shift/worker(s) only", so this is a handful
# of stops, never a whole roster). This replaces -- it does not reuse --
# `solve_shift_route`'s own 5-second default, which was tuned for a batch
# per-shift solve, not a live event handler.
_SCOPED_RESOLVE_TIME_LIMIT_SECONDS = 2.0


class EventType(enum.StrEnum):
    """The three Tier 3 triggers named in ARCHITECTURE.md's Tier 3 section.

    Deliberately a separate enum from ``app.models.enums.
    ReoptimizationEventType`` (same three values) rather than importing it --
    this module stays free of any dependency on ``app.models``/the ORM
    layer, the same way ``app.services.dispatch.solver`` and
    ``app.services.rostering.solver`` stay free of ``Job``/``Worker`` ORM
    rows (see their module docstrings). A future persistence boundary maps
    between the two, the same way it will map ``JobSpec`` to/from ``Job``
    rows.
    """

    JOB_CANCELLED = "job_cancelled"
    WORKER_SICK = "worker_sick"
    VISIT_OVERRAN = "visit_overran"


class OutcomeType(enum.StrEnum):
    """What a Tier 3 event handler decided to do about the disruption.

    Same values as ``app.models.enums.ReoptimizationResolution`` -- see
    ``EventType``'s docstring on why this is a separate, ORM-free enum
    rather than an import of that one.
    """

    SCOPED_RESOLVE = "scoped_resolve"
    ESCALATED_TO_TIER1 = "escalated_to_tier1"


@dataclass(frozen=True)
class ReoptimizationEvent:
    """One Tier 3 trigger.

    Common fields (every event type):
        event_type: which of the three ARCHITECTURE.md triggers this is.
        shift_id, worker_id: which shift/worker this concerns -- Tier 3's
            re-solve scope per ARCHITECTURE.md ("the affected shift/worker(s)
            only, not the whole day or roster").
        home_site_id: the worker's home site -- the route's end depot,
            unconditionally (see ``app/services/dispatch/solver.py``).
        remaining_jobs: the shift's not-yet-visited jobs *before* this
            event's effect is applied. For ``job_cancelled`` this still
            includes the cancelled job itself (the handler removes it); for
            ``visit_overran`` and ``worker_sick`` it is exactly the jobs
            still to be done.
        travel_matrix: the cached Travel Matrix covering these sites (see
            ARCHITECTURE.md's "Supporting components" section -- it also
            feeds Tier 3's scoped re-solves, not just Tier 2's).
        current_site_id: where the worker actually is right now. ``None``
            means "the shift hasn't started yet" -- the re-solve (when one
            happens) then starts from ``home_site_id``, matching
            ``solve_shift_route``'s own default. Required (non-``None``) for
            ``visit_overran``, since "the worker's actual current position"
            is the whole point of that trigger.
        current_time: the worker's actual current time right now. ``None``
            lets the scoped re-solve fall back to ``solve_shift_route``'s own
            default epoch (the earliest remaining job's window start).
            Required (non-``None``) for ``visit_overran``, for the same
            reason as ``current_site_id``.
        time_limit_seconds: the OR-tools search budget for this event's
            scoped re-solve, if one happens. Defaults to this module's
            "seconds" tempo budget (see ``_SCOPED_RESOLVE_TIME_LIMIT_SECONDS``
            above) -- overridable per event mainly for tests.

    Event-specific payload:
        cancelled_job_id: (``job_cancelled`` only) the job being cancelled;
            must be present in ``remaining_jobs``.
        planned_time: (``visit_overran`` only, optional) what the original
            plan assumed "now" would be at this point in the shift -- kept
            purely for the audit trail a later persistence layer will want
            ("planned X, actual Y"); the re-solve itself only ever uses
            ``current_time`` (the actual time).
    """

    event_type: EventType
    shift_id: int
    worker_id: int
    home_site_id: int
    remaining_jobs: Sequence[JobSpec]
    travel_matrix: TravelTimeMatrix
    current_site_id: int | None = None
    current_time: datetime | None = None
    time_limit_seconds: float = _SCOPED_RESOLVE_TIME_LIMIT_SECONDS
    cancelled_job_id: int | None = None
    planned_time: datetime | None = None


@dataclass(frozen=True)
class ReoptimizationResult:
    """The outcome of handling one ``ReoptimizationEvent``.

    Exactly one of ``route`` / ``escalation_reason`` is populated, matching
    ``outcome``:

    - ``SCOPED_RESOLVE``: ``route`` is the new (feasible)
      ``RouteSolveResult`` from Tier 2 -- including the trivial "nothing left
      to do" case, where ``route.stops == []`` and the worker simply goes
      straight home (see the module docstring; this is deliberately *not* a
      separate outcome/enum value -- it is what a scoped resolve over an
      empty job list naturally produces).
    - ``ESCALATED_TO_TIER1``: ``escalation_reason`` is a human-readable string
      naming why Tier 2 alone can't absorb this, worth persisting alongside
      the event for whatever later triggers the actual Tier 1 re-solve. This
      module never calls Tier 1's solver itself (see the module docstring).
    """

    outcome: OutcomeType
    event_type: EventType
    shift_id: int
    worker_id: int
    route: RouteSolveResult | None = None
    escalation_reason: str | None = None
    detail: str | None = None


def handle_event(event: ReoptimizationEvent) -> ReoptimizationResult:
    """Dispatch a ``ReoptimizationEvent`` to its trigger-specific handler.

    This is the module's one public entry point. Never raises for a
    well-formed event of any outcome (scoped-resolve or escalate); raises
    ``ValueError`` only for a malformed event (e.g. a ``job_cancelled`` event
    with no ``cancelled_job_id``, or an event referencing a job id not
    actually in ``remaining_jobs``) -- a caller/data problem, not a
    re-optimization outcome, mirroring how ``solve_shift_route`` treats a
    missing travel-matrix entry.
    """
    if event.event_type is EventType.JOB_CANCELLED:
        return _handle_job_cancelled(event)
    if event.event_type is EventType.VISIT_OVERRAN:
        return _handle_visit_overran(event)
    if event.event_type is EventType.WORKER_SICK:
        return _handle_worker_sick(event)
    raise ValueError(f"unknown event_type: {event.event_type!r}")  # pragma: no cover


def _start_site(event: ReoptimizationEvent) -> int:
    return event.current_site_id if event.current_site_id is not None else event.home_site_id


def _handle_job_cancelled(event: ReoptimizationEvent) -> ReoptimizationResult:
    if event.cancelled_job_id is None:
        raise ValueError("job_cancelled event requires cancelled_job_id")

    reduced_jobs = [j for j in event.remaining_jobs if j.job_id != event.cancelled_job_id]
    if len(reduced_jobs) == len(event.remaining_jobs):
        raise ValueError(
            f"cancelled_job_id {event.cancelled_job_id} is not in remaining_jobs "
            f"for shift {event.shift_id}"
        )

    route = solve_shift_route(
        reduced_jobs,
        event.travel_matrix,
        home_site_id=event.home_site_id,
        current_site_id=event.current_site_id,
        shift_start=event.current_time,
        time_limit_seconds=event.time_limit_seconds,
    )

    if route.feasible:
        detail = (
            "cancelled job removed; no jobs remain, worker routed straight home"
            if not reduced_jobs
            else "cancelled job removed; remaining jobs re-sequenced from the worker's "
            "current position"
        )
        return _scoped(event, route, detail=detail)

    return _escalate(
        event,
        f"removing cancelled job {event.cancelled_job_id} still leaves an infeasible "
        f"route for worker {event.worker_id} on shift {event.shift_id} from their "
        f"current position/time: {route.reason}",
    )


def _handle_visit_overran(event: ReoptimizationEvent) -> ReoptimizationResult:
    if event.current_site_id is None or event.current_time is None:
        raise ValueError(
            "visit_overran event requires both current_site_id and current_time "
            "(the worker's actual position/time after the overrun)"
        )

    route = solve_shift_route(
        event.remaining_jobs,
        event.travel_matrix,
        home_site_id=event.home_site_id,
        current_site_id=event.current_site_id,
        shift_start=event.current_time,
        time_limit_seconds=event.time_limit_seconds,
    )

    if route.feasible:
        detail = (
            "no jobs remain after the overrun; worker routed straight home"
            if not event.remaining_jobs
            else "remaining jobs re-sequenced/re-timed from the worker's actual "
            "post-overrun position and time; all still fit their windows"
        )
        return _scoped(event, route, detail=detail)

    return _escalate(
        event,
        f"visit overrun pushed worker {event.worker_id}'s current time later than "
        f"planned; the remaining jobs on shift {event.shift_id} can no longer all be "
        f"fit from the worker's actual position/time: {route.reason}",
    )


def _handle_worker_sick(event: ReoptimizationEvent) -> ReoptimizationResult:
    # Deliberately no `solve_shift_route` call -- see the module docstring's
    # policy note. A sick worker cannot be re-routed; only a rostering
    # re-solve (reassigning their jobs, or accepting them unfilled) can
    # follow, which is Tier 1's job, not this module's.
    return _escalate(
        event,
        f"worker {event.worker_id} is unavailable for the rest of shift "
        f"{event.shift_id} (reported sick); {len(event.remaining_jobs)} job(s) remain "
        "unvisited on their route. Tier 2 can only resequence a present worker's own "
        "jobs, not reassign them to a different worker, so this always needs a Tier 1 "
        "rostering re-solve rather than a Tier 2 scoped resolve.",
    )


def _scoped(
    event: ReoptimizationEvent, route: RouteSolveResult, *, detail: str
) -> ReoptimizationResult:
    return ReoptimizationResult(
        outcome=OutcomeType.SCOPED_RESOLVE,
        event_type=event.event_type,
        shift_id=event.shift_id,
        worker_id=event.worker_id,
        route=route,
        detail=detail,
    )


def _escalate(event: ReoptimizationEvent, reason: str) -> ReoptimizationResult:
    return ReoptimizationResult(
        outcome=OutcomeType.ESCALATED_TO_TIER1,
        event_type=event.event_type,
        shift_id=event.shift_id,
        worker_id=event.worker_id,
        escalation_reason=reason,
    )
