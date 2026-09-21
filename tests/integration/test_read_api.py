"""Integration tests for the ops-dashboard read API (Part 1): the new
`GET /workers`, `GET /shifts`, `GET /rosters`(+`/{id}`),
`GET /routes/{roster_assignment_id}`, and `GET /events` endpoints.

These hit a real Postgres (via `db_session`/the other fixtures in
``tests/integration/conftest.py``) through the actual FastAPI app over
``httpx.AsyncClient`` -- no Redis needed, since none of these are
job-enqueuing endpoints (unlike ``test_api_routers.py``'s coverage of the
``POST``/``.../jobs/{job_id}`` endpoints).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.main import app
from app.models.enums import ReoptimizationEventType, ReoptimizationResolution, ReoptimizationStatus
from app.models.route import Route
from app.services.dispatch.service import solve_and_persist_route
from tests.integration.factories import (
    make_job,
    make_reoptimization_event,
    make_roster,
    make_roster_assignment,
    make_shift,
    make_site,
    make_travel_entry,
    make_worker,
)

pytestmark = pytest.mark.asyncio

SHIFT_DATE = date(2026, 5, 4)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------
# GET /workers
# --------------------------------------------------------------------------


async def test_list_workers_returns_seeded_workers(db_session, client) -> None:
    home = await make_site(db_session, "W-HOME", name="Home Base", region="north")
    await make_worker(db_session, home, name="Alice", skills=["nursing"], region="north")
    await make_worker(db_session, home, name="Bob", skills=["cleaning"], region="north")
    await db_session.commit()

    response = await client.get("/workers")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    names = {w["name"] for w in body["items"]}
    assert names == {"Alice", "Bob"}
    alice = next(w for w in body["items"] if w["name"] == "Alice")
    assert alice["skills"] == ["nursing"]
    assert alice["region"] == "north"
    assert alice["home_site"]["code"] == "W-HOME"
    assert alice["active"] is True


async def test_list_workers_respects_limit_and_offset(db_session, client) -> None:
    home = await make_site(db_session, "W-HOME2")
    for i in range(3):
        await make_worker(db_session, home, name=f"Worker {i}")
    await db_session.commit()

    response = await client.get("/workers", params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["limit"] == 2
    assert body["offset"] == 1


# --------------------------------------------------------------------------
# GET /shifts
# --------------------------------------------------------------------------


async def test_list_shifts_filters_by_date_range(db_session, client) -> None:
    site = await make_site(db_session, "S-SITE", region="north")
    await make_shift(
        db_session,
        site,
        date=date(2026, 5, 1),
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="nursing",
    )
    in_range = await make_shift(
        db_session,
        site,
        date=date(2026, 5, 5),
        start_time=time(9, 0),
        end_time=time(17, 0),
        required_skill="cleaning",
        is_multi_stop=True,
    )
    await make_shift(
        db_session,
        site,
        date=date(2026, 5, 10),
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="nursing",
    )
    await db_session.commit()

    response = await client.get(
        "/shifts", params={"date_from": "2026-05-03", "date_to": "2026-05-07"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    only = body["items"][0]
    assert only["id"] == in_range.id
    assert only["required_skill"] == "cleaning"
    assert only["is_multi_stop"] is True
    assert only["site"]["code"] == "S-SITE"


async def test_list_shifts_with_no_filter_returns_everything(db_session, client) -> None:
    site = await make_site(db_session, "S-SITE2", region="north")
    await make_shift(
        db_session,
        site,
        date=date(2026, 6, 1),
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="nursing",
    )
    await db_session.commit()

    response = await client.get("/shifts")
    assert response.status_code == 200
    assert response.json()["total"] == 1


# --------------------------------------------------------------------------
# GET /rosters, GET /rosters/{id}
# --------------------------------------------------------------------------


async def test_list_rosters_returns_summaries(db_session, client) -> None:
    await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    await db_session.commit()

    response = await client.get("/rosters")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    roster = body["items"][0]
    assert roster["period_start"] == str(SHIFT_DATE)
    assert roster["status"] == "solved"


async def test_get_roster_detail_includes_assignments(db_session, client) -> None:
    site = await make_site(db_session, "R-SITE", region="north")
    worker = await make_worker(db_session, site, name="Rory", region="north")
    shift = await make_shift(
        db_session,
        site,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="nursing",
    )
    roster = await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    assignment = await make_roster_assignment(db_session, roster, worker, shift)
    await db_session.commit()

    response = await client.get(f"/rosters/{roster.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == roster.id
    assert len(body["assignments"]) == 1
    row = body["assignments"][0]
    assert row["id"] == assignment.id
    assert row["worker"]["name"] == "Rory"
    assert row["shift"]["id"] == shift.id
    assert row["shift"]["site"]["code"] == "R-SITE"
    assert row["day"] == str(SHIFT_DATE)


async def test_get_roster_detail_404_for_unknown_id(client) -> None:
    response = await client.get("/rosters/999999")
    assert response.status_code == 404


# --------------------------------------------------------------------------
# GET /routes/{roster_assignment_id}
# --------------------------------------------------------------------------


async def test_get_route_for_single_site_assignment_returns_site_assignment_mode(
    db_session, client
) -> None:
    home = await make_site(db_session, "RT-HOME", region="north")
    worker = await make_worker(db_session, home, skills=["driver"])
    shift = await make_shift(
        db_session,
        home,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="driver",
        is_multi_stop=False,
    )
    roster = await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    assignment = await make_roster_assignment(db_session, roster, worker, shift)
    await db_session.commit()

    await solve_and_persist_route(db_session, assignment.id)

    response = await client.get(f"/routes/{assignment.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "site_assignment"
    assert body["site"]["code"] == "RT-HOME"
    assert body["arrival"] == "2026-05-04T08:00:00Z"
    assert body["departure"] == "2026-05-04T16:00:00Z"


async def test_get_route_for_multi_stop_assignment_returns_routed_mode_with_stops(
    db_session, client
) -> None:
    home = await make_site(db_session, "RT-HOME2", region="north")
    site_a = await make_site(db_session, "RT-A")
    site_b = await make_site(db_session, "RT-B")
    worker = await make_worker(db_session, home, skills=["driver"])
    shift = await make_shift(
        db_session,
        home,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(18, 0),
        required_skill="driver",
        is_multi_stop=True,
    )
    job1 = await make_job(
        db_session,
        shift,
        site_a,
        window_start=datetime(2026, 5, 4, 9, 0),
        window_end=datetime(2026, 5, 4, 10, 0),
        duration_minutes=15,
    )
    job2 = await make_job(
        db_session,
        shift,
        site_b,
        window_start=datetime(2026, 5, 4, 10, 30),
        window_end=datetime(2026, 5, 4, 11, 30),
        duration_minutes=20,
    )
    await make_travel_entry(db_session, home, site_a, 10)
    await make_travel_entry(db_session, home, site_b, 20)
    await make_travel_entry(db_session, site_a, site_b, 15)
    roster = await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    assignment = await make_roster_assignment(db_session, roster, worker, shift)
    await db_session.commit()

    outcome = await solve_and_persist_route(db_session, assignment.id)
    assert outcome.mode == "routed"

    response = await client.get(f"/routes/{assignment.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "routed"
    assert body["status"] == "solved"
    assert {s["job_id"] for s in body["stops"]} == {job1.id, job2.id}
    assert [s["sequence_no"] for s in body["stops"]] == sorted(
        s["sequence_no"] for s in body["stops"]
    )
    # Return-to-home time is recomputed from the last stop's planned
    # departure + the cached travel time back home (see app/api/routes.py) --
    # not read from a persisted column, since Route has none.
    assert body["return_to_home_time"] is not None
    route = (
        await db_session.execute(
            select(Route)
            .options(selectinload(Route.stops))
            .where(Route.roster_assignment_id == assignment.id)
        )
    ).scalar_one()
    last_stop = sorted(route.stops, key=lambda s: s.sequence_no)[-1]
    last_site_travel_minutes = 15 if last_stop.job_id == job1.id else 20
    expected = last_stop.planned_departure + timedelta(minutes=last_site_travel_minutes)
    assert body["return_to_home_time"] == expected.isoformat().replace("+00:00", "Z")


async def test_get_route_404_for_unknown_roster_assignment(client) -> None:
    response = await client.get("/routes/999999")
    assert response.status_code == 404


async def test_get_route_404_when_not_yet_dispatched(db_session, client) -> None:
    home = await make_site(db_session, "RT-HOME3", region="north")
    worker = await make_worker(db_session, home, skills=["driver"])
    shift = await make_shift(
        db_session,
        home,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="driver",
    )
    roster = await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    assignment = await make_roster_assignment(db_session, roster, worker, shift)
    await db_session.commit()

    response = await client.get(f"/routes/{assignment.id}")
    assert response.status_code == 404


# --------------------------------------------------------------------------
# GET /events
# --------------------------------------------------------------------------


async def test_list_events_filters_by_shift_id_and_resolved(db_session, client) -> None:
    site = await make_site(db_session, "EV-SITE", region="north")
    worker1 = await make_worker(db_session, site, name="Ev Worker 1", region="north")
    worker2 = await make_worker(db_session, site, name="Ev Worker 2", region="north")
    shift1 = await make_shift(
        db_session,
        site,
        date=SHIFT_DATE,
        start_time=time(8, 0),
        end_time=time(16, 0),
        required_skill="nursing",
    )
    shift2 = await make_shift(
        db_session,
        site,
        date=SHIFT_DATE,
        start_time=time(9, 0),
        end_time=time(17, 0),
        required_skill="cleaning",
    )
    await make_reoptimization_event(
        db_session,
        event_type=ReoptimizationEventType.JOB_CANCELLED,
        occurred_at=datetime(2026, 5, 4, 10, 0, tzinfo=UTC),
        shift_id=shift1.id,
        worker_id=worker1.id,
        resolution=ReoptimizationResolution.SCOPED_RESOLVE,
        resolved_at=datetime(2026, 5, 4, 10, 1, tzinfo=UTC),
        status=ReoptimizationStatus.RESOLVED,
    )
    await make_reoptimization_event(
        db_session,
        event_type=ReoptimizationEventType.WORKER_SICK,
        occurred_at=datetime(2026, 5, 4, 11, 0, tzinfo=UTC),
        shift_id=shift2.id,
        worker_id=worker2.id,
        status=ReoptimizationStatus.OPEN,
    )
    await db_session.commit()

    all_response = await client.get("/events")
    assert all_response.status_code == 200
    assert all_response.json()["total"] == 2

    by_shift = await client.get("/events", params={"shift_id": shift1.id})
    assert by_shift.status_code == 200
    body = by_shift.json()
    assert body["total"] == 1
    assert body["items"][0]["shift_id"] == shift1.id
    assert body["items"][0]["resolution"] == "scoped_resolve"

    resolved_only = await client.get("/events", params={"resolved": True})
    assert resolved_only.json()["total"] == 1
    assert resolved_only.json()["items"][0]["status"] == "resolved"

    unresolved_only = await client.get("/events", params={"resolved": False})
    assert unresolved_only.json()["total"] == 1
    assert unresolved_only.json()["items"][0]["status"] == "open"
