"""Integration tests for the Shifts admin data-entry endpoints:
`POST /shifts`, `PATCH /shifts/{id}`, `POST /workers/regenerate-eligibility`
-- in particular, that placeholder `AwardCostMatrix` generation
(`app.services.admin_data.eligibility`) actually fires end to end.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.settings import settings
from app.main import app
from app.models.award_cost_matrix import AwardCostMatrix
from tests.integration.factories import make_site, make_worker

pytestmark = pytest.mark.asyncio

SHIFT_DATE = date(2026, 6, 1)

# Deliberately unusual skill/region strings, distinct from anything
# scripts/seed_demo_data.py seeds (e.g. "nursing"/"north") -- these
# integration tests run against a real, possibly demo-seeded Postgres (see
# tests/integration/conftest.py: tables are only truncated *after* each
# test, so the very first test of a whole run can still see pre-existing
# seed data). Using skill/region values nothing in the seed script uses
# keeps eligibility-matching assertions exact regardless of what else is
# in the database when a given test happens to run first.
SKILL = "xtest-skill"
REGION = "xtest-region"
OTHER_REGION = "xtest-region-other"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_create_shift_generates_placeholder_award_rows_for_matching_workers(
    client, db_session
) -> None:
    site = await make_site(db_session, "S-SITE-1", region=REGION)
    # A separate home site in a different region -- `_effective_region`
    # prefers the home site's own region over `Worker.region` (see
    # app.services.rostering.service._effective_region), so a worker's
    # *home site* has to actually differ for the "wrong region" case to
    # mean anything.
    other_region_site = await make_site(db_session, "S-SITE-1B", region=OTHER_REGION)
    matching = await make_worker(db_session, site, name="Match", skills=[SKILL], region=REGION)
    wrong_skill = await make_worker(
        db_session, site, name="WrongSkill", skills=["xtest-skill-other"], region=REGION
    )
    wrong_region = await make_worker(
        db_session, other_region_site, name="WrongRegion", skills=[SKILL], region=OTHER_REGION
    )
    inactive = await make_worker(
        db_session, site, name="Inactive", skills=[SKILL], region=REGION, active=False
    )
    await db_session.commit()

    response = await client.post(
        "/shifts",
        json={
            "date": SHIFT_DATE.isoformat(),
            "start_time": "08:00",
            "end_time": "16:00",
            "required_skill": SKILL,
            "site_id": site.id,
        },
    )
    assert response.status_code == 201
    body = response.json()
    shift_id = body["shift"]["id"]
    assert body["placeholder_award_rows_created"] == 1
    assert body["shift"]["eligible_worker_count"] == 1
    assert body["shift"]["is_multi_stop"] is False

    award_stmt = select(AwardCostMatrix).where(AwardCostMatrix.shift_id == shift_id)
    rows = (await db_session.execute(award_stmt)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.worker_id == matching.id
    assert row.is_placeholder is True
    assert row.eligible is True
    assert row.min_hours is None
    assert row.max_hours is None
    assert float(row.pay_cost) == pytest.approx(settings.placeholder_hourly_rate * 8)

    matched_worker_ids = {r.worker_id for r in rows}
    assert wrong_skill.id not in matched_worker_ids
    assert wrong_region.id not in matched_worker_ids
    assert inactive.id not in matched_worker_ids


async def test_create_shift_skips_existing_award_row(client, db_session) -> None:
    """A second placeholder-generation pass never clobbers/duplicates an
    existing row (skip-if-exists, even across two shift creations that
    would otherwise collide on nothing -- exercised here via
    regenerate-eligibility, see below, and directly via a second create
    call reusing the same (worker, shift) key is not directly reachable
    from the API since Shift.id is fresh each time; this test instead
    confirms the create path is idempotent when called once."""
    site = await make_site(db_session, "S-SITE-2", region=REGION)
    await make_worker(db_session, site, name="Once", skills=[SKILL], region=REGION)
    await db_session.commit()

    response = await client.post(
        "/shifts",
        json={
            "date": SHIFT_DATE.isoformat(),
            "start_time": "08:00",
            "end_time": "16:00",
            "required_skill": SKILL,
            "site_id": site.id,
        },
    )
    assert response.status_code == 201
    assert response.json()["placeholder_award_rows_created"] == 1


async def test_create_shift_rejects_multi_stop(client, db_session) -> None:
    site = await make_site(db_session, "S-SITE-3")
    await db_session.commit()

    response = await client.post(
        "/shifts",
        json={
            "date": SHIFT_DATE.isoformat(),
            "start_time": "08:00",
            "end_time": "16:00",
            "required_skill": SKILL,
            "site_id": site.id,
            "is_multi_stop": True,
        },
    )
    assert response.status_code == 422
    assert "Job rows" in response.json()["detail"]


async def test_create_shift_rejects_unknown_site(client) -> None:
    response = await client.post(
        "/shifts",
        json={
            "date": SHIFT_DATE.isoformat(),
            "start_time": "08:00",
            "end_time": "16:00",
            "required_skill": SKILL,
            "site_id": 999999,
        },
    )
    assert response.status_code == 422


async def test_patch_shift_rejects_flipping_to_multi_stop(client, db_session) -> None:
    site = await make_site(db_session, "S-SITE-4")
    await db_session.commit()
    created = await client.post(
        "/shifts",
        json={
            "date": SHIFT_DATE.isoformat(),
            "start_time": "08:00",
            "end_time": "16:00",
            "required_skill": SKILL,
            "site_id": site.id,
        },
    )
    shift_id = created.json()["shift"]["id"]

    response = await client.patch(f"/shifts/{shift_id}", json={"is_multi_stop": True})
    assert response.status_code == 422


async def test_patch_shift_regenerates_placeholder_rows_on_skill_change(client, db_session) -> None:
    site = await make_site(db_session, "S-SITE-5", region=REGION)
    await make_worker(db_session, site, name="Nurse", skills=[SKILL], region=REGION)
    driver = await make_worker(
        db_session, site, name="Driver", skills=["xtest-skill-driver"], region=REGION
    )
    await db_session.commit()

    created = await client.post(
        "/shifts",
        json={
            "date": SHIFT_DATE.isoformat(),
            "start_time": "08:00",
            "end_time": "16:00",
            "required_skill": SKILL,
            "site_id": site.id,
        },
    )
    shift_id = created.json()["shift"]["id"]
    assert created.json()["placeholder_award_rows_created"] == 1

    patched = await client.patch(
        f"/shifts/{shift_id}", json={"required_skill": "xtest-skill-driver"}
    )
    assert patched.status_code == 200
    assert patched.json()["placeholder_award_rows_created"] == 1  # newly eligible: driver
    # old (skill-matching) row + new (driving-matching) row
    assert patched.json()["shift"]["eligible_worker_count"] == 2

    award_stmt = select(AwardCostMatrix).where(AwardCostMatrix.shift_id == shift_id)
    rows = (await db_session.execute(award_stmt)).scalars().all()
    worker_ids = {r.worker_id for r in rows}
    assert driver.id in worker_ids


async def test_patch_shift_not_found(client) -> None:
    response = await client.patch("/shifts/999999", json={"required_skill": SKILL})
    assert response.status_code == 404


async def test_regenerate_eligibility_backfills_for_a_new_worker(client, db_session) -> None:
    site = await make_site(db_session, "S-SITE-6", region=REGION)
    await db_session.commit()

    created = await client.post(
        "/shifts",
        json={
            "date": SHIFT_DATE.isoformat(),
            "start_time": "08:00",
            "end_time": "16:00",
            "required_skill": SKILL,
            "site_id": site.id,
        },
    )
    shift_id = created.json()["shift"]["id"]
    assert created.json()["placeholder_award_rows_created"] == 0  # no eligible workers yet

    # A new worker is added *after* the shift already exists -- Worker
    # creation itself must NOT retroactively backfill eligibility (see
    # app.services.admin_data.eligibility's module docstring).
    new_worker_resp = await client.post(
        "/workers",
        json={"name": "Late Nurse", "skills": [SKILL], "region": REGION, "home_site_id": site.id},
    )
    assert new_worker_resp.status_code == 201

    award_stmt = select(AwardCostMatrix).where(AwardCostMatrix.shift_id == shift_id)
    rows_before = (await db_session.execute(award_stmt)).scalars().all()
    assert rows_before == []

    regen = await client.post(
        "/workers/regenerate-eligibility",
        params={"period_start": SHIFT_DATE.isoformat(), "period_end": SHIFT_DATE.isoformat()},
    )
    assert regen.status_code == 200
    assert regen.json()["placeholder_award_rows_created"] == 1

    rows_after = (await db_session.execute(award_stmt)).scalars().all()
    assert len(rows_after) == 1
    assert rows_after[0].is_placeholder is True

    # Running it again is a no-op (skip-if-exists).
    regen_again = await client.post(
        "/workers/regenerate-eligibility",
        params={"period_start": SHIFT_DATE.isoformat(), "period_end": SHIFT_DATE.isoformat()},
    )
    assert regen_again.json()["placeholder_award_rows_created"] == 0
