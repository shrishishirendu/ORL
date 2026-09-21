"""Async SQLAlchemy engine and session factory.

Domain models and Alembic migrations are introduced in a later task; this
module only wires up the async engine/session machinery that Postgres
persistence (for all three tiers) will build on.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.settings import settings

engine = create_async_engine(settings.database_url, echo=False, future=True)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a scoped async DB session."""
    async with async_session_factory() as session:
        yield session
