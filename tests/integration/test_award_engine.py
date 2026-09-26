"""Integration tests for the award-engine integration against a real
Postgres, with the engine itself faked (``tests/fake_award_engine.py``,
an ``httpx.MockTransport`` -- no Node process needed).

Covers: ``populate_award_cost_matrix`` upserting engine rows over
placeholders while leaving non-award workers' placeholders alone, the
baseline being taken from the latest solved roster, engine-down leaving the
table untouched, pre-flight exclusion of invalid worker data; then the
Tier 1 path: solve -> reconcile (``engine_exact`` / ``placeholder_estimate``
/ ``engine_unavailable``), the task wrapper never failing a solve on an
engine problem, and the new Roster fields on ``GET /rosters/{id}``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models.award_cost_matrix import AwardCostMatrix
from app.models.enums import EmploymentType, RosterCostStatus, RosterStatus
from app.models.roster import Roster
from app.services.award_engine.client import AwardEngineUnavailable, EngineContext
from app.services.award_engine.matrix import populate_award_cost_matrix
from app.services.award_engine.reconcile import reconcile_roster_cost
from app.services.rostering.service import solve_and_persist_roster
from app.workers import tasks as tasks_module
from tests.fake_award_engine import ENGINE_COMMIT, LEVEL_KEYS, FakeAwardEngine, fake_cell_cost
from tests.integration.factories import (
    make_award_row,
    make_roster,
    make_roster_assignment,
    make_shift,
    make_site,
    make_worker,
)

pytestmark = pytest.mark.asyncio

DAY1 = date(2026, 10, 5)
DAY2 = date(2026, 10, 6)
CONTEXT = EngineContext(jurisdiction="NSW", legal_employer="Acme Security Pty Ltd")
AWARD = {
    "award_code": "MA000016",
    "classification_level": LEVEL_KEYS[0],
    "employment_type": EmploymentType.CASUAL,
}


async def _scenario(session, *, award_for_c: bool = False):
    """Two award workers (A, B), one without award fields (C), two shifts,
    and pre-existing placeholder rows for A and C on shift 1.
    """
    site = await make_site(session, "STH-01", region="South")
    a = await make_worker(session, site, name="A", skills=["guard"], **AWARD)
    b = await make_worker(session, site, name="B", skills=["guard"], **AWARD)
    c = await make_worker(
        session, site, name="C", skills=["guard"], **(AWARD if award_for_c else {})
    )
    s1 = await make_shift(
        session, site, date=DAY1, start_time=time(8), end_time=time(16), required_skill="guard"
    )
    s2 = await make_shift(
        session, site, date=DAY2, start_time=time(8), end_time=time(16), required_skill="guard"
    )
    await make_award_row(session, a, s1, pay_cost=320.0, is_placeholder=True)
    await make_award_row(session, c, s1, pay_cost=320.0, is_placeholder=True)
    await session.commit()
    return site, (a, b, c), (s1, s2)


async def _rows(session) -> dict[tuple[int, int], AwardCostMatrix]:
    # populate_existing: refresh rows already in the identity map (the
    # upsert bypasses the ORM) without expiring the test's other objects.
    stmt = select(AwardCostMatrix).execution_options(populate_existing=True)
    rows = (await session.execute(stmt)).scalars().all()
    return {(r.worker_id, r.shift_id): r for r in rows}


# --- matrix population -----------------------------------------------------------


async def test_populate_writes_engine_rows_and_keeps_non_award_placeholders(db_session) -> None:
    _, (a, b, c), (s1, s2) = await _scenario(db_session)
    engine = FakeAwardEngine(unresolved_cells={(b.id, s2.id)}, release_gap_cells={(a.id, s2.id)})

    async with engine.client() as client:
        summary = await populate_award_cost_matrix(db_session, DAY1, DAY2, client, context=CONTEXT)
    await db_session.commit()

    rows = await _rows(db_session)
    # A's placeholder on s1 was replaced by an engine row.
    assert rows[(a.id, s1.id)].is_placeholder is False
    assert rows[(a.id, s1.id)].pay_cost == Decimal(str(fake_cell_cost(a.id, s1.id)))
    for key in [(a.id, s2.id), (b.id, s1.id)]:
        assert rows[key].is_placeholder is False
        assert rows[key].eligible is True
    # Unresolved -> ineligible, pay_cost 0.
    assert rows[(b.id, s2.id)].eligible is False
    assert rows[(b.id, s2.id)].pay_cost == Decimal("0")
    # C has no award fields: its placeholder row is untouched, and no row added.
    assert rows[(c.id, s1.id)].is_placeholder is True
    assert rows[(c.id, s1.id)].pay_cost == Decimal("320.00")
    assert (c.id, s2.id) not in rows

    assert summary.rows_written == 4
    assert summary.resolved_count == 3
    assert summary.unresolved_count == 1
    assert summary.unresolved_samples == [
        {"worker_id": b.id, "shift_id": s2.id, "reasons": ["Legal employer is missing"]}
    ]
    assert summary.workers_priced == [a.id, b.id]
    assert summary.workers_without_award_fields == [c.id]
    assert summary.placeholder_rows_remaining == 1
    assert summary.cost_basis == "mixed"
    assert summary.engine_commit == ENGINE_COMMIT
    assert summary.release_gap_cell_count == 1
    assert summary.release_gaps == ["Roster cycle not configured"]
    assert summary.baseline_roster_id is None
    # Only award workers were sent, with the configured context.
    [sent] = engine.bodies("/engine/cost-matrix")
    assert [w["worker_id"] for w in sent["workers"]] == [a.id, b.id]
    assert sent["context"]["legal_employer"] == "Acme Security Pty Ltd"
    assert sent["baseline"] == {}


async def test_populate_is_all_engine_when_every_worker_has_award_fields(db_session) -> None:
    await _scenario(db_session, award_for_c=True)
    async with FakeAwardEngine().client() as client:
        summary = await populate_award_cost_matrix(db_session, DAY1, DAY2, client, context=CONTEXT)
    assert summary.cost_basis == "engine"
    assert summary.placeholder_rows_remaining == 0


async def test_populate_baseline_comes_from_the_latest_solved_roster(db_session) -> None:
    _, (a, b, _c), (s1, s2) = await _scenario(db_session)
    older = await make_roster(db_session, period_start=DAY1, period_end=DAY2)
    older.generated_at = datetime(2026, 9, 1)
    await make_roster_assignment(db_session, older, b, s2)
    latest = await make_roster(db_session, period_start=DAY1, period_end=DAY2)
    latest.generated_at = datetime(2026, 9, 2)
    await make_roster_assignment(db_session, latest, a, s1)
    failed = await make_roster(
        db_session, period_start=DAY1, period_end=DAY2, status=RosterStatus.FAILED
    )
    failed.generated_at = datetime(2026, 9, 3)
    await db_session.commit()

    engine = FakeAwardEngine()
    async with engine.client() as client:
        summary = await populate_award_cost_matrix(db_session, DAY1, DAY2, client, context=CONTEXT)

    assert summary.baseline_roster_id == latest.id
    [sent] = engine.bodies("/engine/cost-matrix")
    assert list(sent["baseline"]) == [str(a.id)]
    assert [s["shift_id"] for s in sent["baseline"][str(a.id)]] == [s1.id]


async def test_populate_engine_down_writes_nothing(db_session) -> None:
    _, (a, _b, c), (s1, _s2) = await _scenario(db_session)
    expected_keys = {(a.id, s1.id), (c.id, s1.id)}  # read ids before rollback expires them
    async with FakeAwardEngine(fail_status=503).client() as client:
        with pytest.raises(AwardEngineUnavailable):
            await populate_award_cost_matrix(db_session, DAY1, DAY2, client, context=CONTEXT)
    await db_session.rollback()

    rows = await _rows(db_session)
    assert set(rows) == expected_keys
    assert all(r.is_placeholder for r in rows.values())


async def test_populate_leaves_out_workers_that_would_fail_engine_validation(db_session) -> None:
    _, (a, b, _c), _ = await _scenario(db_session)
    b.classification_level = "Level 1"  # not an engine level key
    await db_session.commit()

    engine = FakeAwardEngine()
    async with engine.client() as client:
        summary = await populate_award_cost_matrix(db_session, DAY1, DAY2, client, context=CONTEXT)

    assert summary.workers_priced == [a.id]
    assert [w["worker_id"] for w in summary.workers_invalid_award_fields] == [b.id]
    [sent] = engine.bodies("/engine/cost-matrix")
    assert [w["worker_id"] for w in sent["workers"]] == [a.id]


# --- Tier 1 solve + reconciliation ------------------------------------------------


async def test_solve_then_reconcile_stores_exact_engine_cost_and_api_exposes_it(
    db_session,
) -> None:
    await _scenario(db_session, award_for_c=True)
    engine = FakeAwardEngine()
    async with engine.client() as client:
        await populate_award_cost_matrix(db_session, DAY1, DAY2, client, context=CONTEXT)
        await db_session.commit()
        outcome = await solve_and_persist_roster(db_session, DAY1, DAY2)
        assert outcome.status is RosterStatus.SOLVED
        reconciled = await reconcile_roster_cost(
            db_session, outcome.roster_id, client, context=CONTEXT
        )

    assert reconciled.cost_status is RosterCostStatus.ENGINE_EXACT
    [sent] = engine.bodies("/engine/price-roster")
    expected = sum(
        Decimal(str(fake_cell_cost(a["worker_id"], s["shift_id"])))
        for a in sent["assignments"]
        for s in a["shifts"]
    )
    assert reconciled.engine_total_cost == expected

    roster = (
        await db_session.execute(
            select(Roster)
            .where(Roster.id == outcome.roster_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert roster.cost_status is RosterCostStatus.ENGINE_EXACT
    assert roster.engine_total_cost == expected
    assert roster.engine_commit == ENGINE_COMMIT
    # The solver estimate is kept as-is alongside.
    assert float(roster.total_cost) == pytest.approx(outcome.total_cost)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        body = (await api.get(f"/rosters/{roster.id}")).json()
        listed = (await api.get("/rosters")).json()["items"][0]
    assert body["cost_status"] == "engine_exact"
    assert body["engine_total_cost"] == pytest.approx(float(expected))
    assert body["engine_commit"] == ENGINE_COMMIT
    assert listed["cost_status"] == "engine_exact"


async def test_reconcile_flags_placeholder_estimate_when_engine_disabled(db_session) -> None:
    site = await make_site(db_session, "S1")
    worker = await make_worker(db_session, site, skills=["guard"])
    shift = await make_shift(
        db_session, site, date=DAY1, start_time=time(8), end_time=time(16), required_skill="guard"
    )
    await make_award_row(db_session, worker, shift, pay_cost=320.0, is_placeholder=True)
    await db_session.commit()
    outcome = await solve_and_persist_roster(db_session, DAY1, DAY1)

    reconciled = await reconcile_roster_cost(db_session, outcome.roster_id, None)

    assert reconciled.cost_status is RosterCostStatus.PLACEHOLDER_ESTIMATE
    assert reconciled.engine_total_cost is None


async def test_reconcile_leaves_status_null_when_engine_disabled_and_rows_are_real(
    db_session,
) -> None:
    site = await make_site(db_session, "S1")
    worker = await make_worker(db_session, site, skills=["guard"])
    shift = await make_shift(
        db_session, site, date=DAY1, start_time=time(8), end_time=time(16), required_skill="guard"
    )
    await make_award_row(db_session, worker, shift, pay_cost=320.0)
    await db_session.commit()
    outcome = await solve_and_persist_roster(db_session, DAY1, DAY1)

    reconciled = await reconcile_roster_cost(db_session, outcome.roster_id, None)

    assert reconciled.cost_status is None


async def test_reconcile_never_prices_a_partial_roster(db_session) -> None:
    """A worker without award fields is assigned: no engine call, flagged."""
    site = await make_site(db_session, "S1")
    worker = await make_worker(db_session, site, skills=["guard"])  # no award fields
    shift = await make_shift(
        db_session, site, date=DAY1, start_time=time(8), end_time=time(16), required_skill="guard"
    )
    await make_award_row(db_session, worker, shift, pay_cost=320.0, is_placeholder=True)
    await db_session.commit()
    outcome = await solve_and_persist_roster(db_session, DAY1, DAY1)

    engine = FakeAwardEngine()
    async with engine.client() as client:
        reconciled = await reconcile_roster_cost(
            db_session, outcome.roster_id, client, context=CONTEXT
        )

    assert reconciled.cost_status is RosterCostStatus.PLACEHOLDER_ESTIMATE
    assert str(worker.id) in reconciled.cost_detail
    assert engine.paths() == []


async def test_solve_roster_task_survives_an_engine_outage(db_session, monkeypatch) -> None:
    await _scenario(db_session, award_for_c=True)
    async with FakeAwardEngine().client() as client:
        await populate_award_cost_matrix(db_session, DAY1, DAY2, client, context=CONTEXT)
    await db_session.commit()

    monkeypatch.setattr(
        tasks_module, "client_from_settings", lambda: FakeAwardEngine(fail_status=503).client()
    )
    result = await tasks_module.solve_roster_task({}, DAY1.isoformat(), DAY2.isoformat())

    assert result["status"] == "solved"
    assert result["cost_status"] == "engine_unavailable"
    assert result["engine_total_cost"] is None
    roster = (
        await db_session.execute(select(Roster).where(Roster.id == result["roster_id"]))
    ).scalar_one()
    assert roster.status is RosterStatus.SOLVED
    assert roster.cost_status is RosterCostStatus.ENGINE_UNAVAILABLE


async def test_sync_task_commits_and_returns_the_summary(db_session, monkeypatch) -> None:
    await _scenario(db_session, award_for_c=True)
    monkeypatch.setattr(tasks_module, "client_from_settings", lambda: FakeAwardEngine().client())

    result = await tasks_module.sync_award_matrix_task(
        {}, DAY1.isoformat(), (DAY1 + timedelta(days=1)).isoformat()
    )

    assert result["status"] == "ok"
    assert result["rows_written"] == 6
    assert result["cost_basis"] == "engine"
    rows = await _rows(db_session)
    assert len(rows) == 6
    assert not any(r.is_placeholder for r in rows.values())


async def test_sync_task_reports_engine_unavailable_and_keeps_placeholders(
    db_session, monkeypatch
) -> None:
    await _scenario(db_session)
    monkeypatch.setattr(
        tasks_module, "client_from_settings", lambda: FakeAwardEngine(fail_status=503).client()
    )

    result = await tasks_module.sync_award_matrix_task({}, DAY1.isoformat(), DAY2.isoformat())

    assert result["status"] == "engine_unavailable"
    assert result["placeholders_kept"] is True
    rows = await _rows(db_session)
    assert all(r.is_placeholder for r in rows.values())
