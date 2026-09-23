"""Worker — a member of the mobile workforce.

Feeds Tier 1 (skills/geo data are direct solver inputs per ARCHITECTURE.md's
Tier 1 "Inputs" list) and is the FK target for AwardCostMatrix rows and
RosterAssignment rows.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String
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

    ``home_site_id`` is the worker's home location -- the depot for both
    tiers per ARCHITECTURE.md's "Home location as depot" section: Tier 1
    uses ``home_site_id``'s ``Site.region`` as a hard eligibility filter
    (a worker can only be rostered onto a shift whose site is in a
    compatible region), and Tier 2 uses it as the fixed start *and* end node
    of the per-shift VRPTW round trip. It is a required (``NOT NULL``) FK --
    every worker has exactly one home location -- unlike the coarser,
    optional ``region`` column above, which remains as a cheap/free-text
    fallback/override for eligibility when a caller doesn't want to resolve
    it via the FK join (see the rostering solver's docstring for how the two
    interact).

    ``employee_code`` is a nullable, unique, indexed natural key -- added
    for the admin data-entry/bulk-upload feature (see
    ``app/services/admin_data/``), mirroring ``Site.code``'s existing
    pattern. It exists purely so a bulk Workers upload has something stable
    to upsert/replace against across repeated uploads: matching a row to an
    existing ``Worker`` by name alone is ambiguous (names collide, get
    retyped slightly differently) and offers no continuity if a worker is
    renamed between uploads. It stays optional (unlike ``Site.code``)
    because not every admin-entry workflow has an external employee-code
    system to key off of -- when absent, upload matching falls back to an
    exact case-insensitive ``name`` match instead (see
    ``app/services/admin_data/workers.py``, which flags that fallback as
    less reliable in its response).
    """

    __tablename__ = "worker"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    skills: Mapped[list[str]] = mapped_column(ARRAY(String(100)), default=list)
    region: Mapped[str | None] = mapped_column(String(100), index=True, default=None)
    home_site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    employee_code: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, default=None
    )

    home_site: Mapped[Site] = relationship(  # noqa: F821
        back_populates="workers_home_here"
    )
    award_cost_rows: Mapped[list[AwardCostMatrix]] = relationship(  # noqa: F821
        back_populates="worker"
    )
    roster_assignments: Mapped[list[RosterAssignment]] = relationship(  # noqa: F821
        back_populates="worker"
    )

    def __repr__(self) -> str:
        return (
            f"Worker(id={self.id!r}, name={self.name!r}, "
            f"region={self.region!r}, home_site_id={self.home_site_id!r}, "
            f"active={self.active!r})"
        )
