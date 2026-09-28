"""DB-facing boundary for Tier 1 rostering.

Per ``ARCHITECTURE.md``'s Tier 1 section, this module loads the batch
solve's inputs -- active ``Worker`` rows, the period's ``Shift`` rows, and
the ``AwardCostMatrix`` rows covering them -- maps each to the pure
``app.services.rostering.solver`` dataclasses, calls ``solve_roster``, and
persists the result:

- feasible: a ``Roster`` row (``status = SOLVED``, ``total_cost`` set) plus
  one ``RosterAssignment`` row per ``solver.Assignment``.
- infeasible: a ``Roster`` row alone (``status = FAILED``,
  ``failure_reason`` populated from ``RosterSolution.unfilled_shifts`` /
  ``diagnostics`` -- see ``app/models/roster.py``'s ``failure_reason``
  docstring), with no assignment rows.

This should be dispatched from the async task queue (``arq``), not run
inline on an API request -- see ``app/workers/tasks.py``'s
``solve_roster_task``, which is the only caller of
``solve_and_persist_roster`` in this codebase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.award_cost_matrix import AwardCostMatrix
from app.models.enums import RosterStatus
from app.models.roster import Roster, RosterAssignment
from app.models.shift import Shift
from app.models.travel_matrix import TravelMatrixEntry
from app.models.worker import Worker
from app.services.rostering.solver import (
    AwardCostMatrixEntry,
    RosterSolution,
    ShiftInput,
    WorkerInput,
    solve_roster,
)


@dataclass
class RosterSolveOutcome:
    """What the API/task layer needs to report back after a Tier 1 solve."""

    roster_id: int
    status: RosterStatus
    total_cost: float | None = None
    failure_reason: str | None = None
    unfilled_shift_ids: list[int] = field(default_factory=list)
    # Data gaps the solve worked around (e.g. a missing travel time), from
    # ``RosterSolution.warnings``. Reported, not persisted.
    warnings: list[str] = field(default_factory=list)


def _effective_region(worker: Worker) -> str | None:
    """Resolve the region ``WorkerInput.region`` should carry for this worker.

    Per ARCHITECTURE.md's "Home location as depot" section and the
    rostering solver's own module docstring (point 5), a worker's home
    region for Tier 1's eligibility filter is "normally resolved from
    ``home_site_id``'s ``Site.region``" -- so this prefers
    ``worker.home_site.region``, falling back to the coarser, optional
    ``Worker.region`` column (per ``Worker``'s own docstring, a "cheap/
    free-text fallback/override ... when a caller doesn't want to resolve
    it via the FK join") only when the home site itself has no region set.

    FLAG FOR REVIEW: neither ARCHITECTURE.md nor the models pin down which
    of the two wins when both are set and disagree; this module always
    prefers the FK-resolved region as the canonical geographic source. The
    opposite precedence (``Worker.region`` always wins when set) is equally
    defensible from the docstrings alone.
    """
    if worker.home_site is not None and worker.home_site.region is not None:
        return worker.home_site.region
    return worker.region


def _format_failure_reason(result: RosterSolution) -> str:
    lines = [f"unfilled_shifts={result.unfilled_shifts}", *result.diagnostics]
    return "\n".join(lines)


async def solve_and_persist_roster(
    session: AsyncSession,
    period_start: date,
    period_end: date,
    *,
    excluded_worker_ids: frozenset[int] = frozenset(),
) -> RosterSolveOutcome:
    """Load Tier 1 inputs for ``period_start..period_end``, solve, persist.

    Steps (see module docstring): load active workers, the period's shifts
    (joined to ``Site`` for region), the matching ``AwardCostMatrix`` rows,
    call ``solve_roster``, then persist a ``Roster`` (+ ``RosterAssignment``
    rows on success) reflecting the outcome.

    ``excluded_worker_ids`` are left out of the candidate pool for this solve
    only, e.g. a worker who reported sick, when a Tier 3 escalation
    re-rosters their shift's date. Without it the cost objective could hand
    the shift straight back to them. The pure solver is unchanged: the
    workers are simply never loaded as candidates.
    """
    worker_query = (
        select(Worker).options(selectinload(Worker.home_site)).where(Worker.active.is_(True))
    )
    if excluded_worker_ids:
        worker_query = worker_query.where(Worker.id.not_in(excluded_worker_ids))
    worker_rows = (await session.execute(worker_query)).scalars().all()
    workers = [
        WorkerInput(id=w.id, skills=frozenset(w.skills), region=_effective_region(w))
        for w in worker_rows
    ]

    shift_rows = (
        (
            await session.execute(
                select(Shift)
                .options(selectinload(Shift.site))
                .where(Shift.date >= period_start, Shift.date <= period_end)
            )
        )
        .scalars()
        .all()
    )
    shifts = [
        ShiftInput(
            id=s.id,
            date=s.date,
            start_time=s.start_time,
            end_time=s.end_time,
            required_skill=s.required_skill,
            site_id=s.site_id,
            site_region=s.site.region if s.site is not None else None,
        )
        for s in shift_rows
    ]

    matrix: list[AwardCostMatrixEntry] = []
    worker_ids = {w.id for w in workers}
    shift_ids = {s.id for s in shifts}
    if worker_ids and shift_ids:
        matrix_rows = (
            (
                await session.execute(
                    select(AwardCostMatrix).where(
                        AwardCostMatrix.day >= period_start,
                        AwardCostMatrix.day <= period_end,
                        AwardCostMatrix.worker_id.in_(worker_ids),
                        AwardCostMatrix.shift_id.in_(shift_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        matrix = [
            AwardCostMatrixEntry(
                worker_id=m.worker_id,
                day=m.day,
                shift_id=m.shift_id,
                pay_cost=float(m.pay_cost),
                eligible=m.eligible,
                min_hours=float(m.min_hours) if m.min_hours is not None else None,
                max_hours=float(m.max_hours) if m.max_hours is not None else None,
            )
            for m in matrix_rows
        ]

    # Directed travel times between the period's sites, so Tier 1 never gives
    # one worker two shifts at different sites too close together to travel
    # between (solver module docstring point 6).
    site_ids = {s.site_id for s in shifts if s.site_id is not None}
    travel_minutes: dict[tuple[int, int], int] = {}
    if len(site_ids) > 1:
        travel_rows = (
            (
                await session.execute(
                    select(TravelMatrixEntry).where(
                        TravelMatrixEntry.from_site_id.in_(site_ids),
                        TravelMatrixEntry.to_site_id.in_(site_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        travel_minutes = {(t.from_site_id, t.to_site_id): t.travel_minutes for t in travel_rows}

    result: RosterSolution = solve_roster(workers, shifts, matrix, travel_minutes=travel_minutes)

    roster = Roster(
        period_start=period_start,
        period_end=period_end,
        generated_at=datetime.now(UTC),
        status=RosterStatus.SOLVED if result.is_feasible else RosterStatus.FAILED,
        total_cost=result.total_cost if result.is_feasible else None,
        failure_reason=None if result.is_feasible else _format_failure_reason(result),
    )
    session.add(roster)
    await session.flush()  # assign roster.id before creating child rows

    if result.is_feasible:
        for assignment in result.assignments:
            session.add(
                RosterAssignment(
                    roster_id=roster.id,
                    worker_id=assignment.worker_id,
                    day=assignment.day,
                    shift_id=assignment.shift_id,
                )
            )

    await session.commit()

    return RosterSolveOutcome(
        roster_id=roster.id,
        status=roster.status,
        total_cost=roster.total_cost,
        failure_reason=roster.failure_reason,
        unfilled_shift_ids=list(result.unfilled_shifts),
        warnings=list(result.warnings),
    )
