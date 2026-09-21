"""Response schemas for `GET /workers`."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.schemas.common import SiteSummary


class WorkerRead(BaseModel):
    """One `Worker` row for the list endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    skills: list[str]
    region: str | None = None
    home_site: SiteSummary
    active: bool


class WorkerListResponse(BaseModel):
    """`GET /workers` response: a page of workers plus paging metadata."""

    items: list[WorkerRead]
    total: int
    limit: int
    offset: int
