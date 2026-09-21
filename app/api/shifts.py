"""Read-only shift listing: `GET /shifts?date_from=&date_to=`."""

from __future__ import annotations

from datetime import date as date_

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_session
from app.models.shift import Shift
from app.schemas.shifts import ShiftListResponse, ShiftRead

router = APIRouter(tags=["shifts"])


@router.get("/shifts", response_model=ShiftListResponse)
async def list_shifts(
    date_from: date_ | None = Query(None, description="Inclusive lower bound on Shift.date"),
    date_to: date_ | None = Query(None, description="Inclusive upper bound on Shift.date"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> ShiftListResponse:
    """List shifts, optionally filtered to `date_from..date_to` (both inclusive)."""
    stmt = select(Shift).options(selectinload(Shift.site))
    count_stmt = select(func.count()).select_from(Shift)
    if date_from is not None:
        stmt = stmt.where(Shift.date >= date_from)
        count_stmt = count_stmt.where(Shift.date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Shift.date <= date_to)
        count_stmt = count_stmt.where(Shift.date <= date_to)

    total = (await session.execute(count_stmt)).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(Shift.date, Shift.id).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ShiftListResponse(
        items=[ShiftRead.model_validate(s) for s in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
