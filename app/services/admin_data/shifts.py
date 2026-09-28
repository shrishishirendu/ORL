"""Shift create/update/bulk-replace -- the DB-facing boundary behind
``POST``/``PATCH /shifts`` and the Shifts half of
``POST /admin/data/upload``.

Like ``app/services/admin_data/workers.py``, this module never commits --
callers control the transaction (single-row endpoints commit once they've
also run placeholder-eligibility generation for the same shift; the upload
path commits or rolls back the whole multi-row batch at once).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import time as time_

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.award_cost_matrix import AwardCostMatrix
from app.models.job import Job
from app.models.roster import RosterAssignment
from app.models.shift import Shift
from app.models.site import Site
from app.services.admin_data.eligibility import generate_for_shift
from app.services.admin_data.errors import (
    MultiStopNotSupportedError,
    ShiftNotFoundError,
    SiteNotFoundError,
)


@dataclass
class ShiftCreateData:
    """Validated input for ``create_shift``, mirroring
    ``app.schemas.shifts.ShiftCreateRequest``.
    """

    date: date_
    start_time: time_
    end_time: time_
    required_skill: str
    site_id: int
    is_multi_stop: bool = False


async def create_shift(session: AsyncSession, data: ShiftCreateData) -> tuple[Shift, int]:
    """Create one single-site Shift and, in the same transaction, run
    placeholder-eligibility generation for it (see
    ``app.services.admin_data.eligibility``). Returns
    ``(shift, placeholder_award_rows_created)``.

    Raises ``MultiStopNotSupportedError`` if ``data.is_multi_stop`` -- Jobs
    are out of scope for this admin surface (see that error's docstring),
    and ``SiteNotFoundError`` if ``site_id`` doesn't resolve.
    """
    if data.is_multi_stop:
        raise MultiStopNotSupportedError()
    site = (await session.execute(select(Site).where(Site.id == data.site_id))).scalar_one_or_none()
    if site is None:
        raise SiteNotFoundError(site_id=data.site_id)

    shift = Shift(
        date=data.date,
        start_time=data.start_time,
        end_time=data.end_time,
        required_skill=data.required_skill,
        site_id=data.site_id,
        is_multi_stop=False,
    )
    session.add(shift)
    await session.flush()

    created = await generate_for_shift(session, shift)
    return shift, created


async def update_shift(session: AsyncSession, shift_id: int, updates: dict) -> tuple[Shift, int]:
    """Partially update Shift ``shift_id``, then re-run placeholder
    generation for it unconditionally (skip-if-exists makes this cheap/safe
    on every successful update, per the task brief, since a
    ``required_skill``/``site_id`` change is exactly what could open up new
    eligible workers). Returns ``(shift, placeholder_award_rows_created)``.

    Raises ``MultiStopNotSupportedError`` if the update would set
    ``is_multi_stop=True``, ``ShiftNotFoundError``/``SiteNotFoundError`` as
    their names say.
    """
    if updates.get("is_multi_stop") is True:
        raise MultiStopNotSupportedError()

    shift = (await session.execute(select(Shift).where(Shift.id == shift_id))).scalar_one_or_none()
    if shift is None:
        raise ShiftNotFoundError(shift_id)

    if updates.get("site_id") is not None:
        site = (
            await session.execute(select(Site).where(Site.id == updates["site_id"]))
        ).scalar_one_or_none()
        if site is None:
            raise SiteNotFoundError(site_id=updates["site_id"])

    for field_name, value in updates.items():
        setattr(shift, field_name, value)

    await session.flush()
    created = await generate_for_shift(session, shift)
    return shift, created


@dataclass
class ShiftUploadRow:
    """One validated Shifts row from a bulk upload. Mirrors the "Shifts
    sheet/CSV columns" shape from the task brief. ``is_multi_stop`` is
    intentionally absent here -- a row with it true never survives
    validation (see ``app.services.admin_data.upload``), so every row that
    reaches this dataclass is single-site by construction.
    """

    date: date_
    start_time: time_
    end_time: time_
    required_skill: str
    site_code: str


@dataclass
class SkippedShift:
    """One existing Shift in the replacement period that was left alone,
    with why. ``reason`` is ``"already_rostered"`` (it has >=1
    ``RosterAssignment``) or ``"has_real_award_data"`` (it has >=1
    non-placeholder ``AwardCostMatrix`` row with no RosterAssignment yet --
    a genuine anomaly worth flagging to an admin, not something this
    endpoint silently deletes or overwrites -- see
    ``replace_shifts_for_period``'s docstring).
    """

    id: int
    date: date_
    site_code: str
    site_name: str
    reason: str


@dataclass
class ShiftsReplaceSummary:
    """What the API/frontend needs to render a "what happened" report for
    the Shifts half of a bulk upload.
    """

    period_start: date_
    period_end: date_
    created: int = 0
    deleted: int = 0
    placeholder_award_rows_created: int = 0
    shifts_skipped: list[SkippedShift] = field(default_factory=list)


async def replace_shifts_for_period(
    session: AsyncSession,
    period_start: date_,
    period_end: date_,
    rows: Sequence[ShiftUploadRow],
    sites_by_code: dict[str, Site],
) -> ShiftsReplaceSummary:
    """Full-replace semantics for a Shifts upload, scoped to
    ``period_start..period_end`` (both inclusive).

    Within that period, delete only Shift rows that have **zero**
    ``RosterAssignment`` rows (not yet rostered) AND **zero** real
    (``is_placeholder=False``) ``AwardCostMatrix`` rows, then insert the
    uploaded set. A shift with a RosterAssignment is left untouched (it's
    already committed to a roster); a shift with real award data but no
    RosterAssignment is a genuine anomaly (real award-interpreted pay data
    sitting on a shift nobody's been assigned to yet is plausible, but not
    something to silently delete) -- both are reported back in
    ``shifts_skipped`` rather than acted on, so an admin can sort them out
    manually. Deleting an eligible shift also cascades its ``Job`` rows (if
    it happened to be a pre-existing multi-stop shift with none of the
    above blockers) and its (necessarily placeholder-only, by the same
    guard) ``AwardCostMatrix`` rows, since neither table has an
    ``ON DELETE CASCADE`` at the DB level (see the original migration --
    FKs here are plain, unqualified references) and leaving them behind
    would violate their own FK constraints in the Job case or orphan
    placeholder rows in the AwardCostMatrix case.

    After inserting the new Shift rows, placeholder-eligibility generation
    runs for each one (same as the single-shift ``create_shift`` path).

    ``sites_by_code`` must already contain every ``row.site_code``
    referenced (resolved/validated by the caller, same as
    ``upsert_workers_from_rows``).
    """
    existing = (
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

    summary = ShiftsReplaceSummary(period_start=period_start, period_end=period_end)

    ra_shift_ids: set[int] = set()
    real_award_shift_ids: set[int] = set()
    existing_ids = [s.id for s in existing]
    if existing_ids:
        ra_rows = (
            (
                await session.execute(
                    select(RosterAssignment.shift_id)
                    .where(RosterAssignment.shift_id.in_(existing_ids))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        ra_shift_ids = set(ra_rows)

        real_award_rows = (
            (
                await session.execute(
                    select(AwardCostMatrix.shift_id)
                    .where(
                        AwardCostMatrix.shift_id.in_(existing_ids),
                        AwardCostMatrix.is_placeholder.is_(False),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        real_award_shift_ids = set(real_award_rows)

    deletable_ids: list[int] = []
    for shift in existing:
        if shift.id in ra_shift_ids:
            summary.shifts_skipped.append(
                SkippedShift(
                    shift.id, shift.date, shift.site.code, shift.site.name, "already_rostered"
                )
            )
        elif shift.id in real_award_shift_ids:
            summary.shifts_skipped.append(
                SkippedShift(
                    shift.id, shift.date, shift.site.code, shift.site.name, "has_real_award_data"
                )
            )
        else:
            deletable_ids.append(shift.id)

    if deletable_ids:
        await session.execute(delete(Job).where(Job.shift_id.in_(deletable_ids)))
        await session.execute(
            delete(AwardCostMatrix).where(AwardCostMatrix.shift_id.in_(deletable_ids))
        )
        await session.execute(delete(Shift).where(Shift.id.in_(deletable_ids)))
        await session.flush()
        summary.deleted = len(deletable_ids)

    new_shifts: list[Shift] = []
    for row in rows:
        site = sites_by_code[row.site_code]
        shift = Shift(
            date=row.date,
            start_time=row.start_time,
            end_time=row.end_time,
            required_skill=row.required_skill,
            site_id=site.id,
            is_multi_stop=False,
        )
        session.add(shift)
        new_shifts.append(shift)

    if new_shifts:
        await session.flush()
        summary.created = len(new_shifts)
        for shift in new_shifts:
            summary.placeholder_award_rows_created += await generate_for_shift(session, shift)

    return summary
