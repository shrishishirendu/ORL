"""Response schemas for `POST /admin/data/upload`.

(`GET /admin/data/template` returns a raw `.xlsx` file, not JSON, so it has
no schema here.)
"""

from __future__ import annotations

from datetime import date as date_

from pydantic import BaseModel, ConfigDict


class WorkersUpsertSummaryRead(BaseModel):
    """Mirrors `app.services.admin_data.workers.WorkersUpsertSummary`."""

    model_config = ConfigDict(from_attributes=True)

    created: int
    updated: int
    deactivated: int
    matched_by_employee_code: int
    matched_by_name: int
    matched_by_name_workers: list[str]
    deactivated_workers: list[dict]


class SkippedShiftRead(BaseModel):
    """Mirrors `app.services.admin_data.shifts.SkippedShift`."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    date: date_
    site_code: str
    site_name: str
    reason: str


class ShiftsReplaceSummaryRead(BaseModel):
    """Mirrors `app.services.admin_data.shifts.ShiftsReplaceSummary`."""

    model_config = ConfigDict(from_attributes=True)

    period_start: date_
    period_end: date_
    created: int
    deleted: int
    placeholder_award_rows_created: int
    shifts_skipped: list[SkippedShiftRead]


class UploadResponse(BaseModel):
    """`POST /admin/data/upload` response: a structured "what happened"
    report, not just a bare success flag -- `workers`/`shifts` are each
    `None` when that half of the upload wasn't provided at all (as opposed
    to provided-but-empty, which the upload endpoint rejects as a
    validation error -- see app/services/admin_data/upload.py).
    """

    dry_run: bool
    workers: WorkersUpsertSummaryRead | None = None
    shifts: ShiftsReplaceSummaryRead | None = None
