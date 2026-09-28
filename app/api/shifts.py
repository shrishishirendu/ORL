"""Shifts API: `GET /shifts?date_from=&date_to=` (read), plus the admin
data-entry additions `POST /shifts` and `PATCH /shifts/{id}`.
"""

from __future__ import annotations

from datetime import date as date_

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_session
from app.models.award_cost_matrix import AwardCostMatrix
from app.models.shift import Shift
from app.schemas.common import SiteSummary
from app.schemas.shifts import (
    ShiftCreateRequest,
    ShiftCreateResponse,
    ShiftListResponse,
    ShiftRead,
    ShiftUpdateRequest,
    ShiftUpdateResponse,
)
from app.services.admin_data.errors import (
    InvalidBreakError,
    MultiStopNotSupportedError,
    ShiftNotFoundError,
    SiteNotFoundError,
)
from app.services.admin_data.shifts import ShiftCreateData, create_shift, update_shift

router = APIRouter(tags=["shifts"])


def _shift_read_from_row(shift: Shift, eligible_worker_count: int) -> ShiftRead:
    return ShiftRead(
        id=shift.id,
        date=shift.date,
        start_time=shift.start_time,
        end_time=shift.end_time,
        required_skill=shift.required_skill,
        site=SiteSummary.model_validate(shift.site),
        is_multi_stop=shift.is_multi_stop,
        break_minutes=shift.break_minutes,
        break_start=shift.break_start,
        eligible_worker_count=eligible_worker_count,
    )


async def _load_shift_read(session: AsyncSession, shift_id: int) -> ShiftRead:
    """Re-select `shift_id` with `.site` eager-loaded plus its
    eligible-worker count, for the create/update responses (a single row,
    so a plain scalar count is fine here -- `list_shifts` below uses a
    grouped query instead, to avoid N+1 across a whole page).
    """
    row = (
        await session.execute(
            select(Shift, func.count(AwardCostMatrix.id))
            .outerjoin(
                AwardCostMatrix,
                (AwardCostMatrix.shift_id == Shift.id) & (AwardCostMatrix.eligible.is_(True)),
            )
            .options(selectinload(Shift.site))
            .where(Shift.id == shift_id)
            .group_by(Shift.id)
        )
    ).first()
    shift, eligible_count = row
    return _shift_read_from_row(shift, eligible_count)


@router.get("/shifts", response_model=ShiftListResponse)
async def list_shifts(
    date_from: date_ | None = Query(None, description="Inclusive lower bound on Shift.date"),
    date_to: date_ | None = Query(None, description="Inclusive upper bound on Shift.date"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> ShiftListResponse:
    """List shifts, optionally filtered to `date_from..date_to` (both
    inclusive). Each row's `eligible_worker_count` is computed with a
    single grouped `LEFT JOIN` against `AwardCostMatrix` (not a per-row
    follow-up query), so a full page never costs an N+1 -- see
    `app.schemas.shifts.ShiftRead`'s docstring for why this field exists.
    """
    count_stmt = select(func.count()).select_from(Shift)
    if date_from is not None:
        count_stmt = count_stmt.where(Shift.date >= date_from)
    if date_to is not None:
        count_stmt = count_stmt.where(Shift.date <= date_to)
    total = (await session.execute(count_stmt)).scalar_one()

    eligible_counts = (
        select(AwardCostMatrix.shift_id, func.count().label("cnt"))
        .where(AwardCostMatrix.eligible.is_(True))
        .group_by(AwardCostMatrix.shift_id)
        .subquery()
    )
    stmt = (
        select(Shift, func.coalesce(eligible_counts.c.cnt, 0))
        .outerjoin(eligible_counts, eligible_counts.c.shift_id == Shift.id)
        .options(selectinload(Shift.site))
    )
    if date_from is not None:
        stmt = stmt.where(Shift.date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Shift.date <= date_to)

    rows = (
        await session.execute(stmt.order_by(Shift.date, Shift.id).limit(limit).offset(offset))
    ).all()
    return ShiftListResponse(
        items=[_shift_read_from_row(shift, cnt) for shift, cnt in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/shifts", response_model=ShiftCreateResponse, status_code=201)
async def create_shift_endpoint(
    payload: ShiftCreateRequest, session: AsyncSession = Depends(get_session)
) -> ShiftCreateResponse:
    """Create one single-site Shift and, in the same transaction, generate
    placeholder `AwardCostMatrix` rows for every currently-eligible active
    Worker (see `app.services.admin_data.eligibility`). 422 if
    `is_multi_stop=True` (Jobs are out of scope this round -- see
    `MultiStopNotSupportedError`) or if `site_id` doesn't reference an
    existing Site.
    """
    try:
        shift, created = await create_shift(session, ShiftCreateData(**payload.model_dump()))
    except (MultiStopNotSupportedError, InvalidBreakError) as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SiteNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    shift_read = await _load_shift_read(session, shift.id)
    return ShiftCreateResponse(shift=shift_read, placeholder_award_rows_created=created)


@router.patch("/shifts/{shift_id}", response_model=ShiftUpdateResponse)
async def update_shift_endpoint(
    shift_id: int, payload: ShiftUpdateRequest, session: AsyncSession = Depends(get_session)
) -> ShiftUpdateResponse:
    """Partially update Shift `shift_id`, then re-run placeholder
    generation for it (cheap/safe on every update -- see
    `app.services.admin_data.shifts.update_shift`). 404 if it doesn't
    exist, 422 on `is_multi_stop=True` or an unknown `site_id`.
    """
    updates = payload.model_dump(exclude_unset=True)
    try:
        shift, created = await update_shift(session, shift_id, updates)
    except (MultiStopNotSupportedError, InvalidBreakError) as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ShiftNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SiteNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    shift_read = await _load_shift_read(session, shift.id)
    return ShiftUpdateResponse(shift=shift_read, placeholder_award_rows_created=created)
