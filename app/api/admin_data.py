"""Bulk admin-data endpoints: `POST /admin/data/upload`,
`GET /admin/data/template`.

Thin parse-request/call-service/shape-response layer, matching the rest of
`app/api/` -- the actual parsing/upsert/replace logic lives in
`app/services/admin_data/upload.py` and `.../template.py`.
"""

from __future__ import annotations

from datetime import date as date_

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.schemas.admin_data import UploadResponse
from app.services.admin_data.errors import UploadValidationError
from app.services.admin_data.template import build_template_workbook
from app.services.admin_data.upload import process_upload

router = APIRouter(prefix="/admin/data", tags=["admin-data"])

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/template")
async def get_upload_template() -> Response:
    """Return a ready-to-fill `.xlsx` with the exact `Workers`/`Shifts`
    sheet shapes `POST /admin/data/upload`'s `combined_file` expects, one
    placeholder example row each, and a `Legend` sheet of required/optional
    columns -- see `app.services.admin_data.template`.
    """
    content = build_template_workbook()
    return Response(
        content=content,
        media_type=_XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="orl_admin_data_template.xlsx"'},
    )


@router.post("/upload", response_model=UploadResponse)
async def upload_admin_data(
    combined_file: UploadFile | None = File(
        default=None,
        description="A single .xlsx with a 'Workers' sheet and/or a 'Shifts' sheet.",
    ),
    workers_csv: UploadFile | None = File(default=None, description="Standalone Workers CSV."),
    shifts_csv: UploadFile | None = File(default=None, description="Standalone Shifts CSV."),
    period_start: date_ | None = Form(
        default=None, description="Overrides the inferred Shifts replacement window's start."
    ),
    period_end: date_ | None = Form(
        default=None, description="Overrides the inferred Shifts replacement window's end."
    ),
    dry_run: bool = Form(
        default=False,
        description="Validate and compute the summary without persisting anything (rolled back).",
    ),
    session: AsyncSession = Depends(get_session),
) -> UploadResponse:
    """Bulk-upload Workers and/or Shifts. At least one of `combined_file`,
    `workers_csv`, `shifts_csv` is required. See
    `app.services.admin_data.upload.process_upload` for the full-replace
    semantics this applies.
    """
    if combined_file is None and workers_csv is None and shifts_csv is None:
        raise HTTPException(
            status_code=422,
            detail="at least one of combined_file, workers_csv, shifts_csv is required",
        )

    combined_bytes = await combined_file.read() if combined_file is not None else None
    workers_csv_text = (
        (await workers_csv.read()).decode("utf-8-sig") if workers_csv is not None else None
    )
    shifts_csv_text = (
        (await shifts_csv.read()).decode("utf-8-sig") if shifts_csv is not None else None
    )

    try:
        outcome = await process_upload(
            session,
            combined_bytes=combined_bytes,
            workers_csv_text=workers_csv_text,
            shifts_csv_text=shifts_csv_text,
            period_start=period_start,
            period_end=period_end,
            dry_run=dry_run,
        )
    except UploadValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc

    return UploadResponse(
        dry_run=outcome.dry_run,
        workers=outcome.workers,
        shifts=outcome.shifts,
    )
