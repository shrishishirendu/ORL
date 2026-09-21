"""Tier 2 dispatch/routing API: enqueue a per-shift solve, poll its job status."""

from __future__ import annotations

from arq import ArqRedis
from fastapi import APIRouter, Depends

from app.api._jobs import get_job_status
from app.core.queue import get_arq_redis
from app.schemas.dispatch import DispatchSolveRequest
from app.schemas.jobs import JobEnqueuedResponse, JobStatusResponse

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


@router.post("/solve", response_model=JobEnqueuedResponse)
async def solve_route(
    request: DispatchSolveRequest, redis: ArqRedis = Depends(get_arq_redis)
) -> JobEnqueuedResponse:
    """Enqueue a Tier 2 solve for one worker's shift (`roster_assignment_id`).

    A single-site ``RosterAssignment`` still resolves cleanly through this
    same endpoint: ``solve_route_task`` (see ``app/workers/tasks.py``) will
    bypass VRPTW entirely and write a ``SiteAssignment`` instead of a
    ``Route`` -- see ``DispatchSolveRequest``'s docstring.
    """
    job = await redis.enqueue_job("solve_route_task", request.roster_assignment_id)
    assert job is not None
    return JobEnqueuedResponse(job_id=job.job_id)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_dispatch_job(
    job_id: str, redis: ArqRedis = Depends(get_arq_redis)
) -> JobStatusResponse:
    """Poll a previously-enqueued Tier 2 solve job's status/result."""
    return await get_job_status(redis, job_id)
