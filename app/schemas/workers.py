"""Request/response schemas for the Workers API: `GET /workers` (read),
plus `POST`/`PATCH /workers` and `POST /workers/regenerate-eligibility`
(admin data-entry -- see app/services/admin_data/workers.py, .../eligibility.py).
"""

from __future__ import annotations

from datetime import date as date_

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import SiteSummary


class WorkerRead(BaseModel):
    """One `Worker` row for the list endpoint (and the create/update
    responses, which return the same shape)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    skills: list[str]
    region: str | None = None
    home_site: SiteSummary
    active: bool
    employee_code: str | None = None


class WorkerListResponse(BaseModel):
    """`GET /workers` response: a page of workers plus paging metadata."""

    items: list[WorkerRead]
    total: int
    limit: int
    offset: int


class WorkerCreateRequest(BaseModel):
    """`POST /workers` request body. `skills` must be non-empty -- a worker
    with no skills could never be eligible for any shift (see
    app.services.rostering.solver's skill filter), so an empty list is
    rejected here rather than accepted as a degenerate worker.
    """

    name: str = Field(min_length=1)
    skills: list[str] = Field(min_length=1)
    region: str | None = None
    home_site_id: int
    active: bool = True
    employee_code: str | None = None


class WorkerUpdateRequest(BaseModel):
    """`PATCH /workers/{id}` request body -- every field optional; an
    omitted field is left unchanged (see
    app.services.admin_data.workers.update_worker, which is driven by
    `model_dump(exclude_unset=True)` of this schema). `skills`, if given,
    must still be non-empty -- same reasoning as `WorkerCreateRequest`.
    """

    name: str | None = Field(default=None, min_length=1)
    skills: list[str] | None = Field(default=None, min_length=1)
    region: str | None = None
    home_site_id: int | None = None
    active: bool | None = None
    employee_code: str | None = None


class RegenerateEligibilityResponse(BaseModel):
    """`POST /workers/regenerate-eligibility` response."""

    period_start: date_
    period_end: date_
    placeholder_award_rows_created: int
