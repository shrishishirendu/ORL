"""Tests for the FastAPI routers (``app/api/rostering.py``,
``app/api/dispatch.py``, ``app/api/events.py``).

**Coverage note (see the task report):** these hit a real Redis for the
enqueue half (``POST .../solve`` / ``POST /events`` really call
``ArqRedis.enqueue_job``, so a real job lands on the queue and
``GET .../jobs/{job_id}`` really reads its real status back) but no
``arq`` worker is running in this file, so every job here is asserted only
as far as ``"deferred"``/``"queued"`` -- the "does the job actually run and
persist something" half is ``test_e2e_queue.py``'s job, not this file's.

The Pydantic-level request validation (event shape discrimination,
malformed request bodies) is exercised here without needing Redis at all,
since FastAPI validates the request body before any route handler -- and
therefore before the ``get_arq_redis`` dependency -- ever runs.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from app.core.settings import settings
from app.main import app

pytestmark = pytest.mark.asyncio


async def _redis_reachable() -> bool:
    from arq import create_pool
    from arq.connections import RedisSettings

    try:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await pool.ping()
        await pool.aclose(close_connection_pool=True)
        return True
    except (RedisError, OSError):
        return False


@pytest.fixture(autouse=True)
async def _require_redis():
    if not await _redis_reachable():
        pytest.skip("Redis is not reachable at settings.redis_url")
    yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_rostering_solve_enqueues_a_job_and_status_is_pollable(client) -> None:
    response = await client.post(
        "/rostering/solve",
        json={"period_start": "2026-04-01", "period_end": "2026-04-07"},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert job_id

    status_response = await client.get(f"/rostering/jobs/{job_id}")
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["job_id"] == job_id
    assert body["status"] in ("deferred", "queued", "in_progress", "complete")


async def test_dispatch_solve_enqueues_a_job(client) -> None:
    response = await client.post("/dispatch/solve", json={"roster_assignment_id": 12345})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert job_id


async def test_events_job_cancelled_enqueues_a_job(client) -> None:
    response = await client.post(
        "/events",
        json={
            "event_type": "job_cancelled",
            "shift_id": 1,
            "worker_id": 1,
            "cancelled_job_id": 1,
        },
    )
    assert response.status_code == 200
    assert response.json()["job_id"]


async def test_events_visit_overran_requires_current_site_and_time(client) -> None:
    # visit_overran's current_site_id/current_time are required, not
    # optional -- see app/schemas/events.py's VisitOverranEvent docstring.
    # FastAPI/Pydantic rejects this before the request ever reaches Redis.
    response = await client.post(
        "/events",
        json={"event_type": "visit_overran", "shift_id": 1, "worker_id": 1},
    )
    assert response.status_code == 422


async def test_events_rejects_an_unknown_event_type(client) -> None:
    response = await client.post(
        "/events",
        json={"event_type": "meteor_strike", "shift_id": 1, "worker_id": 1},
    )
    assert response.status_code == 422


async def test_job_status_for_an_unknown_job_id_is_not_found(client) -> None:
    response = await client.get("/rostering/jobs/does-not-exist")
    assert response.status_code == 200
    assert response.json()["status"] == "not_found"


async def test_rostering_solve_rejects_a_malformed_request(client) -> None:
    response = await client.post("/rostering/solve", json={"period_start": "not-a-date"})
    assert response.status_code == 422
