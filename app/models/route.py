"""Route / RouteStop — Tier 2's per-shift, per-worker VRPTW output.

Per ARCHITECTURE.md's Tier 2 section: "Output: Route -- an ordered stop
sequence with arrival times per worker", produced only for multi-stop
roles (single-site roles use ``SiteAssignment`` instead, see
``site_assignment.py``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import RouteStatus


class Route(Base):
    """One Tier 2 routing solve for a single shift/worker.

    ``roster_assignment_id`` is unique: exactly one current ``Route`` exists
    per ``RosterAssignment`` at a time. A Tier 3 scoped re-solve produces a
    *new* solved state for that same route rather than a new ``Route`` row
    (see ``ReoptimizationEvent``, which references the ``Route`` it
    re-solved) -- FLAG FOR REVIEW if you'd rather each re-solve create a new
    ``Route`` row (an immutable history of every solve) instead of updating
    this one in place; we chose "one row, re-solved in place, status
    reflects the latest solve" because ARCHITECTURE.md's Tier 3 section
    describes re-optimization as "re-runs routing for the affected
    shift/worker(s)" without indicating routes are versioned/archived, but
    it's a real design fork worth confirming since it affects whether
    ``ReoptimizationEvent`` history is enough to reconstruct past route
    states.
    """

    __tablename__ = "route"

    id: Mapped[int] = mapped_column(primary_key=True)
    roster_assignment_id: Mapped[int] = mapped_column(
        ForeignKey("roster_assignment.id"), unique=True, index=True
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[RouteStatus] = mapped_column(
        Enum(RouteStatus, name="route_status"),
        default=RouteStatus.PENDING,
        index=True,
    )

    roster_assignment: Mapped[RosterAssignment] = relationship(  # noqa: F821
        back_populates="route"
    )
    stops: Mapped[list[RouteStop]] = relationship(
        back_populates="route", cascade="all, delete-orphan", order_by="RouteStop.sequence_no"
    )

    def __repr__(self) -> str:
        return (
            f"Route(id={self.id!r}, roster_assignment_id={self.roster_assignment_id!r}, "
            f"status={self.status!r})"
        )


class RouteStop(Base):
    """One ordered stop within a solved Route: a Job placed at a sequence
    position with a planned arrival/departure time.
    """

    __tablename__ = "route_stop"
    __table_args__ = (
        UniqueConstraint("route_id", "sequence_no", name="uq_route_stop_sequence"),
        UniqueConstraint("route_id", "job_id", name="uq_route_stop_job"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    route_id: Mapped[int] = mapped_column(ForeignKey("route.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"), index=True)
    sequence_no: Mapped[int] = mapped_column(Integer)
    planned_arrival: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    planned_departure: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    route: Mapped[Route] = relationship(back_populates="stops")
    job: Mapped[Job] = relationship(back_populates="route_stops")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"RouteStop(id={self.id!r}, route_id={self.route_id!r}, job_id={self.job_id!r}, "
            f"sequence_no={self.sequence_no!r})"
        )
