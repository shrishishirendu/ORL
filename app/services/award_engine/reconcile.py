"""Post-solve cost reconciliation for Tier 1 rosters.

After a Tier 1 solve, ``Roster.total_cost`` is the solver's *estimate* -- the
sum of per-cell ``AwardCostMatrix.pay_cost`` values, a linear approximation
of a cost that really depends on the whole week (docs/AWARD_INTEGRATION.md).
This module asks the Award Engine service for the exact full-period figure
(``POST /engine/price-roster``) and stores it next to the estimate, in
``Roster.engine_total_cost`` / ``engine_commit``, with ``cost_status`` saying
which number to trust (see ``RosterCostStatus`` for every value).

Decision table (first match wins), for a ``SOLVED`` roster:

1. No assignments -> nothing to price; ``cost_status`` left ``NULL``.
2. Engine not configured -> ``placeholder_estimate`` if any assignment was
   costed off a placeholder ``AwardCostMatrix`` row, else ``NULL``
   (not reconciled -- the pre-integration behaviour).
3. Some assigned worker has no award fields -> ``placeholder_estimate``; the
   engine is **not** called for a partial total (a total covering only some
   workers would be exactly the silent mix the contract forbids).
4. Engine call fails: unreachable/5xx/timeout/pin mismatch ->
   ``engine_unavailable``; 422 -> ``engine_rejected``. Pre-flight
   (``matrix.award_field_problems`` against ``GET /engine/awards``) catches
   the 422 cases ORL can see coming and records them the same way, with the
   offending workers named, without sending the doomed request.
5. Engine answers with ``total_cost: null`` -> ``engine_unresolved``
   (``cost_detail`` lists the unresolved workers and their issues).
6. Otherwise -> ``engine_exact`` with ``engine_total_cost`` set. If the
   solve's assignments used placeholder rows, ``cost_detail`` says so: the
   total is exact, but the solver chose the roster on placeholder numbers.
   Any v1.1 ``release_blocking_gaps`` the engine reported are listed there
   too (they don't block the figure -- they are shown, not hidden).

``FAILED`` rosters are left untouched. Every path is deterministic given
the DB state and the engine's (deterministic) answer. The roster row itself
is already committed before this runs, and the caller
(``app/workers/tasks.py``) swallows any unexpected exception here, so
reconciliation can never fail a solve.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.award_cost_matrix import AwardCostMatrix
from app.models.enums import RosterCostStatus, RosterStatus
from app.models.roster import Roster, RosterAssignment
from app.models.shift import Shift
from app.models.worker import Worker
from app.services.award_engine.client import (
    AwardEngineClient,
    AwardEngineError,
    AwardEngineRejected,
    AwardEngineUnavailable,
    EngineContext,
    PriceRosterRequest,
    PriceRosterResponse,
    RosterWorkerAssignments,
    context_from_settings,
)
from app.services.award_engine.matrix import (
    award_field_problems,
    fetch_level_keys,
    has_award_fields,
    shift_sort_key,
    to_engine_shift,
    to_engine_worker,
)


@dataclass
class ReconcileOutcome:
    roster_id: int
    cost_status: RosterCostStatus | None
    engine_total_cost: Decimal | None = None
    engine_commit: str | None = None
    cost_detail: str | None = None


def build_price_roster_requests(roster: Roster, context: EngineContext) -> list[PriceRosterRequest]:
    """One ``price-roster`` request per award code among the assigned
    workers (sorted), workers by id, each worker's shifts chronologically.
    ``roster.assignments`` must have ``.worker`` and ``.shift.site`` loaded,
    and every assigned worker must satisfy ``has_award_fields``.
    """
    shifts_by_worker: dict[int, list[Shift]] = defaultdict(list)
    workers: dict[int, Worker] = {}
    for assignment in roster.assignments:
        workers[assignment.worker_id] = assignment.worker
        shifts_by_worker[assignment.worker_id].append(assignment.shift)

    by_code: dict[str, list[Worker]] = defaultdict(list)
    for worker in workers.values():
        by_code[worker.award_code].append(worker)

    requests = []
    for award_code in sorted(by_code):
        code_workers = sorted(by_code[award_code], key=lambda w: w.id)
        requests.append(
            PriceRosterRequest(
                award_code=award_code,
                context=context,
                period_start=roster.period_start,
                period_end=roster.period_end,
                workers=[to_engine_worker(w) for w in code_workers],
                assignments=[
                    RosterWorkerAssignments(
                        worker_id=w.id,
                        shifts=[
                            to_engine_shift(s)
                            for s in sorted(shifts_by_worker[w.id], key=shift_sort_key)
                        ],
                    )
                    for w in code_workers
                ],
            )
        )
    return requests


def _unresolved_detail(responses: list[PriceRosterResponse]) -> str:
    lines = []
    for response in responses:
        by_id = {w.worker_id: w for w in response.workers}
        for worker_id in sorted(response.unresolved_worker_ids):
            issues = by_id[worker_id].issues if worker_id in by_id else []
            lines.append(f"worker {worker_id}: " + ("; ".join(issues) or "unresolved"))
    return "engine could not price: " + (" | ".join(lines) or "total_cost is null")


async def _uses_placeholder_rows(session: AsyncSession, roster: Roster) -> bool:
    keys = [(a.worker_id, a.day, a.shift_id) for a in roster.assignments]
    if not keys:
        return False
    found = (
        await session.execute(
            select(AwardCostMatrix.id)
            .where(
                tuple_(
                    AwardCostMatrix.worker_id, AwardCostMatrix.day, AwardCostMatrix.shift_id
                ).in_(keys),
                AwardCostMatrix.is_placeholder.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


async def reconcile_roster_cost(
    session: AsyncSession,
    roster_id: int,
    client: AwardEngineClient | None,
    *,
    context: EngineContext | None = None,
) -> ReconcileOutcome:
    """Apply the module docstring's decision table to ``roster_id`` and
    commit the result onto the ``Roster`` row.
    """
    roster = (
        await session.execute(
            select(Roster)
            .options(
                selectinload(Roster.assignments).selectinload(RosterAssignment.worker),
                selectinload(Roster.assignments)
                .selectinload(RosterAssignment.shift)
                .selectinload(Shift.site),
            )
            .where(Roster.id == roster_id)
        )
    ).scalar_one()
    outcome = ReconcileOutcome(roster_id=roster_id, cost_status=None)
    if roster.status is not RosterStatus.SOLVED:
        return outcome

    if not roster.assignments:
        outcome.cost_detail = "no assignments to price"
    else:
        uses_placeholders = await _uses_placeholder_rows(session, roster)
        unpriceable = sorted(
            {a.worker_id for a in roster.assignments if not has_award_fields(a.worker)}
        )
        if client is None:
            if uses_placeholders:
                outcome.cost_status = RosterCostStatus.PLACEHOLDER_ESTIMATE
                outcome.cost_detail = (
                    "award engine not configured and the solve used placeholder "
                    "AwardCostMatrix rows; total_cost is the solver estimate only"
                )
        elif unpriceable:
            outcome.cost_status = RosterCostStatus.PLACEHOLDER_ESTIMATE
            outcome.cost_detail = (
                "workers without award fields cannot be engine-priced: "
                + ", ".join(str(w) for w in unpriceable)
                + "; total_cost is the solver estimate only"
            )
        else:
            await _price(roster, client, context or context_from_settings(), outcome)
            if outcome.cost_status is RosterCostStatus.ENGINE_EXACT and uses_placeholders:
                note = (
                    "engine_total_cost is exact, but the solver chose this roster using "
                    "placeholder AwardCostMatrix rows (run the matrix sync, then re-solve)"
                )
                outcome.cost_detail = "; ".join(filter(None, [note, outcome.cost_detail]))

    roster.cost_status = outcome.cost_status
    roster.engine_total_cost = outcome.engine_total_cost
    roster.engine_commit = outcome.engine_commit
    roster.cost_detail = outcome.cost_detail
    await session.commit()
    return outcome


async def _price(
    roster: Roster,
    client: AwardEngineClient,
    context: EngineContext,
    outcome: ReconcileOutcome,
) -> None:
    try:
        level_keys = await fetch_level_keys(client)
        workers = sorted({a.worker_id: a.worker for a in roster.assignments}.items())
        problems = [
            f"worker {worker_id}: " + "; ".join(found)
            for worker_id, worker in workers
            if (found := award_field_problems(worker, level_keys))
        ]
        if problems:
            outcome.cost_status = RosterCostStatus.ENGINE_REJECTED
            outcome.cost_detail = "pre-flight, not sent to the engine: " + " | ".join(problems)
            return
        requests = build_price_roster_requests(roster, context)
        responses = [await client.price_roster(request) for request in requests]
    except AwardEngineRejected as exc:
        outcome.cost_status = RosterCostStatus.ENGINE_REJECTED
        outcome.cost_detail = str(exc)
        return
    except AwardEngineUnavailable as exc:
        outcome.cost_status = RosterCostStatus.ENGINE_UNAVAILABLE
        outcome.cost_detail = f"award engine unavailable: {exc}"
        return
    except AwardEngineError as exc:
        outcome.cost_status = RosterCostStatus.ENGINE_UNAVAILABLE
        outcome.cost_detail = f"award engine error: {exc}"
        return

    commits = sorted({r.engine_commit for r in responses})
    outcome.engine_commit = ",".join(commits)[:64] or None
    if any(r.total_cost is None for r in responses):
        outcome.cost_status = RosterCostStatus.ENGINE_UNRESOLVED
        outcome.cost_detail = _unresolved_detail(responses)
        return
    # Summing per-award-code totals is bookkeeping across separate engine
    # answers, not a pay calculation; with one award code (the demo's
    # MA000016 scope) it is the engine's figure verbatim.
    outcome.engine_total_cost = sum((r.total_cost for r in responses), Decimal("0"))
    outcome.cost_status = RosterCostStatus.ENGINE_EXACT
    gaps = sorted(
        {
            f"worker {w.worker_id}: {gap}"
            for r in responses
            for w in r.workers
            for gap in w.release_blocking_gaps
        }
    )
    if gaps:
        outcome.cost_detail = "release-blocking gaps (priced anyway): " + " | ".join(gaps)
