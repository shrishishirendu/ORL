"""Unit tests for ``app/services/award_engine/`` -- no DB, no network.

The engine is ``tests/fake_award_engine.py`` behind an ``httpx.MockTransport``
(or a one-off handler), and ORL models are plain transient SQLAlchemy objects
built in memory. Covers: request mapping and JSON shape, deterministic
request building/ordering, response -> row mapping (incl. unresolved),
client error mapping, reconciliation ``cost_status`` paths, the task layer's
"reconciliation never fails a solve" guarantee, and the API endpoints that
need neither Postgres nor Redis.
"""

from __future__ import annotations

import json
from datetime import date, time
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.settings import settings
from app.main import app
from app.models.enums import EmploymentType, RosterCostStatus, RosterStatus
from app.models.roster import Roster, RosterAssignment
from app.models.shift import Shift
from app.models.site import Site
from app.models.worker import Worker
from app.services.award_engine import reconcile as reconcile_module
from app.services.award_engine.client import (
    AwardEngineClient,
    AwardEngineError,
    AwardEngineRejected,
    AwardEngineUnavailable,
    CostMatrixResponse,
    EngineContext,
)
from app.services.award_engine.matrix import (
    award_field_problems,
    build_cost_matrix_requests,
    has_award_fields,
    rows_from_response,
)
from app.services.award_engine.reconcile import ReconcileOutcome, build_price_roster_requests
from app.services.rostering.service import RosterSolveOutcome
from app.workers import tasks as tasks_module
from tests.fake_award_engine import ENGINE_COMMIT, LEVEL_KEYS, FakeAwardEngine

# No module-level ``pytestmark = pytest.mark.asyncio``: this file mixes sync
# and async tests, and ``asyncio_mode = "auto"`` (pyproject) already runs the
# async ones.

CONTEXT = EngineContext(jurisdiction="NSW", legal_employer="Acme Security Pty Ltd", work_type=None)
DAY = date(2026, 10, 5)
SITE = Site(id=1, code="STH-01", name="South", region="South")


def _worker(worker_id: int, **overrides) -> Worker:
    fields = {
        "id": worker_id,
        "name": f"W{worker_id}",
        "skills": ["guard"],
        "home_site_id": SITE.id,
        "active": True,
        "award_code": "MA000016",
        "classification_level": LEVEL_KEYS[0],
        "employment_type": EmploymentType.CASUAL,
    }
    fields.update(overrides)
    return Worker(**fields)


def _shift(shift_id: int, day: date, start: time, end: time) -> Shift:
    shift = Shift(
        id=shift_id,
        date=day,
        start_time=start,
        end_time=end,
        required_skill="guard",
        site_id=SITE.id,
    )
    shift.site = SITE
    return shift


# --- request mapping / determinism -------------------------------------------


def test_request_maps_worker_and_shift_fields_to_the_contract_shape() -> None:
    worker = _worker(
        12,
        employment_type=EmploymentType.PART_TIME,
        over_award_rate=Decimal("31.50"),
        ordinary_hours_per_week=Decimal("30.00"),
        agreed_ordinary_hours_per_shift=Decimal("7.50"),
    )
    shift = _shift(41, date(2026, 10, 10), time(18, 0), time(6, 0))

    [request] = build_cost_matrix_requests([worker], [shift], {}, CONTEXT)
    body = request.model_dump(mode="json", exclude={"pairs"})

    assert body["award_code"] == "MA000016"
    assert body["context"] == {
        "jurisdiction": "NSW",
        "legal_employer": "Acme Security Pty Ltd",
        "work_type": None,
    }
    assert body["workers"] == [
        {
            "worker_id": 12,
            "name": "W12",
            "award_code": "MA000016",
            "classification_level": "MA000016::securityofficerlevel1",
            "employment_type": "part_time",
            # JSON numbers, not strings.
            "over_award_rate": 31.5,
            "ordinary_hours_per_week": 30.0,
            "agreed_ordinary_hours_per_shift": 7.5,
            "roster_cycle_weeks": 1,
            "roster_cycle_start": None,
        }
    ]
    # Overnight shift passes through as HH:MM with end <= start; no
    # break_start / flags keys (ORL has no data for them).
    assert body["shifts"] == [
        {
            "shift_id": 41,
            "date": "2026-10-10",
            "start_time": "18:00",
            "end_time": "06:00",
            "break_minutes": 0,
            "site_code": "STH-01",
        }
    ]
    assert body["baseline"] == {}


