"""Site — a physical location that shifts, jobs, and site-assignments refer to.

FLAG FOR REVIEW: ``Site`` is not one of the entities explicitly listed in
the task description. It is introduced here only because ``Shift.site``,
``Job.site``, and ``SiteAssignment.site`` are all described as "a site
reference" -- to make those genuine foreign keys (and therefore
indexable/joinable) rather than free-text strings duplicated across tables,
some canonical site table has to exist. This is intentionally minimal (just
enough for an FK target + the geo fields Tier 2's Travel Matrix would key
off of) and is NOT an attempt to model the Travel Matrix itself, which
ARCHITECTURE.md describes as a separate cached supporting component and is
out of scope for this task.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Site(Base):
    """A physical location (client site, depot, etc.)."""

    __tablename__ = "site"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(100), index=True, default=None)
    latitude: Mapped[float | None] = mapped_column(default=None)
    longitude: Mapped[float | None] = mapped_column(default=None)

    shifts: Mapped[list[Shift]] = relationship(back_populates="site")  # noqa: F821
    jobs: Mapped[list[Job]] = relationship(back_populates="site")  # noqa: F821
    site_assignments: Mapped[list[SiteAssignment]] = relationship(  # noqa: F821
        back_populates="site"
    )
    workers_home_here: Mapped[list[Worker]] = relationship(  # noqa: F821
        back_populates="home_site"
    )

    def __repr__(self) -> str:
        return f"Site(id={self.id!r}, code={self.code!r}, name={self.name!r})"
