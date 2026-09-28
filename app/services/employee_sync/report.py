"""Pure comparison of ORL workers against the award-intelligence employee
master. No DB, no HTTP: `service.py` feeds it and applies its fills.

Matching is exact on ``Worker.employee_code`` == ``employeeId`` (both are the
raw payroll number). Names are never used to match: the employee master
carries none, and name matching is the ambiguity ``employee_code`` exists
to avoid (see ``Worker``'s docstring).

Fields compared, with how award-intelligence values map to ORL's:

- ``employment_type``: "Full-time" / "Part-time" / "Casual" ->
  ``full_time`` / ``part_time`` / ``casual``. Anything else is unmappable.
- ``award_code``: the payroll's state-scoped code, e.g. "MA000016-NSW" ->
  "MA000016". The state suffix is checked against ORL's
  ``AWARD_JURISDICTION`` and a mismatch is noted, because ORL prices every
  worker under that one jurisdiction.

ORL never interprets these values beyond that renaming: which award or
employment type is *right* is for a person to decide, so a difference is a
conflict, never an overwrite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.enums import EmploymentType
from app.schemas.employee_sync import (
    FieldComparison,
    FieldOutcome,
    MasterOnlyEmployee,
    MatchedEmployee,
    OrlOnlyWorker,
    SyncEmployeesResponse,
    SyncSource,
    SyncSummary,
)
from app.services.employee_sync.client import EmployeeMasterExport, MasterEmployee

_EMPLOYMENT_TYPES = {
    "full-time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "casual": EmploymentType.CASUAL,
}
_AWARD_CODE = re.compile(r"^(MA\d{6})(?:-([A-Z]{2,3}))?$")


@dataclass(frozen=True)
class WorkerSnapshot:
    id: int
    name: str
    employee_code: str | None
    award_code: str | None
    employment_type: EmploymentType | None


def _employment_type(raw: str) -> EmploymentType | None:
    return _EMPLOYMENT_TYPES.get(raw.strip().lower())


def _award_code(raw: str) -> re.Match[str] | None:
    return _AWARD_CODE.match(raw.strip().upper())


def mapped_value(comparison: FieldComparison) -> str | EmploymentType | None:
    """The ORL value that a FILL comparison writes."""
    if comparison.master_value is None:
        return None
    if comparison.field == "employment_type":
        return _employment_type(comparison.master_value)
    match = _award_code(comparison.master_value)
    return match.group(1) if match else None


def _compare(
    field: str, orl_value: str | None, master_raw: str, mapped: str | None
) -> FieldComparison:
    if not master_raw:
        outcome = FieldOutcome.MASTER_BLANK
    elif mapped is None:
        outcome = FieldOutcome.UNMAPPABLE
    elif not orl_value:
        outcome = FieldOutcome.FILL
    elif orl_value == mapped:
        outcome = FieldOutcome.SAME
    else:
        outcome = FieldOutcome.CONFLICT
    return FieldComparison(
        field=field,
        orl_value=orl_value,
        master_value=master_raw or None,
        outcome=outcome,
        fill_value=mapped if outcome is FieldOutcome.FILL else None,
    )


def _match(worker: WorkerSnapshot, master: MasterEmployee, jurisdiction: str) -> MatchedEmployee:
    employment = _employment_type(master.employment_type)
    award = _award_code(master.award_code)
    notes: list[str] = []
    if award and award.group(2) and award.group(2) != jurisdiction:
        notes.append(
            f"The payroll assigns {master.award_code.strip()}, but ORL prices every worker under "
            f"{jurisdiction} (AWARD_JURISDICTION)."
        )
    return MatchedEmployee(
        worker_id=worker.id,
        worker_name=worker.name,
        employee_code=(worker.employee_code or "").strip(),
        fields=[
            _compare(
                "employment_type",
                worker.employment_type.value if worker.employment_type else None,
                master.employment_type.strip(),
                employment.value if employment else None,
            ),
            _compare(
                "award_code",
                worker.award_code,
                master.award_code.strip(),
                award.group(1) if award else None,
            ),
        ],
        notes=notes,
    )


def build_report(
    workers: list[WorkerSnapshot], export: EmployeeMasterExport, *, jurisdiction: str
) -> SyncEmployeesResponse:
    master_by_id = {employee.employee_id.strip(): employee for employee in export.employees}
    matched: list[MatchedEmployee] = []
    orl_only: list[OrlOnlyWorker] = []
    orl_without_code: list[OrlOnlyWorker] = []
    seen: set[str] = set()

    for worker in sorted(workers, key=lambda w: w.id):
        code = (worker.employee_code or "").strip()
        if not code:
            orl_without_code.append(
                OrlOnlyWorker(worker_id=worker.id, worker_name=worker.name, employee_code=None)
            )
        elif code in master_by_id:
            seen.add(code)
            matched.append(_match(worker, master_by_id[code], jurisdiction))
        else:
            orl_only.append(
                OrlOnlyWorker(worker_id=worker.id, worker_name=worker.name, employee_code=code)
            )

    master_only = [
        MasterOnlyEmployee(
            employee_id=employee_id,
            employment_type=employee.employment_type,
            award_code=employee.award_code,
            state_code=employee.state_code,
            source_classification=employee.source_classification,
        )
        for employee_id, employee in sorted(master_by_id.items())
        if employee_id not in seen
    ]

    outcomes = [field.outcome for entry in matched for field in entry.fields]
    source = export.source
    return SyncEmployeesResponse(
        applied=False,
        source=SyncSource(
            audit_id=source.audit_id,
            created_at=source.created_at,
            source_name=source.source_name,
            last_date=source.last_date,
        ),
        summary=SyncSummary(
            orl_workers=len(workers),
            master_employees=len(master_by_id),
            matched=len(matched),
            fills=outcomes.count(FieldOutcome.FILL),
            conflicts=outcomes.count(FieldOutcome.CONFLICT),
            unmappable=outcomes.count(FieldOutcome.UNMAPPABLE),
            orl_only=len(orl_only),
            orl_without_code=len(orl_without_code),
            master_only=len(master_only),
        ),
        matched=matched,
        orl_only=orl_only,
        orl_without_code=orl_without_code,
        master_only=master_only,
        next_steps=[],
    )