async def test_client_omits_pairs_and_sends_json_numbers() -> None:
    engine = FakeAwardEngine()
    [request] = build_cost_matrix_requests(
        [_worker(1)], [_shift(10, DAY, time(8), time(16))], {}, CONTEXT
    )
    async with engine.client() as client:
        await client.cost_matrix(request)
    [sent] = engine.bodies("/engine/cost-matrix")
    assert "pairs" not in sent
    assert "break_start" not in sent["shifts"][0]


def test_request_building_is_deterministic_regardless_of_input_order() -> None:
    workers = [_worker(3), _worker(1), _worker(2, award_code="MA000099")]
    shifts = [
        _shift(21, DAY, time(14), time(22)),
        _shift(20, DAY, time(6), time(14)),
        _shift(19, date(2026, 10, 4), time(22), time(6)),
    ]
    baseline = {3: [shifts[0], shifts[2]]}

    first = build_cost_matrix_requests(workers, shifts, baseline, CONTEXT)
    second = build_cost_matrix_requests(
        list(reversed(workers)),
        list(reversed(shifts)),
        {3: list(reversed(baseline[3]))},
        CONTEXT,
    )

    dump = [json.dumps(r.model_dump(mode="json"), sort_keys=False) for r in first]
    assert dump == [json.dumps(r.model_dump(mode="json"), sort_keys=False) for r in second]
    # One request per award code, in sorted code order; workers by id.
    assert [r.award_code for r in first] == ["MA000016", "MA000099"]
    assert [w.worker_id for w in first[0].workers] == [1, 3]
    # Shifts chronological (date, start, id).
    assert [s.shift_id for s in first[0].shifts] == [19, 20, 21]
    # Baseline only for workers that have one, chronological.
    assert list(first[0].baseline) == ["3"]
    assert [s.shift_id for s in first[0].baseline["3"]] == [19, 21]


def test_workers_without_award_fields_are_not_sent() -> None:
    no_award = _worker(5, award_code=None, classification_level=None, employment_type=None)
    assert not has_award_fields(no_award)
    requests = build_cost_matrix_requests(
        [no_award, _worker(6)], [_shift(1, DAY, time(8), time(16))], {}, CONTEXT
    )
    assert [[w.worker_id for w in r.workers] for r in requests] == [[6]]


def test_requests_are_chunked_by_worker_to_stay_under_the_cell_limit() -> None:
    workers = [_worker(i) for i in range(1, 6)]
    shifts = [_shift(100 + i, DAY, time(8), time(16)) for i in range(4)]
    requests = build_cost_matrix_requests(workers, shifts, {}, CONTEXT, max_cells=8)
    # 8 cells / 4 shifts = 2 workers per request.
    assert [[w.worker_id for w in r.workers] for r in requests] == [[1, 2], [3, 4], [5]]


def test_no_shifts_means_no_requests() -> None:
    assert build_cost_matrix_requests([_worker(1)], [], {}, CONTEXT) == []


# --- pre-flight validation ----------------------------------------------------


def test_award_field_problems_mirrors_the_engine_422_cases() -> None:
    levels = {"MA000016": frozenset(LEVEL_KEYS)}
    assert award_field_problems(_worker(1), levels) == []
    assert award_field_problems(_worker(1, award_code="MA000999"), levels) == [
        "award_code 'MA000999' is not offered by the engine"
    ]
    assert (
        "is not a level key"
        in award_field_problems(_worker(1, classification_level="Level 1"), levels)[0]
    )
    assert award_field_problems(_worker(1, employment_type=EmploymentType.PART_TIME), levels) == [
        "part_time requires ordinary_hours_per_week",
        "part_time requires agreed_ordinary_hours_per_shift",
    ]
    assert award_field_problems(_worker(1, over_award_rate=Decimal("0")), levels) == [
        "over_award_rate must be greater than 0"
    ]


# --- response -> rows -----------------------------------------------------------


def _cost_matrix_response(rows: list[dict]) -> CostMatrixResponse:
    return CostMatrixResponse.model_validate_json(
        json.dumps({"engine_commit": ENGINE_COMMIT, "rows": rows})
    )


