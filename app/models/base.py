"""Declarative base for ORM models.

Concrete domain models (Roster, Route, AwardCostMatrix cache, etc.) are
added in a later task; this module exists so that engine/session wiring
and future models share one metadata object.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all ORL ORM models."""
