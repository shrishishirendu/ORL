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
from fastapi import APIRouter, Depends

from app.api._jobs import get_job_status
from app.core.queue import get_arq_redis
from app.schemas.events import EventRequest
from app.schemas.jobs import JobEnqueuedResponse, JobStatusResponse

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=JobEnqueuedResponse)
async def submit_event(
    event: EventRequest, redis: ArqRedis = Depends(get_arq_redis)
) -> JobEnqueuedResponse:
    """Validate `event` against one of the three Tier 3 event shapes
    (discriminated on ``event_type``) and enqueue it for
    ``handle_reoptimization_event_task``.
    """
    job = await redis.enqueue_job(
        "handle_reoptimization_event_task", event.model_dump(mode="json")
    )
    assert job is not None
    return JobEnqueuedResponse(job_id=job.job_id)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_event_job(
    job_id: str, redis: ArqRedis = Depends(get_arq_redis)
) -> JobStatusResponse:
    """Poll a previously-enqueued Tier 3 event job's status/result."""
    return await get_job_status(redis, job_id)
