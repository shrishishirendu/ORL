"""Tier 1 rostering API: enqueue a batch solve, poll its job status."""

from __future__ import annotations

from arq import ArqRedis
from fastapi import APIRouter, Depends

from app.api._jobs import get_job_status
from app.core.queue import get_arq_redis
from app.schemas.jobs import JobEnqueuedResponse, JobStatusResponse
from app.schemas.rostering import RosterSolveRequest

router = APIRouter(prefix="/rostering", tags=["rostering"])


@router.post("/solve", response_model=JobEnqueuedResponse)
async def solve_roster(
    request: RosterSolveRequest, redis: ArqRedis = Depends(get_arq_redis)
) -> JobEnqueuedResponse:
    """Enqueue a Tier 1 batch solve for `period_start..period_end`.

    Per ARCHITECTURE.md, Tier 1 is the slow, batch-level tier -- this never
    solves inline on the request; ``solve_roster_task`` (see
    ``app/workers/tasks.py``) does the real work once an ``arq`` worker
    picks the job up.
    """
    job = await redis.enqueue_job(
        "solve_roster_task",
        request.period_start.isoformat(),
        request.period_end.isoformat(),
    )
    assert job is not None
    return JobEnqueuedResponse(job_id=job.job_id)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_roster_job(
    job_id: str, redis: ArqRedis = Depends(get_arq_redis)
) -> JobStatusResponse:
    """Poll a previously-enqueued Tier 1 solve job's status/result."""
    return await get_job_status(redis, job_id)
