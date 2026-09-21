"""Job — a stop within a shift, for multi-stop roles.

Per ARCHITECTURE.md's Tier 2 section, Tier 2's VRPTW routing takes "the
job list for that shift" as input and produces a ``Route`` (ordered stop
sequence). A ``Job`` therefore belongs to a ``Shift`` (the requirement it is
part of), not directly to a ``Route`` or ``RosterAssignment`` -- the same
job list is the input Tier 2 solves against every time that shift is
(re-)routed, including Tier 3's scoped re-solves.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Job(Base):
    """A single stop (visit) that must be made during a multi-stop shift.

    FLAG FOR REVIEW -- Job vs Shift vs Route relationship: ``Job`` is
    modeled as belonging to ``Shift`` (a fixed requirement -- "this shift
    needs these stops made"), and ``RouteStop`` (see ``route.py``) is the
    join row recording where a *solved* ``Route`` places that job in
    sequence with a planned arrival/departure. This means the same ``Job``
    row is reused across re-solves of a shift's route (initial Tier 2 solve,
    and any Tier 3 scoped re-solve) rather than being copied per-route. The
    alternative -- Job belonging directly to a Route/RosterAssignment
    instead of Shift -- would mean a fresh set of Job rows every time a
    shift is re-routed, which seemed wrong given ARCHITECTURE.md frames the
    job list as a Tier 2 *input* alongside the roster and travel matrix, not
    part of a Route's output. Confirm this matches your intent.

    ``duration_minutes`` models "duration" as a plain integer number of
    minutes rather than a SQL ``INTERVAL``, to keep it a simple scalar for
    the CP-SAT/VRPTW solvers to consume directly.

    ``sequence_hint`` is optional -- a soft/preferred ordering hint (e.g.
    from a client's preferred visit order) that Tier 2's VRPTW solver may
    use as a bias, as distinct from ``RouteStop.sequence_no``, which is the
    solved, authoritative order.
    """

    __tablename__ = "job"
    __table_args__ = (Index("ix_job_shift_site", "shift_id", "site_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shift.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    window_start: Mapped[datetime] = mapped_column()
    window_end: Mapped[datetime] = mapped_column()
    duration_minutes: Mapped[int] = mapped_column(Integer)
    sequence_hint: Mapped[int | None] = mapped_column(Integer, default=None)

    shift: Mapped[Shift] = relationship(back_populates="jobs")  # noqa: F821
    site: Mapped[Site] = relationship(back_populates="jobs")  # noqa: F821
    route_stops: Mapped[list[RouteStop]] = relationship(back_populates="job")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"Job(id={self.id!r}, shift_id={self.shift_id!r}, site_id={self.site_id!r}, "
            f"window_start={self.window_start!r}, window_end={self.window_end!r})"
        )
