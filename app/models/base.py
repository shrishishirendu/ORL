"""Declarative base for ORM models.

Concrete domain models (Roster, Route, AwardCostMatrix cache, etc.) live in
sibling modules under ``app/models/`` and all import this ``Base`` so that
engine/session wiring, Alembic autogeneration, and the models themselves
share one ``MetaData`` object.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# A naming convention for constraints/indexes so that Alembic autogenerate
# produces stable, predictable names (e.g. "ix_worker_region" rather than a
# driver-assigned name) instead of leaving them anonymous, which makes later
# ``ALTER``/``DROP CONSTRAINT`` migrations awkward to autogenerate.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base for all ORL ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
