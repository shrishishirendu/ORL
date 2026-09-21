"""Read-only worker listing: `GET /workers`.

Part of the ops dashboard's read API (see the task brief's Part 1) -- the
existing API surface (``app/api/rostering.py`` etc.) only ever
solve/enqueues; this and its sibling read routers add the "look at what's in
the DB" half.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_session
from app.models.worker import Worker
from app.schemas.workers import WorkerListResponse, WorkerRead

router = APIRouter(tags=["workers"])


@router.get("/workers", response_model=WorkerListResponse)
async def list_workers(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> WorkerListResponse:
    """List workers (id, name, skills, region, home_site), newest-id-last.

    A sane-default-limit page (100), not a full pagination framework -- see
    the task brief's Part 1.
    """
    total = (await session.execute(select(func.count()).select_from(Worker))).scalar_one()
    rows = (
        (
            await session.execute(
                select(Worker)
                .options(selectinload(Worker.home_site))
                .order_by(Worker.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return WorkerListResponse(
        items=[WorkerRead.model_validate(w) for w in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