def test_rows_from_response_maps_resolved_unresolved_and_engine_ineligible() -> None:
    response = _cost_matrix_response(
        [
            {
                "worker_id": 2,
                "shift_id": 7,
                "day": "2026-10-05",
                "status": "unresolved",
                "eligible": False,
                "pay_cost": None,
                "reasons": ["Legal employer is missing"],
            },
            {
                "worker_id": 1,
                "shift_id": 7,
                "day": "2026-10-05",
                "status": "resolved",
                "eligible": True,
                "pay_cost": 412.37,
                "reasons": [],
                "release_blocking_gaps": ["Roster cycle not configured"],
            },
            {
                "worker_id": 1,
                "shift_id": 8,
                "day": "2026-10-06",
                "status": "resolved",
                "eligible": False,
                "pay_cost": 999.99,
                "reasons": ["work period exceeds the 14 hour maximum"],
            },
        ]
    )

    rows = rows_from_response(response)

    assert [(r.worker_id, r.shift_id) for r in rows] == [(1, 7), (1, 8), (2, 7)]
    resolved, hard_limit, unresolved = rows
    # Money is parsed from the raw JSON text: exact, no float drift.
    assert resolved.pay_cost == Decimal("412.37")
    assert resolved.eligible and resolved.resolved
    assert resolved.release_gaps == ("Roster cycle not configured",)
    assert hard_limit.resolved and not hard_limit.eligible
    assert hard_limit.pay_cost == Decimal("999.99")
    assert not unresolved.resolved
    assert unresolved.eligible is False
    assert unresolved.pay_cost == Decimal("0")  # NOT NULL column; never usable (ineligible)
    assert unresolved.reasons == ("Legal employer is missing",)
    assert unresolved.as_insert_values()["is_placeholder"] is False


# --- client error mapping ---------------------------------------------------------


def _client(handler) -> AwardEngineClient:
    return AwardEngineClient("http://engine.test", transport=httpx.MockTransport(handler))


async def test_client_maps_422_to_rejected_with_the_engine_details() -> None:
    engine = FakeAwardEngine(fail_status=422)
    [request] = build_cost_matrix_requests(
        [_worker(1)], [_shift(1, DAY, time(8), time(16))], {}, CONTEXT
    )
    async with engine.client() as client:
        with pytest.raises(AwardEngineRejected) as exc_info:
            await client.cost_matrix(request)
    assert exc_info.value.details == ["bad input"]


@pytest.mark.parametrize("status", [500, 503])
async def test_client_maps_5xx_to_unavailable(status: int) -> None:
    engine = FakeAwardEngine(fail_status=status)
    [request] = build_cost_matrix_requests(
        [_worker(1)], [_shift(1, DAY, time(8), time(16))], {}, CONTEXT
    )
    async with engine.client() as client:
        with pytest.raises(AwardEngineUnavailable):
            await client.cost_matrix(request)


async def test_client_maps_connection_errors_to_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(refuse) as client:
        with pytest.raises(AwardEngineUnavailable):
            await client.awards()


async def test_client_rejects_a_body_that_does_not_match_the_contract() -> None:
    async with _client(lambda r: httpx.Response(200, json={"nope": True})) as client:
        with pytest.raises(AwardEngineError):
            await client.awards()


async def test_health_returns_a_degraded_503_body_instead_of_raising() -> None:
    degraded = {
        "status": "degraded",
        "pinned_commit": "a",
        "engine_commit": "b",
        "commit_matches": False,
    }
    async with _client(lambda r: httpx.Response(503, json=degraded)) as client:
        status, health = await client.health()
    assert status == 503
    assert health.status == "degraded"
    assert health.commit_matches is False


# --- reconciliation (_price paths, no DB) --------------------------------------


def _solved_roster(*workers: Worker) -> Roster:
    roster = Roster(id=1, period_start=DAY, period_end=DAY, status=RosterStatus.SOLVED)
    for n, worker in enumerate(workers):
        shift = _shift(50 + n, DAY, time(8 + n), time(12 + n))
        assignment = RosterAssignment(worker_id=worker.id, day=DAY, shift_id=shift.id)
        assignment.worker = worker
        assignment.shift = shift
        roster.assignments.append(assignment)
    return roster


async def _price(engine: FakeAwardEngine, roster: Roster) -> ReconcileOutcome:
    outcome = ReconcileOutcome(roster_id=roster.id, cost_status=None)
    async with engine.client() as client:
        await reconcile_module._price(roster, client, CONTEXT, outcome)
    return outcome


async def test_reconcile_exact_stores_the_engine_total_and_commit() -> None:
    engine = FakeAwardEngine(roster_total=8123.45)
    outcome = await _price(engine, _solved_roster(_worker(2), _worker(1)))
    assert outcome.cost_status is RosterCostStatus.ENGINE_EXACT
    assert outcome.engine_total_cost == Decimal("8123.45")
    assert outcome.engine_commit == ENGINE_COMMIT
    [sent] = engine.bodies("/engine/price-roster")
    assert [w["worker_id"] for w in sent["workers"]] == [1, 2]
    assert [a["worker_id"] for a in sent["assignments"]] == [1, 2]
    assert sent["period_start"] == sent["period_end"] == DAY.isoformat()


