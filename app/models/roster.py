"""Roster / RosterAssignment — Tier 1's batch output.

Per ARCHITECTURE.md's Tier 1 section: "Output: Roster -- a worker -> day ->
shift assignment", produced by an infrequent (weekly/fortnightly) CP-SAT
batch solve that Tiers 2 and 3 then operate within for the rest of the
period.
"""

from __future__ import annotations

from datetime import date as date_
from datetime import datetime

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Index, Numeric, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import RosterStatus


class Roster(Base):
    """One Tier 1 batch run covering a period (e.g. one week/fortnight).

    ``failure_reason`` is populated only when ``status`` is
    ``RosterStatus.FAILED``: a human-readable rendering of the infeasible
    ``RosterSolution.unfilled_shifts``/``diagnostics`` (see
    ``app/services/rostering/solver.py``), so a failed batch solve leaves a
    record of *why* it couldn't cover every shift rather than silently
    dropping that diagnostic information (this column was added specifically
    to close that gap -- see ``app/services/rostering/service.py``, which
    populates it). Free text rather than a structured/JSONB column since
    ``RosterSolution.diagnostics`` is already a list of human-readable
    strings with no further structure to preserve.
    """

    __tablename__ = "roster"
    __table_args__ = (Index("ix_roster_period", "period_start", "period_end"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    period_start: Mapped[date_] = mapped_column(Date)
    period_end: Mapped[date_] = mapped_column(Date)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[RosterStatus] = mapped_column(
        Enum(RosterStatus, name="roster_status"),
        default=RosterStatus.PENDING,
        index=True,
    )
    total_cost: Mapped[float | None] = mapped_column(Numeric(12, 2), default=None)
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)

    assignments: Mapped[list[RosterAssignment]] = relationship(
        back_populates="roster", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"Roster(id={self.id!r}, period_start={self.period_start!r}, "
            f"period_end={self.period_end!r}, status={self.status!r}, "
            f"total_cost={self.total_cost!r})"
        )


class RosterAssignment(Base):
    """One worker -> day -> shift assignment row within a Roster.

    This is the actual output row Tier 1 produces (as opposed to ``Shift``,
    which is the requirement it was solved against, and ``AwardCostMatrix``,
    which is the cost/eligibility input it was solved with).

    Downstream, each ``RosterAssignment`` resolves to exactly one of:

    - a ``Route`` (Tier 2 output), when ``Shift.is_multi_stop`` is True, or
    - a ``SiteAssignment`` (the Tier 2 bypass path), when it is False.

    Both are modeled as a one-to-one relationship keyed off this row's id
    (enforced with a ``UniqueConstraint`` on the FK column in each of those
    tables) rather than that row carrying a "route_id or site_assignment_id"
    of its own, so that neither Tier 2 nor the bypass path needs write
    access back onto the Tier 1 output row.
    """

    __tablename__ = "roster_assignment"
    __table_args__ = (
        UniqueConstraint(
            "roster_id", "worker_id", "day", "shift_id", name="uq_roster_assignment_key"
        ),
        Index("ix_roster_assignment_worker_day_shift", "worker_id", "day", "shift_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    roster_id: Mapped[int] = mapped_column(ForeignKey("roster.id"), index=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("worker.id"), index=True)
    day: Mapped[date_] = mapped_column(Date, index=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shift.id"), index=True)

    roster: Mapped[Roster] = relationship(back_populates="assignments")
    worker: Mapped[Worker] = relationship(back_populates="roster_assignments")  # noqa: F821
    shift: Mapped[Shift] = relationship(back_populates="roster_assignments")  # noqa: F821
    route: Mapped[Route | None] = relationship(  # noqa: F821
        back_populates="roster_assignment", uselist=False
    )
    site_assignment: Mapped[SiteAssignment | None] = relationship(  # noqa: F821
        back_populates="roster_assignment", uselist=False
    )

    def __repr__(self) -> str:
        return (
            f"RosterAssignment(id={self.id!r}, roster_id={self.roster_id!r}, "
            f"worker_id={self.worker_id!r}, day={self.day!r}, shift_id={self.shift_id!r})"
        )
