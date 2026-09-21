"""DB-facing boundary for Tier 1 rostering -- NOT WIRED UP YET.

This module documents, but does not implement, the function that will sit
between ``app.core.db`` and the pure solver in ``solver.py``. Wiring it up
for real is API-routing/task-queue work (dispatching a batch solve onto the
async task queue per ARCHITECTURE.md's "Supporting components" section),
which is out of scope for the Tier 1 solver task this module was added
under -- deferring it rather than guessing at how the API layer will want to
call this.

Intended shape, once implemented:

    async def solve_and_persist_roster(
        session: AsyncSession,
        period_start: date,
        period_end: date,
    ) -> Roster:
        '''Load inputs, solve, persist the result, return the Roster row.

        Steps:
        1. Query ``Worker`` (``active == True``) and map each row to a
           ``solver.WorkerInput(id=worker.id, skills=frozenset(worker.skills),
           region=worker.region)``.
        2. Query ``Shift`` rows with ``date`` between ``period_start`` and
           ``period_end`` (inclusive), joined to ``Site`` for
           ``site_region``, and map each to a ``solver.ShiftInput``.
        3. Query ``AwardCostMatrix`` rows with ``day`` in the same period,
           restricted to the workers/shifts loaded above, and map each to a
           ``solver.AwardCostMatrixEntry``.
        4. Call ``solver.solve_roster(workers, shifts, matrix)``.
        5. Create a ``Roster`` row for the period:
           - ``status = RosterStatus.SOLVED`` if
             ``result.is_feasible``, else ``RosterStatus.FAILED``.
           - ``total_cost = result.total_cost`` (``None`` when infeasible).
           - ``generated_at = datetime.now(UTC)``.
           When infeasible, ``result.unfilled_shifts`` /
           ``result.diagnostics`` need a home -- there is no column on
           ``Roster`` for them yet (see app/models/roster.py). This needs a
           schema decision (a new nullable ``failure_reason``/``JSONB``
           column, or a separate table) before this function can actually
           record *why* a solve failed, not just that it did. Flagging this
           rather than bolting an underspecified column on silently.
        6. For each ``solver.Assignment``, create a ``RosterAssignment`` row
           (``roster_id``, ``worker_id``, ``day``, ``shift_id``) -- only when
           feasible; an infeasible result persists the ``Roster`` row alone,
           with no assignments.
        7. ``session.add`` everything, ``await session.commit()``, return the
           ``Roster``.

    This should be dispatched from the async task queue (``arq``), not run
    inline on an API request, per ARCHITECTURE.md's note that "Batch Tier 1
    solves ... are dispatched onto the async task queue rather than run
    inline on the request."

Not implemented here: no import of ``app.core.db``/``app.models`` is added
by this module, so it carries no risk of accidentally being half-wired
against a schema shape (in particular the missing failure-reason column
above) this task wasn't scoped to decide.
"""

from __future__ import annotations
