"""AwardCostMatrix — the artifact the external Award Interpretation Engine
hands to Tier 1 (see ARCHITECTURE.md's "The AwardCostMatrix boundary").

This table is **consumed-only** from ORL's own solvers: Tier 1 reads
``pay_cost``/``eligible``/``min_hours``/``max_hours`` as fixed input and
never writes to this table itself. Population of this table is the job of
whatever ingests the external Award Interpretation Engine's output -- not
built here, per the task description ("you don't need to build that
ingestion here, just the table").
"""

from __future__ import annotations

from datetime import date as date_

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class AwardCostMatrix(Base):
    """worker x day x shift -> {pay_cost, eligible, min_hours, max_hours}.

    FLAG FOR REVIEW -- primary key shape: ARCHITECTURE.md defines this table
    by its three logical key columns (worker, day, shift), which would make
    a natural composite primary key ``(worker_id, day, shift_id)``. We used a
    surrogate integer ``id`` PK instead, with a ``UniqueConstraint`` over
    ``(worker_id, day, shift_id)`` to enforce the same one-row-per-key
    invariant. Reasoning: this table is periodically replaced wholesale by
    an external ingestion job, and an ingestion process that needs to
    upsert/replace rows (and potentially reference a specific historical
    version of a row, e.g. for audit) is generally easier against a stable
    surrogate key than a wide composite key threaded through FKs elsewhere.
    If you'd rather this be a "pure" composite-PK lookup table with no
    surrogate key (since nothing in Tier 1 needs to reference an individual
    AwardCostMatrix row by id, only look it up by the triple), that's a
    one-line change -- flagging it here rather than assuming.

    ``day`` duplicates ``Shift.date`` for a given ``shift_id``. This is
    deliberate, not an oversight: ARCHITECTURE.md and the task description
    both describe the external engine's matrix as keyed by the triple
    "worker, day, shift" directly, and keeping ``day`` as its own column
    means this table's shape matches the external engine's contract
    byte-for-byte rather than requiring a join through ``Shift`` to recover
    a column the boundary artifact defines explicitly.
    """

    __tablename__ = "award_cost_matrix"
    __table_args__ = (
        UniqueConstraint("worker_id", "day", "shift_id", name="uq_award_cost_matrix_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("worker.id"), index=True)
    day: Mapped[date_] = mapped_column(Date, index=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shift.id"), index=True)
    pay_cost: Mapped[float] = mapped_column(Numeric(10, 2))
    eligible: Mapped[bool] = mapped_column(Boolean)
    min_hours: Mapped[float | None] = mapped_column(Numeric(5, 2), default=None)
    max_hours: Mapped[float | None] = mapped_column(Numeric(5, 2), default=None)

    worker: Mapped[Worker] = relationship(back_populates="award_cost_rows")  # noqa: F821
    shift: Mapped[Shift] = relationship(back_populates="award_cost_rows")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"AwardCostMatrix(id={self.id!r}, worker_id={self.worker_id!r}, "
            f"day={self.day!r}, shift_id={self.shift_id!r}, "
            f"pay_cost={self.pay_cost!r}, eligible={self.eligible!r})"
        )
