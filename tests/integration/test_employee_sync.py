"""Integration tests for `POST /workers/sync-employees`: real Postgres, the
real FastAPI app, and award-intelligence stood in by an httpx MockTransport
(patched in through `client_from_settings`).
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models.enums import EmploymentType
from app.models.worker import Worker
from app.services.employee_sync import client as client_module
from app.services.employee_sync.client import EmployeeMasterClient
from tests.integration.factories import make_site, make_worker

pytestmark = pytest.mark.asyncio


def master_body(*employees: dict) -> dict:
    return {
        "ok": True,
        "schemaVersion": "employee-master-export/v1",
        "source": {"auditId": "audit-7", "createdAt": "2026-09-29T00:00:00Z", "sourceName": "Nsw_Payroll.xlsx", "lastDate": "2018-03-26"},
        "employees": list(employees),
    }


def employee(employee_id: str, **fields: str) -> dict:
    return {"employeeId": employee_id, "employmentType": "Casual", "awardCode": "MA000016-NSW", "stateCode": "NSW", **fields}


def serve(monkeypatch, status: int = 200, body: dict | None = None) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(status, json=body or {}))
    monkeypatch.setattr(
        client_module,
        "client_from_settings",
        lambda: EmployeeMasterClient("http://ai.test", "secret", transport=transport),
    )


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def seed(db_session) -> dict[str, int]:
    site = await make_site(db_session, "SYNC-SITE", region="north")
    blank = await make_worker(db_session, site, name="Blank Fields", skills=["guard"])
    blank.employee_code = "30458"
    conflicting = await make_worker(
        db_session, site, name="Conflicting", skills=["guard"],
        award_code="MA000034", employment_type=EmploymentType.FULL_TIME,
    )
    conflicting.employee_code = "10001"
    unmatched = await make_worker(db_session, site, name="Not In Payroll", skills=["guard"])
    unmatched.employee_code = "77777"
    await make_worker(db_session, site, name="No Code", skills=["guard"])
    await db_session.commit()
    return {"blank": blank.id, "conflicting": conflicting.id}


async def worker_row(db_session, worker_id: int) -> Worker:
    db_session.expire_all()
    return (await db_session.execute(select(Worker).where(Worker.id == worker_id))).scalar_one()


async def test_preview_reports_without_writing(client, db_session, monkeypatch) -> None:
    ids = await seed(db_session)
    serve(monkeypatch, body=master_body(employee("30458"), employee("10001"), employee("55555")))

    response = await client.post("/workers/sync-employees", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is False
    assert body["summary"] == {
        "orl_workers": 4, "master_employees": 3, "matched": 2, "fills": 2, "conflicts": 2,
        "unmappable": 0, "orl_only": 1, "orl_without_code": 1, "master_only": 1,
    }
    assert body["source"]["source_name"] == "Nsw_Payroll.xlsx"
    assert [e["employee_id"] for e in body["master_only"]] == ["55555"]
    blank = await worker_row(db_session, ids["blank"])
    assert blank.award_code is None and blank.employment_type is None


async def test_apply_fills_blanks_and_never_overwrites(client, db_session, monkeypatch) -> None:
    ids = await seed(db_session)
    serve(monkeypatch, body=master_body(employee("30458"), employee("10001")))

    response = await client.post("/workers/sync-employees", json={"apply": True})

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    filled = next(m for m in body["matched"] if m["worker_id"] == ids["blank"])
    assert all(f["applied"] for f in filled["fields"])
    conflicting = next(m for m in body["matched"] if m["worker_id"] == ids["conflicting"])
    assert {f["outcome"] for f in conflicting["fields"]} == {"conflict"}
    assert not any(f["applied"] for f in conflicting["fields"])
    assert any("sync-matrix" in step for step in body["next_steps"])

    blank = await worker_row(db_session, ids["blank"])
    assert blank.award_code == "MA000016"
    assert blank.employment_type is EmploymentType.CASUAL
    kept = await worker_row(db_session, ids["conflicting"])
    assert kept.award_code == "MA000034"
    assert kept.employment_type is EmploymentType.FULL_TIME


async def test_not_configured_is_503(client, monkeypatch) -> None:
    monkeypatch.setattr(client_module, "client_from_settings", lambda: None)
    response = await client.post("/workers/sync-employees", json={})
    assert response.status_code == 503
    assert response.json()["detail"] == "award-intelligence not configured"


async def test_unreachable_is_503_and_a_refusal_is_502(client, db_session, monkeypatch) -> None:
    await seed(db_session)
    serve(monkeypatch, status=503, body={"error": "down"})
    assert (await client.post("/workers/sync-employees", json={"apply": True})).status_code == 503

    serve(monkeypatch, status=403, body={"error": "An API token is required for this operation."})
    response = await client.post("/workers/sync-employees", json={"apply": True})
    assert response.status_code == 502
    assert "403" in response.json()["detail"]
