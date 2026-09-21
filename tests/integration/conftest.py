"""Shared fixtures for the DB-backed integration tests.

Every test under ``tests/integration/`` needs a live Postgres reachable at
``app.core.settings.settings.database_url`` (already migrated to head --
these tests don't run migrations themselves). ``test_e2e_queue.py``
additionally needs a live Redis at ``settings.redis_url``; that file checks
for it itself rather than through a fixture here, since it's the only file
in this package that needs it.

Isolation between tests is by truncating every app table after each test
(``_reset_tables``, autouse) rather than wrapping each test in a
transaction that gets rolled back -- the code under test
(``solve_and_persist_roster`` etc.) calls ``session.commit()`` itself, which
would end an outer "rollback at the end" transaction early.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.queue as queue_module
from app.core.db import async_session_factory, engine
from app.models import Base


async def _postgres_reachable() -> bool:
    try:
        async with engine.connect():
            return True
    except SQLAlchemyError:
        return False


@pytest_asyncio.fixture(autouse=True)
async def _require_postgres() -> AsyncGenerator[None, None]:
    if not await _postgres_reachable():
        pytest.skip("Postgres is not reachable at settings.database_url")
    yield


@pytest_asyncio.fixture(autouse=True)
async def _reset_tables() -> AsyncGenerator[None, None]:
    yield
    async with engine.begin() as conn:
        table_names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
    # pytest-asyncio (function-scoped event loop, the default under
    # asyncio_mode=auto) tears down and recreates the event loop between
    # tests, but `app.core.db.engine`'s connection pool is a module-level
    # singleton whose pooled asyncpg connections are bound to whichever
    # loop created them. Without disposing here, the next test's fresh loop
    # would try to reuse a connection created under the previous (now
    # closed) loop and asyncpg raises "attached to a different loop".
    # Disposing forces the pool to open fresh connections under whatever
    # loop is current next time it's used.
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _reset_arq_pool() -> AsyncGenerator[None, None]:
    """Same cross-loop problem as ``_reset_tables`` above, for
    ``app.core.queue``'s cached ``ArqRedis`` pool: it's a module-level
    singleton (``get_arq_redis`` creates it once per process, see that
    module's docstring), so a pool opened under one test's event loop must
    not be reused by the next test's fresh loop.
    """
    yield
    if queue_module._pool is not None:
        await queue_module._pool.aclose(close_connection_pool=True)
        queue_module._pool = None


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
