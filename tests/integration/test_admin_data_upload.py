"""Integration tests for `POST /admin/data/upload` -- the bulk
Workers/Shifts full-replace endpoint. Covers the full-replace semantics
(upsert-by-employee_code/name, soft-deactivation, the "don't touch
already-rostered shifts" and "don't delete shifts with real AwardCostMatrix
rows" safety rules), dry-run not persisting, and validation-error rollback.
"""

from __future__ import annotations

import csv
import io
from datetime import date, time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models.award_cost_matrix import AwardCostMatrix
from app.models.shift import Shift
from app.models.worker import Worker
from tests.integration.factories import (
    make_award_row,
    make_roster,
    make_roster_assignment,
    make_shift,
    make_site,
    make_worker,
)

pytestmark = pytest.mark.asyncio

PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 7)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _csv_bytes(header: list[str], rows: list[tuple[str, ...]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _workers_csv(rows: list[tuple[str, str, str, str, str, str]]) -> bytes:
    header = ["name", "skills", "region", "home_site_code", "active", "employee_code"]
    return _csv_bytes(header, rows)


def _shifts_csv(rows: list[tuple[str, str, str, str, str, str]]) -> bytes:
    return _csv_bytes(
        ["date", "start_time", "end_time", "required_skill", "site_code", "is_multi_stop"], rows
    )


async def test_upload_requires_at_least_one_source(client) -> None:
    response = await client.post("/admin/data/upload", data={})
    assert response.status_code == 422


async def test_upload_workers_csv_creates_updates_and_deactivates(client, db_session) -> None:
    site = await make_site(db_session, "U-SITE-1", region="north")
    stays_active = await make_worker(
        db_session, site, name="Stays Active", skills=["nursing"], active=True
    )
    stays_active.employee_code = "EMP-STAY"
    gets_deactivated = await make_worker(
        db_session, site, name="Gets Deactivated", skills=["cleaning"], active=True
    )
    await db_session.commit()

    csv_bytes = _workers_csv(
        [
            ("Stays Active", "nursing, first_aid", "north", "U-SITE-1", "true", "EMP-STAY"),
            ("Brand New Worker", "driving", "north", "U-SITE-1", "true", "EMP-NEW"),
        ]
    )
    response = await client.post(
        "/admin/data/upload",
        files={"workers_csv": ("workers.csv", csv_bytes, "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is False
    workers_summary = body["workers"]
    assert workers_summary["created"] == 1
    assert workers_summary["updated"] == 1
    assert workers_summary["matched_by_employee_code"] == 1
    # >=1, not ==1: this upload only names 2 rows, so *every* other
    # currently-active worker gets deactivated too (full-replace semantics
    # -- see app.services.admin_data.workers.upsert_workers_from_rows). In
    # a DB that also has scripts/seed_demo_data.py's workers loaded, those
    # get swept up as well; this test only asserts on the two rows it
    # actually cares about, not the exact global count.
    assert workers_summary["deactivated"] >= 1
    assert {"id": gets_deactivated.id, "name": "Gets Deactivated"} in workers_summary[
        "deactivated_workers"
    ]

    await db_session.refresh(gets_deactivated)
    await db_session.refresh(stays_active)
    assert gets_deactivated.active is False
    assert stays_active.active is True
    assert stays_active.skills == ["nursing", "first_aid"]

    new_worker = (
        await db_session.execute(select(Worker).where(Worker.employee_code == "EMP-NEW"))
    ).scalar_one()
    assert new_worker.name == "Brand New Worker"


async def test_upload_workers_matches_by_name_when_no_employee_code(client, db_session) -> None:
    site = await make_site(db_session, "U-SITE-2")
    worker = await make_worker(db_session, site, name="Name Match Worker", skills=["nursing"])
    await db_session.commit()

    # Same name, different case -- matching is case-insensitive. The
    # upload is a *full replace* of matched fields though, so the row's own
    # casing becomes the persisted name (this is not a "preserve existing
    # name" merge).
    csv_bytes = _workers_csv([("name match worker", "nursing,driving", "", "U-SITE-2", "true", "")])
    response = await client.post(
        "/admin/data/upload", files={"workers_csv": ("workers.csv", csv_bytes, "text/csv")}
    )
    assert response.status_code == 200
    workers_summary = response.json()["workers"]
    assert workers_summary["updated"] == 1
    assert workers_summary["matched_by_name"] == 1
    assert "name match worker" in workers_summary["matched_by_name_workers"]

    await db_session.refresh(worker)
    assert worker.name == "name match worker"
    assert worker.skills == ["nursing", "driving"]


async def test_upload_shifts_replaces_unrostered_shifts_in_period(client, db_session) -> None:
    site = await make_site(db_session, "U-SITE-3")
    old_shift = await make_shift(
        db_session,
        site,
        date=PERIOD_START,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="cleaning",
    )
    await db_session.commit()

    csv_bytes = _shifts_csv(
        [(PERIOD_START.isoformat(), "09:00", "17:00", "nursing", "U-SITE-3", "false")]
    )
    response = await client.post(
        "/admin/data/upload",
        files={"shifts_csv": ("shifts.csv", csv_bytes, "text/csv")},
        data={"period_start": PERIOD_START.isoformat(), "period_end": PERIOD_END.isoformat()},
    )
    assert response.status_code == 200
    shifts_summary = response.json()["shifts"]
    assert shifts_summary["deleted"] == 1
    assert shifts_summary["created"] == 1
    assert shifts_summary["shifts_skipped"] == []

    remaining = (await db_session.execute(select(Shift))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].required_skill == "nursing"
    assert remaining[0].id != old_shift.id


async def test_upload_shifts_skips_already_rostered_shift(client, db_session) -> None:
    site = await make_site(db_session, "U-SITE-4")
    rostered_shift = await make_shift(
        db_session,
        site,
        date=PERIOD_START,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="cleaning",
    )
    worker = await make_worker(db_session, site, name="Rostered Worker", skills=["cleaning"])
    roster = await make_roster(db_session, period_start=PERIOD_START, period_end=PERIOD_END)
    await make_roster_assignment(db_session, roster, worker, rostered_shift)
    await db_session.commit()

    csv_bytes = _shifts_csv(
        [(PERIOD_START.isoformat(), "09:00", "17:00", "nursing", "U-SITE-4", "false")]
    )
    response = await client.post(
        "/admin/data/upload",
        files={"shifts_csv": ("shifts.csv", csv_bytes, "text/csv")},
        data={"period_start": PERIOD_START.isoformat(), "period_end": PERIOD_END.isoformat()},
    )
    assert response.status_code == 200
    shifts_summary = response.json()["shifts"]
    assert shifts_summary["deleted"] == 0
    assert shifts_summary["created"] == 1
    assert len(shifts_summary["shifts_skipped"]) == 1
    assert shifts_summary["shifts_skipped"][0]["reason"] == "already_rostered"
    assert shifts_summary["shifts_skipped"][0]["id"] == rostered_shift.id

    remaining_ids = {s.id for s in (await db_session.execute(select(Shift))).scalars().all()}
    assert rostered_shift.id in remaining_ids  # left untouched
    assert len(remaining_ids) == 2  # untouched old one + newly created one


async def test_upload_shifts_skips_shift_with_real_award_data(client, db_session) -> None:
    site = await make_site(db_session, "U-SITE-5")
    shift_with_real_data = await make_shift(
        db_session,
        site,
        date=PERIOD_START,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="cleaning",
    )
    worker = await make_worker(db_session, site, name="Real Data Worker", skills=["cleaning"])
    # is_placeholder=False by default -- a real award row.
    await make_award_row(db_session, worker, shift_with_real_data, pay_cost=99.0)
    await db_session.commit()

    csv_bytes = _shifts_csv(
        [(PERIOD_START.isoformat(), "09:00", "17:00", "nursing", "U-SITE-5", "false")]
    )
    response = await client.post(
        "/admin/data/upload",
        files={"shifts_csv": ("shifts.csv", csv_bytes, "text/csv")},
        data={"period_start": PERIOD_START.isoformat(), "period_end": PERIOD_END.isoformat()},
    )
    assert response.status_code == 200
    shifts_summary = response.json()["shifts"]
    assert shifts_summary["deleted"] == 0
    assert len(shifts_summary["shifts_skipped"]) == 1
    assert shifts_summary["shifts_skipped"][0]["reason"] == "has_real_award_data"

    remaining_ids = {s.id for s in (await db_session.execute(select(Shift))).scalars().all()}
    assert shift_with_real_data.id in remaining_ids

    real_row = (
        await db_session.execute(
            select(AwardCostMatrix).where(AwardCostMatrix.shift_id == shift_with_real_data.id)
        )
    ).scalar_one()
    assert real_row.is_placeholder is False  # untouched, not orphaned/deleted


async def test_upload_rejects_multi_stop_shift_row(client, db_session) -> None:
    await make_site(db_session, "U-SITE-6")
    await db_session.commit()

    csv_bytes = _shifts_csv(
        [(PERIOD_START.isoformat(), "09:00", "17:00", "nursing", "U-SITE-6", "true")]
    )
    response = await client.post(
        "/admin/data/upload", files={"shifts_csv": ("shifts.csv", csv_bytes, "text/csv")}
    )
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any("is_multi_stop" in e for e in errors)

    assert (await db_session.execute(select(Shift))).scalars().all() == []


async def test_upload_rejects_unknown_site_code_and_rolls_back_everything(
    client, db_session
) -> None:
    await make_site(db_session, "U-SITE-7")
    await db_session.commit()

    workers_bytes = _workers_csv([("Valid Worker", "nursing", "", "U-SITE-7", "true", "")])
    shifts_bytes = _shifts_csv(
        [(PERIOD_START.isoformat(), "09:00", "17:00", "nursing", "NO-SUCH-SITE", "false")]
    )
    response = await client.post(
        "/admin/data/upload",
        files={
            "workers_csv": ("workers.csv", workers_bytes, "text/csv"),
            "shifts_csv": ("shifts.csv", shifts_bytes, "text/csv"),
        },
    )
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any("NO-SUCH-SITE" in e for e in errors)

    # Nothing committed -- not even the otherwise-valid Workers row.
    workers = (await db_session.execute(select(Worker))).scalars().all()
    assert all(w.name != "Valid Worker" for w in workers)


async def test_upload_dry_run_does_not_persist(client, db_session) -> None:
    await make_site(db_session, "U-SITE-8")
    await db_session.commit()

    csv_bytes = _workers_csv([("Dry Run Worker", "nursing", "", "U-SITE-8", "true", "DRY-1")])
    response = await client.post(
        "/admin/data/upload",
        files={"workers_csv": ("workers.csv", csv_bytes, "text/csv")},
        data={"dry_run": "true"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["workers"]["created"] == 1

    persisted = (
        await db_session.execute(select(Worker).where(Worker.employee_code == "DRY-1"))
    ).scalar_one_or_none()
    assert persisted is None


async def test_get_template_returns_an_xlsx(client) -> None:
    response = await client.get("/admin/data/template")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert len(response.content) > 0
