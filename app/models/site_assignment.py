"""SiteAssignment — the simple bypass path for single-site roles.

Per ARCHITECTURE.md's Tier 2 section: "Single-site roles skip this tier
entirely via a simple 'Site Assignment' path -- there is nothing to
sequence when a worker has one site for the shift." This table exists
alongside ``Route``/``RouteStop`` precisely so single-site shifts never need
to go through VRPTW routing at all.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class SiteAssignment(Base):
    """A single worker/site/shift arrival-departure pairing, with no routing.

    ``roster_assignment_id`` is unique for the same reason as
    ``Route.roster_assignment_id``: exactly one of ``Route`` or
    ``SiteAssignment`` should exist per ``RosterAssignment``, selected by
    ``Shift.is_multi_stop``. That either/or invariant (a RosterAssignment
    has a Route XOR a SiteAssignment, never both/neither once resolved) is
    not enforced at the DB level here (it would need a check constraint
    spanning both tables, which Postgres can't express directly without a
    trigger) -- FLAG FOR REVIEW: worth deciding whether that invariant is
    enforced in application code (e.g. in the service that resolves a
    RosterAssignment down to one path or the other) or left unenforced at
    the DB level as done here.
    """

    __tablename__ = "site_assignment"

    id: Mapped[int] = mapped_column(primary_key=True)
    roster_assignment_id: Mapped[int] = mapped_column(
        ForeignKey("roster_assignment.id"), unique=True, index=True
    )
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    arrival: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    departure: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    roster_assignment: Mapped[RosterAssignment] = relationship(  # noqa: F821
        back_populates="site_assignment"
    )
    site: Mapped[Site] = relationship(back_populates="site_assignments")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"SiteAssignment(id={self.id!r}, "
            f"roster_assignment_id={self.roster_assignment_id!r}, site_id={self.site_id!r})"
        )
