"""Read-only roster listing/detail: `GET /rosters`, `GET /rosters/{id}`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_session
from app.models.roster import Roster, RosterAssignment
from app.models.shift import Shift
from app.schemas.rosters import RosterDetail, RosterListResponse, RosterSummary

router = APIRouter(tags=["rosters"])


@router.get("/rosters", response_model=RosterListResponse)
async def list_rosters(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> RosterListResponse:
    """List rosters (id, period, status, total_cost, failure_reason),
    most-recently-generated first.
    """
    total = (await session.execute(select(func.count()).select_from(Roster))).scalar_one()
    rows = (
        (
            await session.execute(
                select(Roster)
                .order_by(Roster.generated_at.desc(), Roster.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return RosterListResponse(
        items=[RosterSummary.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/rosters/{roster_id}", response_model=RosterDetail)
async def get_roster(roster_id: int, session: AsyncSession = Depends(get_session)) -> RosterDetail:
    """One roster plus its `RosterAssignment` rows (worker, shift, day)."""
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
    ).scalar_one_or_none()
    if roster is None:
        raise HTTPException(status_code=404, detail=f"Roster {roster_id} not found")
    return RosterDetail.model_validate(roster)
