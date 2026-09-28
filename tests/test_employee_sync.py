"""Unit tests for the employee sync: the award-intelligence client (over an
httpx MockTransport) and the pure report builder. The DB-backed endpoint is
covered in tests/integration/test_employee_sync.py.
"""

from __future__ import annotations

import httpx
import pytest

from app.models.enums import EmploymentType
from app.schemas.employee_sync import FieldOutcome, SyncEmployeesResponse
from app.services.employee_sync.client import (
    EmployeeMasterClient,
    EmployeeMasterError,
    EmployeeMasterExport,
    EmployeeMasterUnavailable,
    parse_export,
)
from app.services.employee_sync.report import WorkerSnapshot, build_report, mapped_value
from app.services.employee_sync.service import next_steps


def master_body(*employees: dict) -> dict:
    return {
        "ok": True,
        "schemaVersion": "employee-master-export/v1",
        "source": {"auditId": "audit-7", "createdAt": "2026-09-29T00:00:00Z", "sourceName": "Nsw_Payroll.xlsx", "lastDate": "2018-03-26"},
        "employees": list(employees),
    }


def employee(employee_id: str, **fields: str) -> dict:
    return {"employeeId": employee_id, "employmentType": "Casual", "awardCode": "MA000016-NSW", "stateCode": "NSW", **fields}


def export(*employees: dict) -> EmployeeMasterExport:
    return parse_export(master_body(*employees))


def worker(worker_id: int, code: str | None, *, award: str | None = None, employment: EmploymentType | None = None) -> WorkerSnapshot:
    return WorkerSnapshot(id=worker_id, name=f"Worker {worker_id}", employee_code=code, award_code=award, employment_type=employment)


def fields_of(report: SyncEmployeesResponse, worker_id: int) -> dict[str, FieldOutcome]:
    entry = next(e for e in report.matched if e.worker_id == worker_id)
    return {f.field: f.outcome for f in entry.fields}


# --- client --------------------------------------------------------------------


def client_answering(handler) -> EmployeeMasterClient:
    return EmployeeMasterClient("http://ai.test/", "secret", transport=httpx.MockTransport(handler))


async def test_client_sends_the_token_and_parses_the_export() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=master_body(employee("30458", employeeRank="Level 3")))

    async with client_answering(handler) as client:
        result = await client.employee_master()

    assert seen[0].url.path == "/api/employee-master"
    assert seen[0].headers["authorization"] == "Bearer secret"
    assert result.source.source_name == "Nsw_Payroll.xlsx"
    assert result.employees[0].employee_id == "30458"
    assert result.employees[0].award_code == "MA000016-NSW"
    assert result.employees[0].employee_rank == "Level 3"


@pytest.mark.parametrize(
    ("status", "error"),
    [(503, EmployeeMasterUnavailable), (500, EmployeeMasterUnavailable), (403, EmployeeMasterError), (404, EmployeeMasterError)],
)
async def test_client_maps_error_statuses(status: int, error: type[Exception]) -> None:
    async with client_answering(lambda _r: httpx.Response(status, json={"error": "nope"})) as client:
        with pytest.raises(error):
            await client.employee_master()


async def test_client_treats_a_network_failure_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with client_answering(handler) as client:
        with pytest.raises(EmployeeMasterUnavailable):
            await client.employee_master()


async def test_client_rejects_the_wrong_schema() -> None:
    async with client_answering(lambda _r: httpx.Response(200, json={"ok": True, "employees": []})) as client:
        with pytest.raises(EmployeeMasterError, match="employee-master-export/v1"):
            await client.employee_master()


# --- report --------------------------------------------------------------------


def test_matches_on_employee_code_and_groups_the_rest() -> None:
    report = build_report(
        [worker(1, "30458"), worker(2, "99999"), worker(3, None), worker(4, " 10001 ")],
        export(employee("30458"), employee("10001"), employee("55555")),
        jurisdiction="NSW",
    )
    assert [m.worker_id for m in report.matched] == [1, 4]
    assert [w.worker_id for w in report.orl_only] == [2]
    assert [w.worker_id for w in report.orl_without_code] == [3]
    assert [e.employee_id for e in report.master_only] == ["55555"]
    assert report.summary.model_dump() == {
        "orl_workers": 4, "master_employees": 3, "matched": 2, "fills": 4, "conflicts": 0,
        "unmappable": 0, "orl_only": 1, "orl_without_code": 1, "master_only": 1,
    }


def test_blank_orl_fields_are_fills_with_mapped_values() -> None:
    report = build_report([worker(1, "30458")], export(employee("30458")), jurisdiction="NSW")
    entry = report.matched[0]
    assert fields_of(report, 1) == {"employment_type": FieldOutcome.FILL, "award_code": FieldOutcome.FILL}
    assert [mapped_value(f) for f in entry.fields] == [EmploymentType.CASUAL, "MA000016"]


def test_equal_values_are_same_after_mapping() -> None:
    report = build_report(
        [worker(1, "30458", award="MA000016", employment=EmploymentType.CASUAL)],
        export(employee("30458")),
        jurisdiction="NSW",
    )
    assert fields_of(report, 1) == {"employment_type": FieldOutcome.SAME, "award_code": FieldOutcome.SAME}
    assert report.matched[0].notes == []


def test_differing_values_are_conflicts() -> None:
    report = build_report(
        [worker(1, "30458", award="MA000034", employment=EmploymentType.FULL_TIME)],
        export(employee("30458")),
        jurisdiction="NSW",
    )
    assert fields_of(report, 1) == {"employment_type": FieldOutcome.CONFLICT, "award_code": FieldOutcome.CONFLICT}
    assert report.summary.conflicts == 2


def test_unknown_and_blank_master_values() -> None:
    report = build_report(
        [worker(1, "30458")],
        export(employee("30458", employmentType="Contractor", awardCode="")),
        jurisdiction="NSW",
    )
    assert fields_of(report, 1) == {"employment_type": FieldOutcome.UNMAPPABLE, "award_code": FieldOutcome.MASTER_BLANK}


def test_notes_a_state_suffix_outside_orl_jurisdiction() -> None:
    report = build_report([worker(1, "30458")], export(employee("30458", awardCode="MA000016-VIC")), jurisdiction="NSW")
    assert fields_of(report, 1)["award_code"] is FieldOutcome.FILL
    assert "MA000016-VIC" in report.matched[0].notes[0]


def test_next_steps_describe_what_is_left() -> None:
    report = build_report(
        [worker(1, "30458", award="MA000034"), worker(2, None)],
        export(employee("30458"), employee("55555")),
        jurisdiction="NSW",
    )
    steps = " ".join(next_steps(report))
    assert "run again with apply" in steps
    assert "1 conflict" in steps
    assert "Only in award-intelligence: 1 employee." in steps
    assert "No employee_code, so they can't be matched: 1 ORL worker." in steps
    assert "classification_level isn't synced" in steps
