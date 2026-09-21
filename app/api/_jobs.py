"""Shared "look up an arq job's status/result" helper for every router
under ``app/api/`` that exposes a `GET .../jobs/{job_id}` endpoint.
"""

from __future__ import annotations

from arq import ArqRedis
from arq.jobs import Job

from app.schemas.jobs import JobStatusResponse


async def get_job_status(redis: ArqRedis, job_id: str) -> JobStatusResponse:
    """Look up `job_id`'s current arq status, and its result if it finished.

    Does not wait for the job -- a job still queued/running is reported as
    such, with ``result``/``success`` left unset. A job that raised inside
    the task function is reported as ``success: False`` with ``result`` set
    to ``str(exception)`` (never the raw exception object, which is not
    JSON-serializable).
    """
    job = Job(job_id, redis)
    status = await job.status()
    info = await job.result_info()
    if info is None:
        return JobStatusResponse(job_id=job_id, status=status.value)
    return JobStatusResponse(
        job_id=job_id,
        status=status.value,
        success=info.success,
        result=info.result if info.success else str(info.result),
    )
