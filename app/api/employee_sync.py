"""Employee sync endpoint: `POST /workers/sync-employees`.

Synchronous (not enqueued): it is one HTTP read from award-intelligence and
a single pass over the workers table, with no solver or engine calculation.
Thin layer over `app.services.employee_sync.service`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.settings import settings
from app.schemas.employee_sync import SyncEmployeesRequest, SyncEmployeesResponse
from app.services.employee_sync import client as client_module
from app.services.employee_sync.client import EmployeeMasterError, EmployeeMasterUnavailable
from app.services.employee_sync.service import sync_employees

router = APIRouter(tags=["employee-sync"])


@router.post("/workers/sync-employees", response_model=SyncEmployeesResponse)
async def sync_employees_endpoint(
    payload: SyncEmployeesRequest | None = None, session: AsyncSession = Depends(get_session)
) -> SyncEmployeesResponse:
    """Compare ORL workers with the award-intelligence employee master.

    Preview by default. With `apply: true`, blank ORL fields are filled;
    values ORL already holds are never changed, only reported as conflicts.
    503 when award-intelligence isn't configured (`AWARD_INTELLIGENCE_URL`)
    or can't be reached; 502 when it answers without a usable employee
    master (bad token, no payroll import yet, wrong schema).
    """
    apply = bool(payload and payload.apply)
    client = client_module.client_from_settings()
    if client is None:
        raise HTTPException(status_code=503, detail="award-intelligence not configured")
    try:
        report = await sync_employees(session, client, apply=apply, jurisdiction=settings.award_jurisdiction)
    except EmployeeMasterUnavailable as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except EmployeeMasterError as exc:
        await session.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if apply:
        await session.commit()
    return report
