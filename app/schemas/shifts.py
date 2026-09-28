"""Request/response schemas for the Shifts API: `GET /shifts` (read), plus
`POST`/`PATCH /shifts` (admin data-entry -- see app/services/admin_data/shifts.py,
.../eligibility.py).
"""

from __future__ import annotations

from datetime import date as date_
from datetime import time

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import SiteSummary


class ShiftRead(BaseModel):
    """One `Shift` row for the list endpoint.

    `eligible_worker_count` is the number of `AwardCostMatrix` rows with
    `eligible=True` for this shift (placeholder or real, both count -- both
    represent a worker Tier 1 would actually consider) -- added so the
    admin dashboard's Shifts table can show an "eligible workers" coverage
    badge without a second per-row request; see app/api/shifts.py for how
    this is computed with one grouped query rather than N+1.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    date: date_
    start_time: time
    end_time: time
    required_skill: str
    site: SiteSummary
    is_multi_stop: bool
    break_minutes: int | None = None
    break_start: time | None = None
    eligible_worker_count: int = 0


class ShiftListResponse(BaseModel):
    """`GET /shifts` response: a page of shifts plus paging metadata."""

    items: list[ShiftRead]
    total: int
    limit: int
    offset: int


class ShiftCreateRequest(BaseModel):
    """`POST /shifts` request body.

    `is_multi_stop=True` is rejected (422) by the service layer, not here
    -- see app.services.admin_data.errors.MultiStopNotSupportedError for
    the exact message, which this schema doesn't duplicate.
    """

    date: date_
    start_time: time
    end_time: time
    required_skill: str = Field(min_length=1)
    site_id: int
    is_multi_stop: bool = False
    # Rostered unpaid break. Omit when unknown; checked against the shift
    # span by the service layer (422 when it doesn't fit).
    break_minutes: int | None = Field(default=None, ge=0)
    break_start: time | None = None


class ShiftUpdateRequest(BaseModel):
    """`PATCH /shifts/{id}` request body -- every field optional; an
    omitted field is left unchanged.
    """

    date: date_ | None = None
    start_time: time | None = None
    end_time: time | None = None
    required_skill: str | None = Field(default=None, min_length=1)
    site_id: int | None = None
    is_multi_stop: bool | None = None
    break_minutes: int | None = Field(default=None, ge=0)
    break_start: time | None = None


class ShiftCreateResponse(BaseModel):
    """`POST /shifts` response: the created shift plus how many placeholder
    AwardCostMatrix rows this create just generated for it (see
    app.services.admin_data.eligibility) -- so the admin UI can immediately
    show whether the new shift is actually solvable yet.
    """

    shift: ShiftRead
    placeholder_award_rows_created: int


class ShiftUpdateResponse(BaseModel):
    """`PATCH /shifts/{id}` response -- same shape as `ShiftCreateResponse`,
    since an update also re-runs placeholder generation (see
    app.services.admin_data.shifts.update_shift).
    """

    shift: ShiftRead
    placeholder_award_rows_created: int
