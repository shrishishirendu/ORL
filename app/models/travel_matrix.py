"""TravelMatrixEntry — the cached travel-time matrix between sites.

Per ARCHITECTURE.md's "Supporting components" section: "a cached
travel-time matrix between sites, feeding Tier 2 (and Tier 3's scoped
re-solves) so that VRPTW routing doesn't recompute travel times from
scratch on every solve." This table is what the dispatch (Tier 2) and
re-optimization (Tier 3) DB-facing services query (see
``app/services/dispatch/service.py``) to build the in-memory
``app.services.dispatch.solver.TravelTimeMatrix`` the pure solvers expect
-- see that module's docstring on why the in-memory shape is a plain
``{(from_site_id, to_site_id): minutes}`` mapping rather than a dense 2D
array; this table mirrors that shape directly, one row per directed pair.

FLAG FOR REVIEW -- directionality: one row is one *directed* (from, to)
pair. Real travel times are not always symmetric (one-way streets,
asymmetric traffic), so this table does not assume/enforce a mirrored row
for the reverse direction the way ``TravelTimeMatrix.from_symmetric_pairs``
does purely for tests -- a caller/ingestion job that wants a symmetric cache
must insert both directions explicitly. A same-site (``from_site_id ==
to_site_id``) row is neither required nor forbidden here; the in-memory
``TravelTimeMatrix.get`` already treats same-site lookups as 0 without
needing a cached entry, so such a row would simply never be consulted by
either solver.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class TravelMatrixEntry(Base):
    """One cached directed travel time between two sites, in whole minutes."""

    __tablename__ = "travel_matrix_entry"
    __table_args__ = (
        UniqueConstraint(
            "from_site_id",
            "to_site_id",
            name="uq_travel_matrix_entry_site_pair",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    from_site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    to_site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    travel_minutes: Mapped[int] = mapped_column(Integer)

    from_site: Mapped[Site] = relationship(foreign_keys=[from_site_id])  # noqa: F821
    to_site: Mapped[Site] = relationship(foreign_keys=[to_site_id])  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"TravelMatrixEntry(id={self.id!r}, from_site_id={self.from_site_id!r}, "
            f"to_site_id={self.to_site_id!r}, travel_minutes={self.travel_minutes!r})"
        )
