"""Async task queue (arq) connection, shared by the FastAPI routers.

The API layer never runs a Tier 1/2/3 solve inline on a request (per
ARCHITECTURE.md's "FastAPI service layer + async task queue" section) --
each ``POST`` handler in ``app/api/`` enqueues a job onto this pool and
returns its id immediately; ``app/workers/tasks.py`` (run by a separate
``arq`` worker process, see its ``WorkerSettings``) is what actually pulls
jobs off the queue and calls the DB-facing services.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.core.settings import settings

_pool: ArqRedis | None = None


async def get_arq_redis() -> AsyncGenerator[ArqRedis, None]:
    """FastAPI dependency yielding a shared arq redis connection pool.

    Lazily created once per process and reused across requests -- creating
    a fresh connection pool per request would defeat the point of pooling.
    """
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield _pool
