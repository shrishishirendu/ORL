"""Shared native-Postgres enum types used across ORL domain models.

We use SQLAlchemy's ``Enum`` type with ``native_enum=True`` (the default),
which Alembic/SQLAlchemy will render as a native Postgres ``CREATE TYPE ...
AS ENUM (...)`` on migration. This gives us DB-level validation of these
status/event vocabularies at the cost of a migration step whenever a new
value is added (``ALTER TYPE ... ADD VALUE``). For the small, slow-changing
vocabularies below (roster/route status, re-optimization event types) that
trade-off favours native enums over a plain ``VARCHAR`` + check constraint.

NOTE FOR REVIEW: the exact members of each enum are not specified verbatim
in ARCHITECTURE.md or the task list beyond the examples given (e.g.
"pending/solved/failed", "job_cancelled/worker_sick/visit_overran",
"scoped_resolve/escalated_to_tier1"). Where the task description gave an
explicit list we used it as-is; where a status needed an extra value to be
usable end-to-end (e.g. a "running"/"in-progress" state) we flagged it in
the model's docstring rather than silently inventing it.
"""

from __future__ import annotations

import enum


class RosterStatus(enum.StrEnum):
    """Lifecycle status of a Tier 1 batch Roster run.

    ``pending`` / ``solved`` / ``failed`` are exactly what the task
    description specifies. We did NOT add an intermediate "running" state
    even though Tier 1 solves are dispatched onto the async task queue and
    could plausibly be "in progress" for a while -- flagging this as a
    decision point: if the async worker needs to distinguish "queued but
    not started" from "actively solving", this enum will need a 4th value.
    """

    PENDING = "pending"
    SOLVED = "solved"
    FAILED = "failed"


class RouteStatus(enum.StrEnum):
    """Lifecycle status of a Tier 2 Route.

    Not specified explicitly in the task description beyond "status: ...".
    We modeled it analogously to RosterStatus since Route is the Tier 2
    equivalent of a solved artifact. FLAG FOR REVIEW: confirm these are the
    values you want (in particular whether a route can be individually
    "superseded" once Tier 3 re-optimizes it, rather than just re-solved in
    place).
    """

    PENDING = "pending"
    SOLVED = "solved"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class ReoptimizationEventType(enum.StrEnum):
    """Tier 3 trigger types, exactly as listed in ARCHITECTURE.md's triggers
    and the task description: job cancelled, worker sick, visit overran.
    """

    JOB_CANCELLED = "job_cancelled"
    WORKER_SICK = "worker_sick"
    VISIT_OVERRAN = "visit_overran"


class ReoptimizationResolution(enum.StrEnum):
    """How a Tier 3 event was (or will be) resolved, per ARCHITECTURE.md's
    Tier 3 section: a scoped Tier 2 re-solve, or escalation up to Tier 1
    when the scoped re-solve is infeasible.
    """

    SCOPED_RESOLVE = "scoped_resolve"
    ESCALATED_TO_TIER1 = "escalated_to_tier1"


class ReoptimizationStatus(enum.StrEnum):
    """Processing status of a ReoptimizationEvent row.

    Not specified explicitly anywhere in the source material -- FLAG FOR
    REVIEW. We added this because the task explicitly lists ``status`` as a
    ReoptimizationEvent column distinct from ``resolution`` (which records
    *how* it was resolved) and ``resolved_at`` (*when*). ``status`` here
    tracks whether the event has been handled yet at all.
    """

    OPEN = "open"
    RESOLVING = "resolving"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class EmploymentType(enum.StrEnum):
    """A worker's employment basis, as the Award Engine service's contract
    spells it (``docs/AWARD_ENGINE_CONTRACT.md``, "Worker" shape:
    ``full_time|part_time|casual``).

    ORL only *stores* this and passes it through to the engine -- it never
    applies any award rule keyed on it (casual loading, part-time agreed
    hours, ...); that interpretation lives entirely in the engine, per
    ARCHITECTURE.md's "AwardCostMatrix boundary".
    """

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CASUAL = "casual"


class RosterCostStatus(enum.StrEnum):
    """Where a solved ``Roster``'s reported cost came from -- the result of
    post-solve reconciliation against the Award Engine service (see
    ``app/services/award_engine/reconcile.py``, which is the only writer).

    - ``engine_exact``: the engine priced the whole solved roster;
      ``Roster.engine_total_cost`` is the exact full-period figure.
    - ``engine_unresolved``: the engine was reached but could not price at
      least one worker (``total_cost: null`` in its response), so there is
      no exact figure -- only the solver estimate.
    - ``engine_unavailable``: the engine is configured but could not be
      reached (network error, timeout, 5xx).
    - ``engine_rejected``: the engine answered 422 -- ORL sent data it
      considers invalid (e.g. an unknown classification level key). Kept
      apart from ``engine_unavailable`` because the fix is in ORL's worker
      data, not in the engine's uptime.
    - ``placeholder_estimate``: the roster was (at least partly) costed off
      placeholder ``AwardCostMatrix`` rows, or assigns workers that have no
      award fields and so cannot be engine-priced. The only cost on offer
      is the solver estimate, flagged as such.

    ``NULL`` on the column means "no reconciliation happened": the engine is
    not configured and no placeholder rows were involved, the roster
    failed, or it predates this column.
    """

    ENGINE_EXACT = "engine_exact"
    ENGINE_UNRESOLVED = "engine_unresolved"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    ENGINE_REJECTED = "engine_rejected"
    PLACEHOLDER_ESTIMATE = "placeholder_estimate"
