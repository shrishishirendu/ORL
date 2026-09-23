"""Integration tests for the Workers admin data-entry endpoints:
`POST /workers`, `PATCH /workers/{id}`.

Hits a real Postgres through the actual FastAPI app over
`httpx.AsyncClient`, same pattern as `tests/integration/test_read_api.py`
-- no Redis needed, these aren't job-enqueuing endpoints.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.integration.factories import make_site

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_create_worker_returns_the_created_row(client, db_session) -> None:
    site = await make_site(db_session, "W-SITE-1", region="north")
    await db_session.commit()

    response = await client.post(
        "/workers",
        json={
            "name": "Priya Sharma",
            "skills": ["nursing", "driving"],
            "region": "north",
            "home_site_id": site.id,
            "employee_code": "EMP-100",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Priya Sharma"
    assert body["skills"] == ["nursing", "driving"]
    assert body["home_site"]["id"] == site.id
    assert body["active"] is True
    assert body["employee_code"] == "EMP-100"


async def test_create_worker_rejects_unknown_home_site(client) -> None:
    response = await client.post(
        "/workers",
        json={"name": "No Site", "skills": ["nursing"], "home_site_id": 999999},
    )
    assert response.status_code == 422


async def test_create_worker_rejects_empty_skills(client, db_session) -> None:
    site = await make_site(db_session, "W-SITE-2")
    await db_session.commit()
    response = await client.post(
        "/workers", json={"name": "No Skills", "skills": [], "home_site_id": site.id}
    )
    assert response.status_code == 422


async def test_create_worker_rejects_duplicate_employee_code(client, db_session) -> None:
    site = await make_site(db_session, "W-SITE-3")
    await db_session.commit()
    first = await client.post(
        "/workers",
        json={
            "name": "First",
            "skills": ["nursing"],
            "home_site_id": site.id,
            "employee_code": "DUP-1",
        },
    )
    assert first.status_code == 201

    second = await client.post(
        "/workers",
        json={
            "name": "Second",
            "skills": ["nursing"],
            "home_site_id": site.id,
            "employee_code": "DUP-1",
        },
    )
    assert second.status_code == 409


async def test_patch_worker_partial_update(client, db_session) -> None:
    site = await make_site(db_session, "W-SITE-4")
    other_site = await make_site(db_session, "W-SITE-5")
    await db_session.commit()

    created = await client.post(
        "/workers", json={"name": "Original Name", "skills": ["nursing"], "home_site_id": site.id}
    )
    worker_id = created.json()["id"]

    patched = await client.patch(
        f"/workers/{worker_id}", json={"active": False, "home_site_id": other_site.id}
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["name"] == "Original Name"  # untouched
    assert body["active"] is False
    assert body["home_site"]["id"] == other_site.id


async def test_patch_worker_not_found(client) -> None:
    response = await client.patch("/workers/999999", json={"active": False})
    assert response.status_code == 404


async def test_patch_worker_duplicate_employee_code(client, db_session) -> None:
    site = await make_site(db_session, "W-SITE-6")
    await db_session.commit()

    a = await client.post(
        "/workers",
        json={
            "name": "A",
            "skills": ["nursing"],
            "home_site_id": site.id,
            "employee_code": "CODE-A",
        },
    )
    b = await client.post(
        "/workers",
        json={
            "name": "B",
            "skills": ["nursing"],
            "home_site_id": site.id,
            "employee_code": "CODE-B",
        },
    )
    assert a.status_code == 201 and b.status_code == 201

    response = await client.patch(f"/workers/{b.json()['id']}", json={"employee_code": "CODE-A"})
    assert response.status_code == 409
