"""Domain models (SQLAlchemy ORM).

Every model module is imported here so that:

- ``from app.models import Worker, Roster, ...`` works as a convenience.
- ``Base.metadata`` (see ``app/models/base.py``) is fully populated as long
  as this package has been imported once -- which matters for Alembic
  autogenerate (``alembic/env.py`` imports this package before diffing
  against ``Base.metadata``) and for the metadata-level tests in
  ``tests/test_models.py``.
"""

from app.models.award_cost_matrix import AwardCostMatrix
from app.models.base import Base
from app.models.enums import (
    EmploymentType,
    ReoptimizationEventType,
    ReoptimizationResolution,
    ReoptimizationStatus,
    RosterCostStatus,
    RosterStatus,
    RouteStatus,
)
from app.models.job import Job
from app.models.reoptimization import ReoptimizationEvent
from app.models.roster import Roster, RosterAssignment
from app.models.route import Route, RouteStop
from app.models.shift import Shift
from app.models.site import Site
from app.models.site_assignment import SiteAssignment
from app.models.travel_matrix import TravelMatrixEntry
from app.models.worker import Worker

__all__ = [
    "AwardCostMatrix",
    "Base",
    "EmploymentType",
    "Job",
    "ReoptimizationEvent",
    "ReoptimizationEventType",
    "ReoptimizationResolution",
    "ReoptimizationStatus",
    "Roster",
    "RosterAssignment",
    "RosterCostStatus",
    "RosterStatus",
    "Route",
    "RouteStatus",
    "RouteStop",
    "Shift",
    "Site",
    "SiteAssignment",
    "TravelMatrixEntry",
    "Worker",
]
