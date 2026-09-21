"""Worker — a member of the mobile workforce.

Feeds Tier 1 (skills/geo data are direct solver inputs per ARCHITECTURE.md's
Tier 1 "Inputs" list) and is the FK target for AwardCostMatrix rows and
RosterAssignment rows.
"""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Worker(Base):
    """A worker who can be assigned to shifts.

    FLAG FOR REVIEW: ``skills`` is stored as a native Postgres ``ARRAY(TEXT)``
    rather than a normalized ``worker_skill`` join table. This matches the
    task description's "skills (list of strings)" literally and keeps
    skill-eligibility checks in Tier 1 cheap (``skills @> ARRAY[...]``), but
    it ties this column to Postgres specifically (already true of this repo
    given ``asyncpg``) and makes "which workers have skill X" queries rely on
    a GIN index rather than a plain B-tree join. If skills need their own
    metadata (certifications, expiry dates) later, this should become a
    proper ``WorkerSkill`` table instead.

    ``geo/region`` is modeled as a single free-text ``region`` column (not a
    FK to ``Site``) since a worker's home region is a coarse
    solver/eligibility input, distinct from the specific ``Site`` a shift or
    job occurs at.
    """

    __tablename__ = "worker"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    skills: Mapped[list[str]] = mapped_column(ARRAY(String(100)), default=list)
    region: Mapped[str | None] = mapped_column(String(100), index=True, default=None)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    award_cost_rows: Mapped[list[AwardCostMatrix]] = relationship(  # noqa: F821
        back_populates="worker"
    )
    roster_assignments: Mapped[list[RosterAssignment]] = relationship(  # noqa: F821
        back_populates="worker"
    )

    def __repr__(self) -> str:
        return (
            f"Worker(id={self.id!r}, name={self.name!r}, "
            f"region={self.region!r}, active={self.active!r})"
        )
