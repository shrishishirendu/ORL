"""Tier 1 rostering solver — pure CP-SAT model, no DB access.

Per ``ARCHITECTURE.md``'s Tier 1 section, this module takes the
``AwardCostMatrix`` (an external, consumed-only artifact -- see
``app/models/award_cost_matrix.py``), worker skills/geo data and shift
requirements, and produces a ``Roster``: a worker -> day -> shift assignment
that **minimizes total dollar cost** (``AwardCostMatrix.pay_cost``), not raw
hours worked.

Everything here operates on plain dataclasses, not ORM rows or an open DB
session -- ``app/services/rostering/service.py`` is the (documented, not yet
wired) boundary that will translate ``Worker``/``Shift``/``AwardCostMatrix``
rows into these shapes and ``RosterAssignment`` rows back out.

Modeling decisions/assumptions not fully pinned down by ARCHITECTURE.md or
the schema -- flagging these explicitly rather than silently picking one:

1. **min_hours/max_hours are stored per (worker, day, shift) row** in
   ``AwardCostMatrix``, but ARCHITECTURE.md and the task both describe them
   as a period-level bound on a worker's *total* assigned hours ("respect
   each worker's min_hours/max_hours ... over the rostering period"). This
   module therefore aggregates each worker's bound across every matrix row
   supplied for them: the **most restrictive** value wins (``max()`` of the
   ``min_hours`` values, ``min()`` of the ``max_hours`` values), so a solve
   can never silently violate a bound that appears on any one row. If the
   Award Interpretation Engine always emits the same min/max on every row
   for a given worker (the likely intent), this aggregation is a no-op.
2. **min_hours only binds a worker who is actually rostered at least one
   shift.** A worker assigned zero shifts is not forced to satisfy
   ``min_hours`` (that would make every unused worker's absence itself an
   infeasibility, which cannot be the intent). ``max_hours`` is enforced
   unconditionally (trivially satisfied at zero assigned hours).
3. **Shift duration** is computed from ``start_time``/``end_time`` on the
   shift's ``date``, treating ``end_time <= start_time`` as an overnight
   shift that ends the following day. This also means the no-double-booking
   check is a true datetime-interval overlap (via CP-SAT ``NoOverlap`` on
   optional intervals), which is a strict superset of the task's literal
   "same day" wording -- it additionally catches a shift that runs past
   midnight into the next shift's early hours.
4. **Objective ties are unresolved.** The objective is exactly total
   ``pay_cost`` -- no secondary/tie-break term (e.g. preferring fewer
   distinct workers, or a specific worker) is specified anywhere in
   ARCHITECTURE.md, so none is added. When multiple assignments share the
   minimum cost, CP-SAT returns *one* optimal solution with no guaranteed
   preference among them.
5. **Geo is a hard eligibility filter, not a cost term.** Per the product
   owner's architectural correction (home location is the depot for both
   tiers -- see ARCHITECTURE.md's "Home location as depot" section), a
   worker can only be assigned to a shift when ``worker.region ==
   shift.site_region``. This is enforced exactly like the skill/eligibility
   filter already was: a worker missing a region match is simply never a
   candidate for that shift, computed up front alongside the
   skill/``AwardCostMatrix.eligible`` check, before the CP-SAT model is even
   built. The **objective is unchanged** -- still exactly total ``pay_cost``,
   no distance/travel term -- this is a feasibility filter, not a cost
   preference.

   "Region compatible" is implemented here as plain string equality on the
   ``region``/``site_region`` fields already threaded through
   ``WorkerInput``/``ShiftInput``. Nothing in ARCHITECTURE.md or the schema
   defines a coarser notion of compatibility (e.g. adjacent-region lists, a
   region hierarchy) -- if the business needs one later, it belongs as an
   explicit input (e.g. a compatible-regions set) rather than invented here.
   FLAG FOR REVIEW: a ``None`` on *either* side (``WorkerInput.region`` or
   ``ShiftInput.site_region``) is treated as "no constraint" -- i.e. that
   candidate is **not** excluded on region grounds -- rather than as a
   guaranteed mismatch. Both fields remain individually optional on their
   dataclasses (mirroring the nullable ``Worker.region``/``Site.region``
   columns), and callers/tests that never populate region data at all (as
   every pre-existing rostering test here does) get the old
   region-agnostic behaviour unchanged. A caller that has resolved
   ``Worker.home_site_id`` to a real region should always populate
   ``WorkerInput.region`` so the filter actually bites; a `None` here
   should be read as "region unknown to this solve", not "no home region",
   since every ``Worker`` now has a mandatory ``home_site_id``.
6. **Travel time between a worker's shifts** (product decision 2026-09-29:
   a general workforce, where one worker can do several shifts a day at
   different sites). When ``travel_minutes`` is supplied, a worker is never
   given two shifts at different sites whose gap -- the end of the earlier
   to the start of the later, on the absolute timeline so overnight shifts
   count -- is shorter than the directed travel time from the first site to
   the second. It is a hard feasibility rule, like no-double-booking; the
   **objective is unchanged** (still exactly total ``pay_cost``). Travel
   pay, broken-shift and minimum-engagement rules stay with the Award
   Interpretation Engine, which prices whole days.

   The rule is pairwise over every two shifts a worker could hold, which is
   exact for consecutive shifts. For a worker with shifts A, B, C it also
   checks A -> C directly; with a travel matrix that obeys the triangle
   inequality that is implied by A -> B and B -> C, so it only bites if the
   matrix has A -> C longer than A -> B -> C.

   **A missing travel-matrix entry is never read as 0 minutes** (the same
   rule as Tier 2's ``MissingTravelTimeError``). A pair of shifts at two
   sites with no entry, and a gap shorter than the longest travel time in
   the supplied matrix, is left unconstrained and reported in
   ``RosterSolution.warnings`` so the gap in the data is visible. With no
   travel data at all, a single warning says travel was not checked.
   ``travel_minutes=None`` (the default, for callers that predate this rule)
   skips the check entirely.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import datetime, time, timedelta

from ortools.sat.python import cp_model

# CP-SAT requires integer coefficients/domains. pay_cost (Numeric(10,2)) is
# scaled to whole cents; hours (Numeric(5,2)) are scaled to whole minutes via
# each shift's actual start/end times.
_PAY_COST_SCALE = 100

# Weight applied to each unfilled shift in the diagnostic relaxed re-solve
# (see `_diagnose_infeasibility`). Must dominate any plausible total pay_cost
# so the relaxed solve always prefers covering shifts over saving money,
# meaning the shifts it still leaves unfilled are the ones actually in
# conflict with the hard overlap/hours constraints.
_UNFILLED_PENALTY_WEIGHT = 10**9


class SolveStatus(enum.StrEnum):
    """Outcome of a `solve_roster` call."""

    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"


@dataclass(frozen=True)
class WorkerInput:
    """A worker, as the solver needs to see it.

    ``skills`` mirrors ``Worker.skills`` (see app/models/worker.py). ``region``
    mirrors ``Worker.region`` (the worker's home region, normally resolved
    from ``Worker.home_site_id``'s ``Site.region``) and is used as a hard
    eligibility filter against ``ShiftInput.site_region`` -- see module
    docstring point 5.
    """

    id: int
    skills: frozenset[str]
    region: str | None = None


@dataclass(frozen=True)
class ShiftInput:
    """A shift requirement, as the solver needs to see it.

    Mirrors ``Shift`` (see app/models/shift.py). ``site_region`` -- the
    region of the shift's ``Site`` -- is used as a hard eligibility filter
    against ``WorkerInput.region``, same caveat on ``None`` handling as
    ``WorkerInput.region`` (see module docstring point 5).
    """

    id: int
    date: date_
    start_time: time
    end_time: time
    required_skill: str
    site_id: int | None = None
    site_region: str | None = None


@dataclass(frozen=True)
class AwardCostMatrixEntry:
    """One `AwardCostMatrix` row (worker x day x shift), as the solver needs
    to see it. Mirrors app/models/award_cost_matrix.py.
    """

    worker_id: int
    day: date_
    shift_id: int
    pay_cost: float
    eligible: bool
    min_hours: float | None = None
    max_hours: float | None = None


@dataclass(frozen=True)
class Assignment:
    """One worker -> day -> shift assignment in a solved roster."""

    worker_id: int
    shift_id: int
    day: date_
    pay_cost: float


@dataclass
class RosterSolution:
    """Result of a `solve_roster` call.

    ``status`` is always set and never raises for an infeasible input --
    infeasibility is a normal, expected outcome (not enough eligible/skilled
    workers, or overlap/hours constraints that can't all be satisfied), and
    Tier 3's later escalation-to-Tier-1 path needs to be able to detect it
    without catching an exception.

    ``unfilled_shifts`` and ``diagnostics`` are only populated when
    ``status is SolveStatus.INFEASIBLE``; they name the shifts that could not
    be covered and, in ``diagnostics``, a human-readable reason for each.
    """

    status: SolveStatus
    assignments: list[Assignment] = field(default_factory=list)
    total_cost: float | None = None
    unfilled_shifts: list[int] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    # Data gaps the solve worked around without guessing (e.g. a missing
    # travel time between two sites). Set whatever the status.
    warnings: list[str] = field(default_factory=list)

    @property
    def is_feasible(self) -> bool:
        return self.status is not SolveStatus.INFEASIBLE


def _shift_window(shift: ShiftInput, epoch: date_) -> tuple[int, int]:
    """Return (start_offset_minutes, duration_minutes) for `shift`, measured
    from midnight on `epoch`. An `end_time <= start_time` shift is treated as
    running past midnight into the next day (see module docstring point 3).
    """
    start_dt = datetime.combine(shift.date, shift.start_time)
    end_dt = datetime.combine(shift.date, shift.end_time)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    epoch_dt = datetime.combine(epoch, time.min)
    start_offset = int((start_dt - epoch_dt).total_seconds() // 60)
    duration = int((end_dt - start_dt).total_seconds() // 60)
    return start_offset, duration


def _worker_hour_bounds_minutes(
    worker_ids: Sequence[int],
    matrix_by_worker: dict[int, list[AwardCostMatrixEntry]],
) -> dict[int, tuple[int | None, int | None]]:
    """Aggregate each worker's (min_minutes, max_minutes) bound across every
    matrix row supplied for them -- most restrictive wins. See module
    docstring point 1 for why this aggregates rather than reading a single
    row's value.
    """
    bounds: dict[int, tuple[int | None, int | None]] = {}
    for worker_id in worker_ids:
        rows = matrix_by_worker.get(worker_id, [])
        min_values = [r.min_hours for r in rows if r.min_hours is not None]
        max_values = [r.max_hours for r in rows if r.max_hours is not None]
        min_minutes = round(max(min_values) * 60) if min_values else None
        max_minutes = round(min(max_values) * 60) if max_values else None
        bounds[worker_id] = (min_minutes, max_minutes)
    return bounds


def _travel_conflicts(
    shifts: Sequence[ShiftInput],
    candidates_by_shift: dict[int, list[WorkerInput]],
    epoch: date_,
    travel_minutes: Mapping[tuple[int, int], int],
) -> tuple[list[tuple[int, int]], list[str]]:
    """Pairs of shifts no single worker can hold because the travel time
    between their sites doesn't fit the gap, plus warnings for site pairs with
    no travel time. See module docstring point 6.

    Only pairs sharing at least one candidate worker matter. Overlapping
    pairs are left to NoOverlap. Shifts are scanned in start order, so once
    the gap reaches the longest known travel time no later shift can conflict.
    """
    windows = []
    for shift in shifts:
        start, duration = _shift_window(shift, epoch)
        windows.append((start, start + duration, shift))
    windows.sort(key=lambda w: (w[0], w[2].id))
    candidate_ids = {s.id: {w.id for w in candidates_by_shift[s.id]} for s in shifts}
    longest = max(travel_minutes.values(), default=0)

    conflicts: list[tuple[int, int]] = []
    missing: set[tuple[int, int]] = set()
    for i, (_, a_end, a) in enumerate(windows):
        for b_start, _, b in windows[i + 1 :]:
            gap = b_start - a_end
            if gap >= longest:
                break
            if gap < 0 or a.site_id is None or b.site_id is None or a.site_id == b.site_id:
                continue
            if not candidate_ids[a.id] & candidate_ids[b.id]:
                continue
            needed = travel_minutes.get((a.site_id, b.site_id))
            if needed is None:
                missing.add((a.site_id, b.site_id))
            elif gap < needed:
                conflicts.append((a.id, b.id))

    warnings = [
        f"No travel time from site {src} to site {dst}: shifts there were not checked for "
        "travel between them. Add the pair to the travel matrix."
        for src, dst in sorted(missing)
    ]
    if not travel_minutes and len({s.site_id for s in shifts if s.site_id is not None}) > 1:
        warnings.append(
            "No travel times were loaded, so travel between a worker's shifts at different "
            "sites was not checked."
        )
    return conflicts, warnings


def _build_model(
    workers: Sequence[WorkerInput],
    shifts: Sequence[ShiftInput],
    candidates_by_shift: dict[int, list[WorkerInput]],
    matrix_by_ws: dict[tuple[int, int], AwardCostMatrixEntry],
    hour_bounds: dict[int, tuple[int | None, int | None]],
    epoch: date_,
    *,
    allow_unfilled: bool,
    travel_conflicts: Sequence[tuple[int, int]] = (),
) -> tuple[
    cp_model.CpModel,
    dict[tuple[int, int], cp_model.IntVar],
    dict[int, cp_model.IntVar],
]:
    """Shared model-building for both the real solve and the diagnostic
    relaxed re-solve. Returns (model, assignment_vars, unfilled_vars).
    `unfilled_vars` is empty when `allow_unfilled` is False.
    """
    model = cp_model.CpModel()
    assign_vars: dict[tuple[int, int], cp_model.IntVar] = {}
    unfilled_vars: dict[int, cp_model.IntVar] = {}

    for shift in shifts:
        candidates = candidates_by_shift[shift.id]
        for worker in candidates:
            assign_vars[(worker.id, shift.id)] = model.NewBoolVar(f"x_w{worker.id}_s{shift.id}")

        coverage_terms = [assign_vars[(w.id, shift.id)] for w in candidates]
        if allow_unfilled:
            unfilled = model.NewBoolVar(f"unfilled_s{shift.id}")
            unfilled_vars[shift.id] = unfilled
            model.Add(sum(coverage_terms) + unfilled == 1)
        else:
            # Only reachable when every shift has >=1 candidate (callers
            # short-circuit before this otherwise), so `== 1` is meaningful.
            model.Add(sum(coverage_terms) == 1)

    # No-double-booking: one optional interval per (worker, candidate shift),
    # present iff that assignment is chosen; CP-SAT's NoOverlap keeps any two
    # present intervals for the same worker from overlapping in time.
    intervals_by_worker: dict[int, list[cp_model.IntervalVar]] = {w.id: [] for w in workers}
    for shift in shifts:
        start_offset, duration = _shift_window(shift, epoch)
        for worker in candidates_by_shift[shift.id]:
            interval = model.NewOptionalIntervalVar(
                start_offset,
                duration,
                start_offset + duration,
                assign_vars[(worker.id, shift.id)],
                f"iv_w{worker.id}_s{shift.id}",
            )
            intervals_by_worker[worker.id].append(interval)
    for worker_id, intervals in intervals_by_worker.items():
        if len(intervals) > 1:
            model.AddNoOverlap(intervals)

    # Travel between sites: never both shifts of a conflicting pair for one
    # worker (module docstring point 6).
    for a_id, b_id in travel_conflicts:
        for worker in candidates_by_shift[a_id]:
            if (worker.id, b_id) in assign_vars:
                model.AddBoolOr(
                    [assign_vars[(worker.id, a_id)].Not(), assign_vars[(worker.id, b_id)].Not()]
                )

    # min/max hours over the period (see module docstring points 1 and 2).
    for worker in workers:
        worker_terms = [
            (assign_vars[(worker.id, shift.id)], _shift_window(shift, epoch)[1])
            for shift in shifts
            if (worker.id, shift.id) in assign_vars
        ]
        if not worker_terms:
            continue
        total_minutes = sum(var * minutes for var, minutes in worker_terms)
        min_minutes, max_minutes = hour_bounds.get(worker.id, (None, None))
        if max_minutes is not None:
            model.Add(total_minutes <= max_minutes)
        if min_minutes is not None:
            used = model.NewBoolVar(f"used_w{worker.id}")
            assignment_sum = sum(var for var, _ in worker_terms)
            model.Add(assignment_sum >= 1).OnlyEnforceIf(used)
            model.Add(assignment_sum == 0).OnlyEnforceIf(used.Not())
            model.Add(total_minutes >= min_minutes).OnlyEnforceIf(used)

    return model, assign_vars, unfilled_vars


def _diagnose_infeasibility(
    workers: Sequence[WorkerInput],
    shifts: Sequence[ShiftInput],
    candidates_by_shift: dict[int, list[WorkerInput]],
    matrix_by_ws: dict[tuple[int, int], AwardCostMatrixEntry],
    hour_bounds: dict[int, tuple[int | None, int | None]],
    epoch: date_,
    max_time_in_seconds: float,
    travel_conflicts: Sequence[tuple[int, int]] = (),
) -> list[int]:
    """Re-solve with each shift's coverage made optional (at a heavy penalty)
    to find which shifts are actually in conflict with the hard
    overlap/min-max-hours constraints, once eligibility/skill gaps (which are
    caught earlier, before any solve) are ruled out. Always feasible: leaving
    every shift unfilled trivially satisfies NoOverlap and the hours bounds.
    """
    model, assign_vars, unfilled_vars = _build_model(
        workers,
        shifts,
        candidates_by_shift,
        matrix_by_ws,
        hour_bounds,
        epoch,
        allow_unfilled=True,
        travel_conflicts=travel_conflicts,
    )
    penalty_terms = [_UNFILLED_PENALTY_WEIGHT * var for var in unfilled_vars.values()]
    # Cost terms need integer coefficients too; scale to cents like the main
    # objective (see _PAY_COST_SCALE).
    scaled_cost_terms = [
        round(matrix_by_ws[(w_id, s_id)].pay_cost * _PAY_COST_SCALE) * var
        for (w_id, s_id), var in assign_vars.items()
    ]
    model.Minimize(sum(penalty_terms) + sum(scaled_cost_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time_in_seconds
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # Should not happen (the relaxed model is always feasible), but don't
        # pretend to have a diagnosis we don't.
        return [s.id for s in shifts]
    return sorted(shift_id for shift_id, var in unfilled_vars.items() if solver.Value(var) == 1)


def solve_roster(
    workers: Sequence[WorkerInput],
    shifts: Sequence[ShiftInput],
    matrix: Sequence[AwardCostMatrixEntry],
    *,
    max_time_in_seconds: float = 30.0,
    travel_minutes: Mapping[tuple[int, int], int] | None = None,
) -> RosterSolution:
    """Solve a Tier 1 roster with CP-SAT.

    Pure function: no DB access, no I/O. Assigns each shift to exactly one
    eligible, skilled worker while respecting no-double-booking, travel time
    between a worker's shifts at different sites (``travel_minutes``, keyed
    ``(from_site_id, to_site_id)`` -- see module docstring point 6) and each
    worker's min/max hours over the period, minimizing total ``pay_cost``.

    Returns a `RosterSolution` in all cases -- including when no feasible
    roster exists -- rather than raising, so a caller (including a future
    Tier 3 escalation path) can branch on `RosterSolution.status` /
    `is_feasible` instead of catching an exception.
    """
    if not shifts:
        return RosterSolution(status=SolveStatus.OPTIMAL, assignments=[], total_cost=0.0)

    shift_by_id = {s.id: s for s in shifts}
    worker_by_id = {w.id: w for w in workers}

    matrix_by_ws: dict[tuple[int, int], AwardCostMatrixEntry] = {}
    matrix_by_worker: dict[int, list[AwardCostMatrixEntry]] = {}
    for entry in matrix:
        shift = shift_by_id.get(entry.shift_id)
        if shift is None or entry.worker_id not in worker_by_id:
            # Row refers to a shift/worker outside this solve's scope (e.g. a
            # different rostering period) -- not this call's concern.
            continue
        if entry.day != shift.date:
            raise ValueError(
                f"AwardCostMatrix row for worker={entry.worker_id} shift={entry.shift_id} "
                f"has day={entry.day!r} but Shift.date={shift.date!r}"
            )
        key = (entry.worker_id, entry.shift_id)
        if key in matrix_by_ws:
            raise ValueError(f"Duplicate AwardCostMatrix row for {key}")
        matrix_by_ws[key] = entry
        matrix_by_worker.setdefault(entry.worker_id, []).append(entry)

    def _region_compatible(worker: WorkerInput, shift: ShiftInput) -> bool:
        # A None on either side means "region unknown to this solve" -- not
        # enforced as a mismatch. See module docstring point 5.
        if worker.region is None or shift.site_region is None:
            return True
        return worker.region == shift.site_region

    candidates_by_shift: dict[int, list[WorkerInput]] = {}
    for shift in shifts:
        candidates_by_shift[shift.id] = [
            worker
            for worker in workers
            if shift.required_skill in worker.skills
            and (entry := matrix_by_ws.get((worker.id, shift.id))) is not None
            and entry.eligible
            and _region_compatible(worker, shift)
        ]

    unfillable = [sid for sid, candidates in candidates_by_shift.items() if not candidates]
    if unfillable:
        diagnostics = [
            f"Shift {sid} ({shift_by_id[sid].required_skill}): no eligible worker with "
            "the required skill and a compatible home region is available (checked "
            "AwardCostMatrix.eligible, Worker.skills, and Worker.region vs. "
            "Shift.site_region)."
            for sid in sorted(unfillable)
        ]
        return RosterSolution(
            status=SolveStatus.INFEASIBLE,
            unfilled_shifts=sorted(unfillable),
            diagnostics=diagnostics,
        )

    epoch = min(s.date for s in shifts)
    hour_bounds = _worker_hour_bounds_minutes(list(worker_by_id), matrix_by_worker)
    travel_conflicts: list[tuple[int, int]] = []
    warnings: list[str] = []
    if travel_minutes is not None:
        travel_conflicts, warnings = _travel_conflicts(
            shifts, candidates_by_shift, epoch, travel_minutes
        )

    model, assign_vars, _ = _build_model(
        workers,
        shifts,
        candidates_by_shift,
        matrix_by_ws,
        hour_bounds,
        epoch,
        allow_unfilled=False,
        travel_conflicts=travel_conflicts,
    )
    cost_terms = [
        round(matrix_by_ws[(w_id, s_id)].pay_cost * _PAY_COST_SCALE) * var
        for (w_id, s_id), var in assign_vars.items()
    ]
    model.Minimize(sum(cost_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time_in_seconds
    status = solver.Solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments = []
        total_cost = 0.0
        for (worker_id, shift_id), var in assign_vars.items():
            if solver.Value(var) == 1:
                entry = matrix_by_ws[(worker_id, shift_id)]
                assignments.append(
                    Assignment(
                        worker_id=worker_id,
                        shift_id=shift_id,
                        day=shift_by_id[shift_id].date,
                        pay_cost=entry.pay_cost,
                    )
                )
                total_cost += entry.pay_cost
        solve_status = SolveStatus.OPTIMAL if status == cp_model.OPTIMAL else SolveStatus.FEASIBLE
        return RosterSolution(
            status=solve_status,
            assignments=assignments,
            total_cost=round(total_cost, 2),
            warnings=warnings,
        )

    # Every shift has >=1 eligible/skilled candidate, yet no feasible
    # assignment exists -- the conflict is in NoOverlap and/or min/max hours.
    # Run a relaxed diagnostic solve to identify which shifts are the
    # sticking point.
    unfilled = _diagnose_infeasibility(
        workers,
        shifts,
        candidates_by_shift,
        matrix_by_ws,
        hour_bounds,
        epoch,
        max_time_in_seconds,
        travel_conflicts,
    )
    diagnostics = [
        f"Shift {sid}: could not be covered without violating a worker's "
        "no-double-booking, travel-time-between-sites or min/max-hours constraint "
        "(all eligible/skilled candidates are ruled out once those constraints are applied)."
        for sid in unfilled
    ]
    return RosterSolution(
        status=SolveStatus.INFEASIBLE,
        unfilled_shifts=unfilled,
        diagnostics=diagnostics,
        warnings=warnings,
    )
