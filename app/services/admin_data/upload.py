"""Parsing + orchestration for ``POST /admin/data/upload``.

Accepts a combined ``.xlsx`` (a ``Workers`` sheet and/or a ``Shifts``
sheet), and/or standalone ``workers_csv``/``shifts_csv`` files -- all
optional, but at least one source must be present (the API layer enforces
that before calling in here). The column names/shapes parsed below are the
source of truth the task brief points to, and exactly what
``app.services.admin_data.template.build_template_workbook`` produces one
example row of -- keep the two in lockstep if either changes.

**Validation-then-commit, not partial application.** Every row from every
source is parsed and validated *before* any database write happens
(``process_upload`` raises ``UploadValidationError`` with the full list of
problems found, across both Workers and Shifts, if there are any). This is
what makes the "everything in one DB transaction -- roll back the whole
thing on any row failure" requirement simple: nothing is written until
validation has already passed for the entire upload.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime
from datetime import time as time_

import openpyxl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.site import Site
from app.services.admin_data.errors import UploadValidationError
from app.services.admin_data.shifts import (
    ShiftsReplaceSummary,
    ShiftUploadRow,
    replace_shifts_for_period,
)
from app.services.admin_data.workers import (
    WorkersUpsertSummary,
    WorkerUploadRow,
    upsert_workers_from_rows,
)

WORKERS_SHEET = "Workers"
SHIFTS_SHEET = "Shifts"
WORKERS_COLUMNS = ["name", "skills", "region", "home_site_code", "active", "employee_code"]
SHIFTS_COLUMNS = ["date", "start_time", "end_time", "required_skill", "site_code", "is_multi_stop"]

_TRUE_STRINGS = {"true", "1", "yes", "y"}
_FALSE_STRINGS = {"false", "0", "no", "n"}


def _parse_bool(raw: object, *, default: bool) -> bool | None:
    """bool-ish -> bool, per the task brief ("true/false/1/0/yes/no").
    Returns ``None`` (not a fallback default) when `raw` is present but
    unparseable, so the caller can turn that into a validation error
    instead of silently guessing.
    """
    if isinstance(raw, bool):
        return raw
    if raw is None or str(raw).strip() == "":
        return default
    s = str(raw).strip().lower()
    if s in _TRUE_STRINGS:
        return True
    if s in _FALSE_STRINGS:
        return False
    return None


def _parse_skills(raw: object) -> list[str]:
    """ "nursing, first_aid" -> ["nursing", "first_aid"]."""
    return [s.strip() for s in str(raw or "").split(",") if s.strip()]


def _parse_hhmm(raw: object) -> time_:
    if raw is None:
        raise ValueError("missing time value")
    if isinstance(raw, time_):
        return raw
    if isinstance(raw, datetime):
        return raw.time()
    s = str(raw).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"invalid time value {raw!r} (expected HH:MM)")


def _parse_date(raw: object) -> date_:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date_):
        return raw
    return date_.fromisoformat(str(raw).strip())


def _row_dicts_from_csv(text: str) -> list[dict]:
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def _row_dicts_from_sheet(ws) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    out = []
    for raw_row in rows[1:]:
        if all(c is None or str(c).strip() == "" for c in raw_row):
            continue  # a blank trailing row -- openpyxl often yields these
        out.append({header[i]: raw_row[i] for i in range(len(header)) if i < len(raw_row)})
    return out


def _validate_worker_rows(
    dicts: list[dict], errors: list[str], label: str
) -> list[WorkerUploadRow]:
    rows: list[WorkerUploadRow] = []
    seen_employee_codes: set[str] = set()
    for i, d in enumerate(dicts, start=2):  # row 1 is the header
        name = str(d.get("name") or "").strip()
        if not name:
            errors.append(f"{label} row {i}: missing 'name'")
            continue
        skills = _parse_skills(d.get("skills"))
        if not skills:
            errors.append(
                f"{label} row {i} ({name}): 'skills' must be a non-empty comma-separated list"
            )
            continue
        home_site_code = str(d.get("home_site_code") or "").strip()
        if not home_site_code:
            errors.append(f"{label} row {i} ({name}): missing 'home_site_code'")
            continue
        active = _parse_bool(d.get("active"), default=True)
        if active is None:
            errors.append(
                f"{label} row {i} ({name}): 'active' must be a bool-ish value "
                "(true/false/1/0/yes/no) if given"
            )
            continue
        employee_code = str(d.get("employee_code") or "").strip() or None
        if employee_code:
            if employee_code in seen_employee_codes:
                errors.append(
                    f"{label} row {i} ({name}): duplicate employee_code "
                    f"{employee_code!r} within this upload"
                )
                continue
            seen_employee_codes.add(employee_code)
        region = str(d.get("region") or "").strip() or None
        rows.append(
            WorkerUploadRow(
                name=name,
                skills=skills,
                region=region,
                home_site_code=home_site_code,
                active=active,
                employee_code=employee_code,
            )
        )
    return rows


def _validate_shift_rows(dicts: list[dict], errors: list[str], label: str) -> list[ShiftUploadRow]:
    rows: list[ShiftUploadRow] = []
    for i, d in enumerate(dicts, start=2):
        try:
            date_val = _parse_date(d.get("date"))
        except (ValueError, TypeError):
            errors.append(
                f"{label} row {i}: invalid or missing 'date' (expected ISO date, e.g. 2026-04-01)"
            )
            continue
        try:
            start_val = _parse_hhmm(d.get("start_time"))
            end_val = _parse_hhmm(d.get("end_time"))
        except ValueError as exc:
            errors.append(f"{label} row {i} ({date_val}): {exc}")
            continue
        required_skill = str(d.get("required_skill") or "").strip()
        if not required_skill:
            errors.append(f"{label} row {i} ({date_val}): missing 'required_skill'")
            continue
        site_code = str(d.get("site_code") or "").strip()
        if not site_code:
            errors.append(f"{label} row {i} ({date_val}): missing 'site_code'")
            continue
        is_multi_stop = _parse_bool(d.get("is_multi_stop"), default=False)
        if is_multi_stop is None:
            errors.append(
                f"{label} row {i} ({date_val}): 'is_multi_stop' must be a bool-ish value if given"
            )
            continue
        if is_multi_stop:
            errors.append(
                f"{label} row {i} ({date_val}): is_multi_stop=true is not supported by this "
                "upload -- multi-stop shifts need Job rows, which this endpoint doesn't manage; "
                "use the existing seed/DB path for multi-stop shifts"
            )
            continue
        rows.append(
            ShiftUploadRow(
                date=date_val,
                start_time=start_val,
                end_time=end_val,
                required_skill=required_skill,
                site_code=site_code,
            )
        )
    return rows


@dataclass
class _ParsedSources:
    worker_rows: list[WorkerUploadRow]
    shift_rows: list[ShiftUploadRow]
    have_worker_source: bool
    have_shift_source: bool
    errors: list[str]


def _parse_sources(
    combined_bytes: bytes | None, workers_csv_text: str | None, shifts_csv_text: str | None
) -> _ParsedSources:
    errors: list[str] = []
    worker_dicts: list[dict] = []
    shift_dicts: list[dict] = []
    have_worker_source = False
    have_shift_source = False

    if combined_bytes is not None:
        try:
            wb = openpyxl.load_workbook(io.BytesIO(combined_bytes), data_only=True, read_only=True)
        except Exception as exc:  # noqa: BLE001 -- any openpyxl/zip failure is a user-facing validation error
            errors.append(f"combined_file: could not be read as .xlsx ({exc})")
            wb = None
        if wb is not None:
            found_a_sheet = False
            if WORKERS_SHEET in wb.sheetnames:
                worker_dicts.extend(_row_dicts_from_sheet(wb[WORKERS_SHEET]))
                have_worker_source = True
                found_a_sheet = True
            if SHIFTS_SHEET in wb.sheetnames:
                shift_dicts.extend(_row_dicts_from_sheet(wb[SHIFTS_SHEET]))
                have_shift_source = True
                found_a_sheet = True
            if not found_a_sheet:
                errors.append(
                    f"combined_file: no {WORKERS_SHEET!r} or {SHIFTS_SHEET!r} sheet found"
                )

    if workers_csv_text is not None:
        worker_dicts.extend(_row_dicts_from_csv(workers_csv_text))
        have_worker_source = True

    if shifts_csv_text is not None:
        shift_dicts.extend(_row_dicts_from_csv(shifts_csv_text))
        have_shift_source = True

    if have_worker_source and not worker_dicts:
        errors.append("a Workers source was given but contains no data rows")
    if have_shift_source and not shift_dicts:
        errors.append("a Shifts source was given but contains no data rows")

    worker_rows = _validate_worker_rows(worker_dicts, errors, "Workers") if worker_dicts else []
    shift_rows = _validate_shift_rows(shift_dicts, errors, "Shifts") if shift_dicts else []

    return _ParsedSources(worker_rows, shift_rows, have_worker_source, have_shift_source, errors)


@dataclass
class UploadOutcome:
    """What ``app/api/admin_data.py`` shapes into ``UploadResponse``."""

    dry_run: bool
    workers: WorkersUpsertSummary | None
    shifts: ShiftsReplaceSummary | None


async def process_upload(
    session: AsyncSession,
    *,
    combined_bytes: bytes | None,
    workers_csv_text: str | None,
    shifts_csv_text: str | None,
    period_start: date_ | None,
    period_end: date_ | None,
    dry_run: bool,
) -> UploadOutcome:
    """Parse+validate every provided source, then either apply (commit) or
    preview (roll back) the resulting Workers upsert / Shifts full-replace.

    Raises ``UploadValidationError`` (never touching the database) if any
    row anywhere failed validation. ``period_start``/``period_end`` are
    used verbatim if given; otherwise, when Shifts rows are present, the
    replacement period is inferred as ``min(date)..max(date)`` across them.
    """
    parsed = _parse_sources(combined_bytes, workers_csv_text, shifts_csv_text)
    errors = list(parsed.errors)

    all_site_codes = {r.home_site_code for r in parsed.worker_rows} | {
        r.site_code for r in parsed.shift_rows
    }
    sites_by_code: dict[str, Site] = {}
    if all_site_codes:
        site_rows = (
            (await session.execute(select(Site).where(Site.code.in_(all_site_codes))))
            .scalars()
            .all()
        )
        sites_by_code = {s.code: s for s in site_rows}
        for code in sorted(all_site_codes - sites_by_code.keys()):
            errors.append(f"unknown site code {code!r} referenced in the upload")

    if parsed.have_shift_source and parsed.shift_rows:
        if period_start is None and period_end is None:
            period_start = min(r.date for r in parsed.shift_rows)
            period_end = max(r.date for r in parsed.shift_rows)
        elif period_start is None or period_end is None:
            errors.append("period_start and period_end must both be given, or both omitted")
    # else: either no Shifts source at all, or every Shifts row failed
    # validation (already reported above) -- either way, there's no period
    # to infer and nothing more to add here.

    if errors:
        raise UploadValidationError(errors)

    workers_summary: WorkersUpsertSummary | None = None
    shifts_summary: ShiftsReplaceSummary | None = None

    if parsed.have_worker_source:
        workers_summary = await upsert_workers_from_rows(session, parsed.worker_rows, sites_by_code)

    if parsed.have_shift_source and parsed.shift_rows:
        assert period_start is not None and period_end is not None
        shifts_summary = await replace_shifts_for_period(
            session, period_start, period_end, parsed.shift_rows, sites_by_code
        )

    if dry_run:
        await session.rollback()
    else:
        await session.commit()

    return UploadOutcome(dry_run=dry_run, workers=workers_summary, shifts=shifts_summary)
