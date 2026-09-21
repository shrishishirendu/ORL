"""Tier 3 re-optimization API: submit an event, poll its job status.

Per ARCHITECTURE.md's Tier 3 section, events reach the API layer live
("job cancelled / worker sick / visit overran ... reaching the API layer")
and are handled event-driven, on the order of seconds. They are still
queued (see ``app/workers/tasks.py``'s module docstring on why) rather than
handled inline -- consistency with Tiers 1/2, not because Tier 3 itself is
slow.
"""

from __future__ import annotations

from arq import ArqRedis
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._jobs import get_job_status
from app.core.db import get_session
from app.core.queue import get_arq_redis
from app.models.enums import ReoptimizationStatus
from app.models.reoptimization import ReoptimizationEvent
from app.schemas.events import EventListResponse, EventRead, EventRequest
from app.schemas.jobs import JobEnqueuedResponse, JobStatusResponse

router = APIRouter(prefix="/events", tags=["events"])

# A "resolved" event is one Tier 3 has finished handling, whichever way --
# scoped-resolved or escalated to Tier 1 -- as opposed to one still
# OPEN/RESOLVING. See `EventRead`'s docstring / app/models/enums.py.
_RESOLVED_STATUSES = (ReoptimizationStatus.RESOLVED, ReoptimizationStatus.ESCALATED)


@router.post("", response_model=JobEnqueuedResponse)
async def submit_event(
    event: EventRequest, redis: ArqRedis = Depends(get_arq_redis)
) -> JobEnqueuedResponse:
    """Validate `event` against one of the three Tier 3 event shapes
    (discriminated on ``event_type``) and enqueue it for
    ``handle_reoptimization_event_task``.
    """
    job = await redis.enqueue_job("handle_reoptimization_event_task", event.model_dump(mode="json"))
    assert job is not None
    return JobEnqueuedResponse(job_id=job.job_id)


@router.get("", response_model=EventListResponse)
async def list_events(
    shift_id: int | None = Query(None),
    resolved: bool | None = Query(
        None,
        description=(
            "True: status is RESOLVED or ESCALATED (Tier 3 finished handling it). "
            "False: status is OPEN or RESOLVING (not yet finished). Omit for both."
        ),
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> EventListResponse:
    """List `ReoptimizationEvent` rows, most-recent-first, filterable by
    `shift_id` and/or `resolved`.
    """
    stmt = select(ReoptimizationEvent)
    count_stmt = select(func.count()).select_from(ReoptimizationEvent)
    if shift_id is not None:
        stmt = stmt.where(ReoptimizationEvent.shift_id == shift_id)
        count_stmt = count_stmt.where(ReoptimizationEvent.shift_id == shift_id)
    if resolved is not None:
        condition = (
            ReoptimizationEvent.status.in_(_RESOLVED_STATUSES)
            if resolved
            else ReoptimizationEvent.status.notin_(_RESOLVED_STATUSES)
        )
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = (await session.execute(count_stmt)).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(ReoptimizationEvent.occurred_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return EventListResponse(
        items=[EventRead.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_event_job(job_id: str, redis: ArqRedis = Depends(get_arq_redis)) -> JobStatusResponse:
    """Poll a previously-enqueued Tier 3 event job's status/result."""
    return await get_job_status(redis, job_id)
