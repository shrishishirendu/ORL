"""Response schema for `GET /sites` -- the one Sites-related addition in
this feature (see app/api/sites.py's docstring: a read-only helper to
populate site dropdowns in the new Worker/Shift admin forms, not a Sites
CRUD surface).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SiteRead(BaseModel):
    """One `Site` row for `GET /sites`."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    region: str | None = None


class SiteListResponse(BaseModel):
    """`GET /sites` response. No pagination -- there are only ever a
    handful of sites (see the task brief), so this returns every row.
    """

    items: list[SiteRead]
