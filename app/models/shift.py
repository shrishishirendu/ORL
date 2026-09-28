"""Shift — a requirement definition (not an assignment).

Per ARCHITECTURE.md, a Shift is what Tier 1 solves *against* (it is part of
Tier 1's "shift requirements" input) and, once assigned, is what routes
either through Tier 2 (multi-stop roles) or bypasses it via a direct
SiteAssignment (single-site roles).
"""

from __future__ import annotations

from datetime import date as date_
from datetime import time

from sqlalchemy import Boolean, Date, ForeignKey, Index, Integer, String, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Shift(Base):
    """A shift requirement: a skill needed at a site on a date/time window.

    ``is_multi_stop`` is the flag ARCHITECTURE.md's Tier 2 section describes:
    "only multi-stop roles go through VRPTW routing. Single-site roles skip
    this tier entirely via a simple Site Assignment path." A Shift with
    ``is_multi_stop=True`` is expected to have ``Job`` rows (stops) once
    assigned and routed via a ``Route``/``RouteStop``; one with
    ``is_multi_stop=False`` is expected to resolve straight to a
    ``SiteAssignment`` instead.

    ``break_minutes`` / ``break_start`` are the rostered unpaid break. They
    are sent to the Award Engine as-is (docs/AWARD_ENGINE_CONTRACT.md), so
    it prices break-net hours. ``None`` means "not known": the engine is then
    told 0 minutes (the previous behaviour, an over-estimate for any shift
    that has an unpaid break). ``break_start`` is needed for the engine to
    resolve a break that could fall in differently paid time (overnight, or
    across 06:00/18:00). ORL never infers either value. A rostered break is
    a plan, not a timesheet record, so ORL doesn't claim ``break_recorded``.
    """

    __tablename__ = "shift"
    __table_args__ = (Index("ix_shift_date_required_skill", "date", "required_skill"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date_] = mapped_column(Date, index=True)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    required_skill: Mapped[str] = mapped_column(String(100), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    is_multi_stop: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    break_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    break_start: Mapped[time | None] = mapped_column(Time, default=None)

    site: Mapped[Site] = relationship(back_populates="shifts")  # noqa: F821
    award_cost_rows: Mapped[list[AwardCostMatrix]] = relationship(  # noqa: F821
        back_populates="shift"
    )
    roster_assignments: Mapped[list[RosterAssignment]] = relationship(  # noqa: F821
        back_populates="shift"
    )
    jobs: Mapped[list[Job]] = relationship(back_populates="shift")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"Shift(id={self.id!r}, date={self.date!r}, "
            f"start_time={self.start_time!r}, end_time={self.end_time!r}, "
            f"required_skill={self.required_skill!r}, site_id={self.site_id!r}, "
            f"is_multi_stop={self.is_multi_stop!r})"
        )
