"""Placeholder ``AwardCostMatrix`` generation -- the ORL-side bridge over
the AwardCostMatrix boundary for newly admin-entered Workers/Shifts.

**Why this exists.** ARCHITECTURE.md's "AwardCostMatrix boundary" section
is a locked architectural principle: ORL never computes pay or eligibility
itself, and ``AwardCostMatrix`` is consumed-only, owned by the external
Award Interpretation Engine. The admin data-entry feature this module
belongs to (see ``app/services/admin_data/``) only covers Workers and
Shifts -- not Award Cost Matrix, which stays out of scope, per the task
brief, as a directly-editable entity. That leaves a gap: a Worker/Shift
pair entered through this admin surface has no real award-interpreted row
yet, so Tier 1 would never be able to roster them (see
``app/services/rostering/solver.py``'s eligibility filter -- a shift with
no eligible candidate is infeasible by construction). Leaving that gap
unaddressed would make the whole admin feature a dead end for a working
demo/MVP; building a full Award Cost Matrix editor to close it would blow
past this round's explicitly agreed scope. The compromise, decided
explicitly rather than invented here: auto-generate a clearly-flagged
**placeholder** row (``AwardCostMatrix.is_placeholder=True`` -- see that
column's docstring in ``app/models/award_cost_matrix.py``) using a flat
rate (``Settings.placeholder_hourly_rate``) instead of real award data.
This is a deliberate, acknowledged compromise on the boundary for the sake
of a working demo/MVP -- not a reversal of it: ORL is still not
*interpreting* pay/eligibility rules, it's stamping a stopgap number on a
row that would otherwise not exist at all, and the column exists precisely
so that stopgap is never mistaken for real award data.

**Trigger point.** Only Shift creation/update (see ``app/api/shifts.py``)
triggers this automatically, never Worker creation. Retroactively scanning
every historical/future Shift each time a new Worker is added is unbounded
work with a murky semantics question attached (should a worker added today
really get auto-eligibility-backfilled onto a shift three months ago?).
Instead, adding/bulk-uploading workers leaves eligibility as-is, and
``POST /workers/regenerate-eligibility`` (``regenerate_eligibility`` below)
is the explicit, admin-triggered "catch up eligibility for this period" action
-- run after a worker-roster change, or any time, safely, since it shares
the exact same skip-if-existing-row rule as the Shift-create path.

**Region resolution and skill/region matching mirror Tier 1's own rules
exactly**, on purpose: this module computes the same "would Tier 1 consider
this worker eligible for this shift" set that
``app.services.rostering.solver.solve_roster`` computes, so a placeholder
row never makes a worker "eligible" here that Tier 1's own filter would
reject (or vice versa). ``_effective_region`` is imported directly from
``app.services.rostering.service`` (not reimplemented) for exactly this
reason; ``_region_compatible`` below mirrors the nested function of the
same name inside ``app.services.rostering.solver.solve_roster`` (module
docstring point 5: a ``None`` on either side means "region unknown", not a
mismatch) -- it can't be imported directly since that one is a closure, so
it is kept here as a small, deliberately identical copy rather than a
divergent reimplementation. Likewise, shift-hours are computed via
``app.services.rostering.solver._shift_window`` (imported, not
re-derived), the same overnight-aware ``end_time <= start_time`` handling
described in that module's docstring point 3, so a placeholder row's
implied hours can never quietly disagree with what Tier 1 itself would
compute for the same shift.

Neither of these imports/mirrors modifies
``app/services/rostering/solver.py`` or ``.../service.py`` in any way --
this module only reads their pure helper functions.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date as date_

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.settings import settings
from app.models.award_cost_matrix import AwardCostMatrix
from app.models.shift import Shift
from app.models.worker import Worker
from app.services.rostering.service import _effective_region
from app.services.rostering.solver import ShiftInput, _shift_window


def _region_compatible(worker_region: str | None, shift_region: str | None) -> bool:
    """Mirrors the nested ``_region_compatible`` inside
    ``app.services.rostering.solver.solve_roster`` -- see this module's
    docstring for why it's a deliberate copy rather than an import.
    """
    if worker_region is None or shift_region is None:
        return True
    return worker_region == shift_region


def _shift_hours(shift: Shift) -> float:
    """Overnight-aware shift duration in hours, via the rostering solver's
    own ``_shift_window`` (see this module's docstring) rather than a
    re-derivation of the same start/end-time arithmetic.
    """
    shift_input = ShiftInput(
        id=shift.id,
        date=shift.date,
        start_time=shift.start_time,
        end_time=shift.end_time,
        required_skill=shift.required_skill,
    )
    _, duration_minutes = _shift_window(shift_input, shift.date)
    return duration_minutes / 60


async def _load_active_workers(session: AsyncSession) -> list[Worker]:
    rows = (
        await session.execute(
            select(Worker)
            .options(selectinload(Worker.home_site))
            .where(Worker.active.is_(True))
        )
    ).scalars().all()
    return list(rows)


async def _generate(
    session: AsyncSession, shifts: Sequence[Shift], workers: Sequence[Worker]
) -> int:
    """Shared core of both entry points below.

    For every (shift, worker) pair that is skill+region eligible (the exact
    filter Tier 1's own ``solve_roster`` applies -- see module docstring)
    and has no existing ``AwardCostMatrix`` row yet for
    ``(worker_id, day=shift.date, shift_id)``, insert one placeholder row.
    Rows that already exist are **never** touched, whatever their own
    ``is_placeholder`` value -- a real, Award-Interpretation-Engine-sourced
    row is never clobbered by this stopgap.

    ``shifts`` must have ``.site`` eager-loaded and ``workers`` must have
    ``.home_site`` eager-loaded (both callers below arrange this) -- reading
    either lazily on an async session would raise.
    """
    if not shifts or not workers:
        return 0

    shift_ids = [s.id for s in shifts]
    worker_ids = [w.id for w in workers]
    existing_rows = (
        await session.execute(
            select(AwardCostMatrix.worker_id, AwardCostMatrix.shift_id).where(
                AwardCostMatrix.shift_id.in_(shift_ids),
                AwardCostMatrix.worker_id.in_(worker_ids),
            )
        )
    ).all()
    existing_keys = {(worker_id, shift_id) for worker_id, shift_id in existing_rows}

    hourly_rate = settings.placeholder_hourly_rate
    created = 0
    for shift in shifts:
        shift_region = shift.site.region if shift.site is not None else None
        candidates = [w for w in workers if shift.required_skill in w.skills]
        for worker in candidates:
            key = (worker.id, shift.id)
            if key in existing_keys:
                continue
            if not _region_compatible(_effective_region(worker), shift_region):
                continue
            hours = _shift_hours(shift)
            session.add(
                AwardCostMatrix(
                    worker_id=worker.id,
                    day=shift.date,
                    shift_id=shift.id,
                    pay_cost=round(hourly_rate * hours, 2),
                    eligible=True,
                    min_hours=None,
                    max_hours=None,
                    is_placeholder=True,
                )
            )
            existing_keys.add(key)
            created += 1

    if created:
        await session.flush()
    return created


async def generate_for_shift(session: AsyncSession, shift: Shift) -> int:
    """Placeholder-generate for exactly one newly created/updated Shift,
    against every currently active Worker. This is what
    ``POST /shifts``/``PATCH /shifts/{id}`` call synchronously in the same
    request/transaction (see ``app/services/admin_data/shifts.py``).

    Re-selects `shift` with ``.site`` eager-loaded via its id -- the caller
    may hold a `Shift` instance whose `.site` relationship was never
    populated (e.g. one built via `Shift(...)` and just flushed), and
    thanks to SQLAlchemy's identity map this re-select returns the exact
    same Python object, just with `.site` now loaded, rather than a
    disconnected duplicate.
    """
    reloaded = (
        await session.execute(
            select(Shift).options(selectinload(Shift.site)).where(Shift.id == shift.id)
        )
    ).scalar_one()
    workers = await _load_active_workers(session)
    return await _generate(session, [reloaded], workers)


async def regenerate_eligibility(
    session: AsyncSession, period_start: date_, period_end: date_
) -> int:
    """``POST /workers/regenerate-eligibility``'s core: re-run the same
    matching logic against every Shift in ``period_start..period_end`` and
    every currently active Worker, filling in only missing rows
    (skip-if-exists, same rule as ``generate_for_shift``). Safe to run any
    time as a "make sure eligibility is caught up" action -- it never
    touches an existing row.
    """
    shifts = (
        await session.execute(
            select(Shift)
            .options(selectinload(Shift.site))
            .where(Shift.date >= period_start, Shift.date <= period_end)
        )
    ).scalars().all()
    workers = await _load_active_workers(session)
    return await _generate(session, list(shifts), workers)
