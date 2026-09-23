"""Workers API: `GET /workers` (read), plus the admin data-entry additions
`POST /workers`, `PATCH /workers/{id}`, and
`POST /workers/regenerate-eligibility`.

The `GET` listing predates this feature (see the task brief's Part 1); the
rest is new. Kept thin -- parse request, call
`app.services.admin_data.workers`/`.eligibility`, shape the response --
matching `app/services/rostering/service.py`'s router being the thin layer
over the actual DB-facing logic.
"""

from __future__ import annotations

from datetime import date as date_

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_session
from app.models.worker import Worker
from app.schemas.workers import (
    RegenerateEligibilityResponse,
    WorkerCreateRequest,
    WorkerListResponse,
    WorkerRead,
    WorkerUpdateRequest,
)
from app.services.admin_data.eligibility import regenerate_eligibility
from app.services.admin_data.errors import (
    DuplicateEmployeeCodeError,
    SiteNotFoundError,
    WorkerNotFoundError,
)
from app.services.admin_data.workers import (
    WorkerCreateData,
    create_worker,
    update_worker,
)

router = APIRouter(tags=["workers"])


async def _load_worker_read(session: AsyncSession, worker_id: int) -> WorkerRead:
    """Re-select `worker_id` with `.home_site` eager-loaded, so
    `WorkerRead.model_validate` never hits a lazy-load on the async
    session -- the `Worker` instance the service layer hands back may not
    have that relationship populated (e.g. a freshly created row).
    """
    worker = (
        await session.execute(
            select(Worker).options(selectinload(Worker.home_site)).where(Worker.id == worker_id)
        )
    ).scalar_one()
    return WorkerRead.model_validate(worker)


@router.get("/workers", response_model=WorkerListResponse)
async def list_workers(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> WorkerListResponse:
    """List workers (id, name, skills, region, home_site, employee_code),
    newest-id-last.

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


@router.post("/workers", response_model=WorkerRead, status_code=201)
async def create_worker_endpoint(
    payload: WorkerCreateRequest, session: AsyncSession = Depends(get_session)
) -> WorkerRead:
    """Create one Worker. 409 on a duplicate `employee_code`, 422 if
    `home_site_id` doesn't reference an existing Site.
    """
    try:
        worker = await create_worker(session, WorkerCreateData(**payload.model_dump()))
    except DuplicateEmployeeCodeError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SiteNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    return await _load_worker_read(session, worker.id)


@router.patch("/workers/{worker_id}", response_model=WorkerRead)
async def update_worker_endpoint(
    worker_id: int, payload: WorkerUpdateRequest, session: AsyncSession = Depends(get_session)
) -> WorkerRead:
    """Partially update Worker `worker_id`. 404 if it doesn't exist, 409 on
    a duplicate `employee_code`, 422 if `home_site_id` doesn't reference an
    existing Site.
    """
    updates = payload.model_dump(exclude_unset=True)
    try:
        worker = await update_worker(session, worker_id, updates)
    except WorkerNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DuplicateEmployeeCodeError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SiteNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    return await _load_worker_read(session, worker.id)


@router.post("/workers/regenerate-eligibility", response_model=RegenerateEligibilityResponse)
async def regenerate_eligibility_endpoint(
    period_start: date_ = Query(...),
    period_end: date_ = Query(...),
    session: AsyncSession = Depends(get_session),
) -> RegenerateEligibilityResponse:
    """Re-run placeholder-eligibility generation (see
    `app.services.admin_data.eligibility`) against every Shift in
    `period_start..period_end` and every currently active Worker, filling
    in any missing rows. Run this after a worker-roster change (e.g. a
    bulk Workers upload) to backfill coverage for a period -- safe to run
    any time, since it never touches an existing `AwardCostMatrix` row.
    """
    created = await regenerate_eligibility(session, period_start, period_end)
    await session.commit()
    return RegenerateEligibilityResponse(
        period_start=period_start, period_end=period_end, placeholder_award_rows_created=created
    )
