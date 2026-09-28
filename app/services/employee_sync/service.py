"""Run the employee sync: read the award-intelligence employee master,
compare it with ORL's workers (``report.py``) and, on ``apply``, fill blank
ORL fields.

Owner decisions (docs/AWARD_INTEGRATION.md, 2026-09-28/29): both systems keep
their own data; the sync is explicit; it fills only fields ORL has left
blank; every difference is reported and nothing is silently overwritten.
The caller owns the transaction (commit on success).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.worker import Worker
from app.schemas.employee_sync import FieldOutcome, SyncEmployeesResponse
from app.services.employee_sync.client import EmployeeMasterClient
from app.services.employee_sync.report import WorkerSnapshot, build_report, mapped_value


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def next_steps(report: SyncEmployeesResponse) -> list[str]:
    s = report.summary
    steps: list[str] = []
    if s.fills and not report.applied:
        steps.append(
            f"{_plural(s.fills, 'blank ORL field')} can be filled from the employee master: "
            "run again with apply."
        )
    if report.applied and s.fills:
        steps.append(
            "Award fields changed: re-run the award cost sync (POST /award-engine/sync-matrix) "
            "so roster costs use them."
        )
    if s.conflicts:
        steps.append(
            f"{_plural(s.conflicts, 'conflict')}: both systems hold a different value. "
            "Decide which is right and correct the record in the other system. "
            "The sync never overwrites."
        )
    if s.unmappable:
        steps.append(
            "Not expressible in ORL, check by hand: "
            f"{_plural(s.unmappable, 'employee-master value')}."
        )
    if s.master_only:
        steps.append(
            f"Only in award-intelligence: {_plural(s.master_only, 'employee')}. "
            "Add them in ORL with their employee_code (skills and a home site are needed, "
            "and aren't in the employee master)."
        )
    if s.orl_only:
        steps.append(
            f"Not in the latest payroll import: {_plural(s.orl_only, 'ORL worker')} "
            "(employee_code not found)."
        )
    if s.orl_without_code:
        steps.append(
            "No employee_code, so they can't be matched: "
            f"{_plural(s.orl_without_code, 'ORL worker')}."
        )
    if s.matched:
        steps.append(
            "classification_level isn't synced: the employee master has no engine level key. "
            "Set it in ORL."
        )
    return steps


async def sync_employees(
    session: AsyncSession, client: EmployeeMasterClient, *, apply: bool, jurisdiction: str
) -> SyncEmployeesResponse:
    async with client:
        export = await client.employee_master()

    workers = list((await session.execute(select(Worker).order_by(Worker.id))).scalars().all())
    report = build_report(
        [
            WorkerSnapshot(
                id=w.id,
                name=w.name,
                employee_code=w.employee_code,
                award_code=w.award_code,
                employment_type=w.employment_type,
            )
            for w in workers
        ],
        export,
        jurisdiction=jurisdiction,
    )

    if apply:
        by_id = {w.id: w for w in workers}
        for entry in report.matched:
            for comparison in entry.fields:
                if comparison.outcome is not FieldOutcome.FILL:
                    continue
                setattr(by_id[entry.worker_id], comparison.field, mapped_value(comparison))
                comparison.applied = True
        report.applied = True
        await session.flush()

    report.next_steps = next_steps(report)
    return report
