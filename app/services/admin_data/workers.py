"""Worker create/update/bulk-upsert -- the DB-facing boundary behind
``POST``/``PATCH /workers`` and the Workers half of
``POST /admin/data/upload``.

This module never commits -- callers (``app/api/workers.py``,
``app/services/admin_data/upload.py``) control the transaction, since some
callers (e.g. Shift-side placeholder generation composed with a Worker op,
or a dry-run upload) need to compose several service calls into one
commit/rollback decision. See ``app/api/workers.py`` for where the commit
happens for the single-row endpoints.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import EmploymentType
from app.models.site import Site
from app.models.worker import Worker
from app.services.admin_data.errors import (
    DuplicateEmployeeCodeError,
    SiteNotFoundError,
    WorkerNotFoundError,
)


@dataclass
class WorkerCreateData:
    """Validated input for ``create_worker``, mirroring
    ``app.schemas.workers.WorkerCreateRequest``.
    """

    name: str
    skills: list[str]
    region: str | None
    home_site_id: int
    active: bool = True
    employee_code: str | None = None
    # Award fields (see `Worker`'s docstring) -- stored as given, never
    # interpreted here.
    award_code: str | None = None
    classification_level: str | None = None
    employment_type: EmploymentType | None = None
    over_award_rate: float | None = None
    ordinary_hours_per_week: float | None = None
    agreed_ordinary_hours_per_shift: float | None = None


async def _check_employee_code_free(
    session: AsyncSession, employee_code: str, *, exclude_worker_id: int | None = None
) -> None:
    stmt = select(Worker.id).where(Worker.employee_code == employee_code)
    if exclude_worker_id is not None:
        stmt = stmt.where(Worker.id != exclude_worker_id)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        raise DuplicateEmployeeCodeError(employee_code)


async def create_worker(session: AsyncSession, data: WorkerCreateData) -> Worker:
    """Create one Worker. Raises ``SiteNotFoundError`` if ``home_site_id``
    doesn't resolve, ``DuplicateEmployeeCodeError`` if ``employee_code`` is
    already taken.
    """
    site = (
        await session.execute(select(Site).where(Site.id == data.home_site_id))
    ).scalar_one_or_none()
    if site is None:
        raise SiteNotFoundError(site_id=data.home_site_id)
    if data.employee_code:
        await _check_employee_code_free(session, data.employee_code)

    worker = Worker(
        name=data.name,
        skills=list(data.skills),
        region=data.region,
        home_site_id=data.home_site_id,
        active=data.active,
        employee_code=data.employee_code,
        award_code=data.award_code,
        classification_level=data.classification_level,
        employment_type=data.employment_type,
        over_award_rate=data.over_award_rate,
        ordinary_hours_per_week=data.ordinary_hours_per_week,
        agreed_ordinary_hours_per_shift=data.agreed_ordinary_hours_per_shift,
    )
    session.add(worker)
    await session.flush()
    return worker


async def update_worker(session: AsyncSession, worker_id: int, updates: dict) -> Worker:
    """Partially update Worker ``worker_id`` with whichever of
    ``name``/``skills``/``region``/``home_site_id``/``active``/
    ``employee_code`` (or award-field) keys are present in ``updates`` (built by the API
    layer via ``payload.model_dump(exclude_unset=True)``, so an omitted
    field is genuinely left alone, not reset to a default).
    """
    worker = (
        await session.execute(select(Worker).where(Worker.id == worker_id))
    ).scalar_one_or_none()
    if worker is None:
        raise WorkerNotFoundError(worker_id)

    if updates.get("home_site_id") is not None:
        site = (
            await session.execute(select(Site).where(Site.id == updates["home_site_id"]))
        ).scalar_one_or_none()
        if site is None:
            raise SiteNotFoundError(site_id=updates["home_site_id"])

    if updates.get("employee_code"):
        await _check_employee_code_free(
            session, updates["employee_code"], exclude_worker_id=worker_id
        )

    for field_name, value in updates.items():
        setattr(worker, field_name, value)

    await session.flush()
    return worker


@dataclass
class WorkerUploadRow:
    """One validated Workers row from a bulk upload (csv or xlsx sheet),
    built by ``app.services.admin_data.upload``. Mirrors the "Workers
    sheet/CSV columns" shape from the task brief.
    """

    name: str
    skills: list[str]
    region: str | None
    home_site_code: str
    active: bool
    employee_code: str | None


@dataclass
class WorkersUpsertSummary:
    """What the API/frontend needs to render a "what happened" report for
    the Workers half of a bulk upload -- see this module's docstring and
    ``upsert_workers_from_rows`` for the full-replace semantics this
    reports on.
    """

    created: int = 0
    updated: int = 0
    deactivated: int = 0
    matched_by_employee_code: int = 0
    matched_by_name: int = 0
    # Names matched by the less-reliable case-insensitive-name fallback --
    # surfaced explicitly so an admin can double check these weren't
    # accidental near-duplicates (see this module's docstring).
    matched_by_name_workers: list[str] = field(default_factory=list)
    deactivated_workers: list[dict] = field(default_factory=list)  # [{"id": int, "name": str}, ...]


async def upsert_workers_from_rows(
    session: AsyncSession, rows: Sequence[WorkerUploadRow], sites_by_code: dict[str, Site]
) -> WorkersUpsertSummary:
    """Full-replace semantics for a Workers upload.

    Every row is upserted by ``employee_code`` when present, else by exact
    case-insensitive ``name`` match (flagged in the summary as less
    reliable -- names collide or get retyped slightly differently across
    uploads, unlike a stable employee code). Any currently-**active**
    ``Worker`` not matched by any row in this upload is soft-deactivated
    (``active=False``) -- **never hard-deleted**, since ``Worker`` is an FK
    target for ``AwardCostMatrix``/``RosterAssignment`` history that must
    survive a worker "leaving" the roster.

    ``sites_by_code`` must already contain every ``row.home_site_code``
    referenced (the caller -- ``app.services.admin_data.upload`` --
    resolves and validates these up front, across both the Workers and
    Shifts halves of an upload, so a bad site code is one combined
    validation error rather than a mid-upsert KeyError here).
    """
    all_workers = (await session.execute(select(Worker))).scalars().all()
    by_employee_code: dict[str, Worker] = {
        w.employee_code: w for w in all_workers if w.employee_code
    }
    by_name_lower: dict[str, Worker] = {}
    for w in all_workers:
        by_name_lower.setdefault(w.name.lower(), w)

    summary = WorkersUpsertSummary()
    matched_ids: set[int] = set()

    for row in rows:
        site = sites_by_code[row.home_site_code]
        matched_worker: Worker | None = None
        matched_by: str | None = None
        if row.employee_code:
            matched_worker = by_employee_code.get(row.employee_code)
            if matched_worker is not None:
                matched_by = "employee_code"
        if matched_worker is None:
            candidate = by_name_lower.get(row.name.lower())
            if candidate is not None:
                matched_worker = candidate
                matched_by = "name"

        if matched_worker is None:
            worker = Worker(
                name=row.name,
                skills=list(row.skills),
                region=row.region,
                home_site_id=site.id,
                active=row.active,
                employee_code=row.employee_code,
            )
            session.add(worker)
            await session.flush()
            summary.created += 1
            matched_ids.add(worker.id)
            if row.employee_code:
                by_employee_code[row.employee_code] = worker
            by_name_lower.setdefault(row.name.lower(), worker)
        else:
            matched_worker.name = row.name
            matched_worker.skills = list(row.skills)
            matched_worker.region = row.region
            matched_worker.home_site_id = site.id
            matched_worker.active = row.active
            if row.employee_code:
                matched_worker.employee_code = row.employee_code
                by_employee_code[row.employee_code] = matched_worker
            summary.updated += 1
            matched_ids.add(matched_worker.id)
            if matched_by == "name":
                summary.matched_by_name += 1
                summary.matched_by_name_workers.append(matched_worker.name)
            else:
                summary.matched_by_employee_code += 1

    for worker in all_workers:
        if worker.active and worker.id not in matched_ids:
            worker.active = False
            summary.deactivated += 1
            summary.deactivated_workers.append({"id": worker.id, "name": worker.name})

    await session.flush()
    return summary