async def test_reconcile_unresolved_keeps_no_total_and_names_the_workers() -> None:
    engine = FakeAwardEngine(unresolved_workers={2})
    outcome = await _price(engine, _solved_roster(_worker(1), _worker(2)))
    assert outcome.cost_status is RosterCostStatus.ENGINE_UNRESOLVED
    assert outcome.engine_total_cost is None
    assert "worker 2: Legal employer is missing" in outcome.cost_detail


async def test_reconcile_engine_down_sets_unavailable() -> None:
    engine = FakeAwardEngine(fail_status=503)
    outcome = await _price(engine, _solved_roster(_worker(1)))
    assert outcome.cost_status is RosterCostStatus.ENGINE_UNAVAILABLE
    assert outcome.engine_total_cost is None


async def test_reconcile_422_sets_rejected() -> None:
    engine = FakeAwardEngine(fail_status=422)
    outcome = await _price(engine, _solved_roster(_worker(1)))
    assert outcome.cost_status is RosterCostStatus.ENGINE_REJECTED
    assert "bad input" in outcome.cost_detail


async def test_reconcile_preflight_blocks_invalid_worker_data_without_calling_price() -> None:
    engine = FakeAwardEngine()
    outcome = await _price(engine, _solved_roster(_worker(1, classification_level="Level 1")))
    assert outcome.cost_status is RosterCostStatus.ENGINE_REJECTED
    assert outcome.cost_detail.startswith("pre-flight")
    assert "/engine/price-roster" not in engine.paths()


def test_price_roster_requests_group_by_award_code() -> None:
    roster = _solved_roster(_worker(1), _worker(2, award_code="MA000099"))
    requests = build_price_roster_requests(roster, CONTEXT)
    assert [(r.award_code, [w.worker_id for w in r.workers]) for r in requests] == [
        ("MA000016", [1]),
        ("MA000099", [2]),
    ]


# --- task layer: reconciliation never fails a solve --------------------------------


async def test_reconcile_failure_never_fails_the_solve_task(monkeypatch) -> None:
    async def explode(*args, **kwargs):
        raise RuntimeError("db went away")

    monkeypatch.setattr(tasks_module, "reconcile_roster_cost", explode)
    monkeypatch.setattr(tasks_module, "client_from_settings", lambda: None)
    result = await tasks_module._reconcile_roster(
        RosterSolveOutcome(roster_id=7, status=RosterStatus.SOLVED, total_cost=10.0)
    )
    assert result["cost_status"] is None
    assert "reconciliation failed" in result["cost_detail"]


async def test_failed_rosters_are_not_reconciled(monkeypatch) -> None:
    async def must_not_run(*args, **kwargs):
        raise AssertionError("reconcile called for a failed roster")

    monkeypatch.setattr(tasks_module, "reconcile_roster_cost", must_not_run)
    result = await tasks_module._reconcile_roster(
        RosterSolveOutcome(roster_id=7, status=RosterStatus.FAILED)
    )
    assert result == {"engine_total_cost": None, "engine_commit": None, "cost_status": None}


async def test_sync_task_reports_disabled_when_engine_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(tasks_module, "client_from_settings", lambda: None)
    result = await tasks_module.sync_award_matrix_task({}, "2026-10-05", "2026-10-11")
    assert result["status"] == "disabled"


# --- API (no Postgres/Redis needed) ----------------------------------------------


@pytest.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_health_reports_disabled_when_engine_not_configured(api, monkeypatch) -> None:
    monkeypatch.setattr(settings, "award_engine_url", None)
    response = await api.get("/award-engine/health")
    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "status": "disabled",
        "engine": None,
        "detail": None,
    }


async def test_health_reports_unreachable_as_503(api, monkeypatch) -> None:
    monkeypatch.setattr(settings, "award_engine_url", "http://127.0.0.1:9")
    monkeypatch.setattr(settings, "award_engine_timeout_s", 2.0)
    response = await api.get("/award-engine/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unreachable"


async def test_sync_matrix_is_503_when_engine_not_configured(api, monkeypatch) -> None:
    from app.core.queue import get_arq_redis

    async def no_redis():
        yield None

    monkeypatch.setattr(settings, "award_engine_url", None)
    app.dependency_overrides[get_arq_redis] = no_redis
    try:
        response = await api.post(
            "/award-engine/sync-matrix",
            json={"period_start": "2026-10-05", "period_end": "2026-10-11"},
        )
    finally:
        app.dependency_overrides.pop(get_arq_redis, None)
    assert response.status_code == 503


async def test_sync_matrix_rejects_a_reversed_period(api) -> None:
    response = await api.post(
        "/award-engine/sync-matrix",
        json={"period_start": "2026-10-11", "period_end": "2026-10-05"},
    )
    assert response.status_code == 422
