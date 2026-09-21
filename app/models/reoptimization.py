"""ReoptimizationEvent — Tier 3's event-driven intra-day re-optimization log.

Per ARCHITECTURE.md's Tier 3 section: triggered by a job cancellation,
worker sickness, or a visit overrun; resolved either by a scoped re-solve
back into Tier 2, or by escalation up to Tier 1 when the scoped re-solve
can't produce a feasible route.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import (
    ReoptimizationEventType,
    ReoptimizationResolution,
    ReoptimizationStatus,
)


class ReoptimizationEvent(Base):
    """One Tier 3 disruption event and how it was (or is being) resolved.

    FLAG FOR REVIEW -- "affected route/shift/worker reference(s)": the task
    description phrases this as "reference(s)" (plural-capable), which is
    ambiguous between "an event can reference several routes/shifts/workers"
    (would need association tables) and "an event has one of each,
    optionally" (single nullable FKs). We modeled the latter -- one nullable
    FK each to ``Route``, ``Shift``, and ``Worker`` -- since ARCHITECTURE.md's
    Tier 3 examples (a cancelled job, a sick worker, an overrun visit) are
    each naturally scoped to a single shift/worker/route, and Tier 3's
    re-solve scope is explicitly described as "the affected shift/worker(s)
    only, not the whole day or roster" (i.e. narrow by design). If a single
    disruption really can fan out to multiple routes/workers at once (e.g.
    one worker's sickness cascading across several of their shifts that day),
    this should become a join table instead.

    All three FK columns are nullable because which ones are populated
    depends on ``event_type``: a ``worker_sick`` event centers on a
    ``worker_id`` (and, once resolved, the ``route``/``shift`` it affected),
    while a ``job_cancelled`` event might be raised before any specific
    worker/route is known to be impacted.
    """

    __tablename__ = "reoptimization_event"
    __table_args__ = (
        Index("ix_reopt_event_occurred_at", "occurred_at"),
        Index("ix_reopt_event_route_shift_worker", "route_id", "shift_id", "worker_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[ReoptimizationEventType] = mapped_column(
        Enum(ReoptimizationEventType, name="reoptimization_event_type"), index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    route_id: Mapped[int | None] = mapped_column(ForeignKey("route.id"), index=True, default=None)
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shift.id"), index=True, default=None)
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("worker.id"), index=True, default=None)
    resolution: Mapped[ReoptimizationResolution | None] = mapped_column(
        Enum(ReoptimizationResolution, name="reoptimization_resolution"), default=None
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[ReoptimizationStatus] = mapped_column(
        Enum(ReoptimizationStatus, name="reoptimization_status"),
        default=ReoptimizationStatus.OPEN,
        index=True,
    )

    route: Mapped[Route | None] = relationship()  # noqa: F821
    shift: Mapped[Shift | None] = relationship()  # noqa: F821
    worker: Mapped[Worker | None] = relationship()  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"ReoptimizationEvent(id={self.id!r}, event_type={self.event_type!r}, "
            f"status={self.status!r}, resolution={self.resolution!r})"
        )
