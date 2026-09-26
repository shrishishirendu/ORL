"""Award Engine service endpoints: fill `AwardCostMatrix` from the engine,
poll that job, and proxy the engine's health.

`POST /award-engine/sync-matrix` follows the same enqueue-then-poll shape as
the three tier endpoints (ARCHITECTURE.md, "API surface and job polling"):
a sync costs two engine calculations per worker x shift cell plus a bulk
upsert, which grows with the period and the workforce, so it runs on the
arq worker (`sync_award_matrix_task`) rather than inline on the request.
"""

from __future__ import annotations

from arq import ArqRedis
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.api._jobs import get_job_status
from app.core.queue import get_arq_redis
from app.core.settings import settings
from app.schemas.award_engine import AwardEngineHealthResponse, MatrixSyncRequest
from app.schemas.jobs import JobEnqueuedResponse, JobStatusResponse
from app.services.award_engine.client import AwardEngineError, client_from_settings

router = APIRouter(prefix="/award-engine", tags=["award-engine"])


@router.post("/sync-matrix", response_model=JobEnqueuedResponse)
async def sync_matrix(
    request: MatrixSyncRequest, redis: ArqRedis = Depends(get_arq_redis)
) -> JobEnqueuedResponse:
    """Enqueue an `AwardCostMatrix` sync for `period_start..period_end`.

    503 if the engine is not configured (`AWARD_ENGINE_URL` unset) -- there
    is nothing to enqueue. Poll the result at `GET /award-engine/jobs/{id}`;
    its `result.status` is `ok` (with the sync summary) or
    `engine_unavailable`/`engine_rejected` (nothing written, placeholders kept).
    """
    if not settings.award_engine_url:
        raise HTTPException(status_code=503, detail="award engine not configured")
    job = await redis.enqueue_job(
        "sync_award_matrix_task",
        request.period_start.isoformat(),
        request.period_end.isoformat(),
    )
    assert job is not None
    return JobEnqueuedResponse(job_id=job.job_id)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_sync_job(job_id: str, redis: ArqRedis = Depends(get_arq_redis)) -> JobStatusResponse:
    """Poll a previously-enqueued matrix sync job's status/result."""
    return await get_job_status(redis, job_id)


@router.get("/health", response_model=AwardEngineHealthResponse)
async def award_engine_health() -> JSONResponse:
    """Proxy `GET /engine/health`.

    200 with `configured: false` when the engine is disabled (that is a
    valid configuration, not a fault); otherwise the engine's own HTTP
    status (503 when `degraded`), or 503 `unreachable` if it can't be reached.
    """
    client = client_from_settings()
    if client is None:
        body = AwardEngineHealthResponse(configured=False, status="disabled")
        return JSONResponse(body.model_dump())
    try:
        async with client:
            status_code, health = await client.health()
    except AwardEngineError as exc:
        body = AwardEngineHealthResponse(configured=True, status="unreachable", detail=str(exc))
        return JSONResponse(body.model_dump(), status_code=503)
    body = AwardEngineHealthResponse(
        configured=True, status=health.status, engine=health.model_dump(mode="json")
    )
    return JSONResponse(body.model_dump(), status_code=status_code)
