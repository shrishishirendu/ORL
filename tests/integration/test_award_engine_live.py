"""Opt-in live test against a real Award Engine service (``engine-service/``).

Skipped unless ``AWARD_ENGINE_URL`` is set in the environment (and, like
every file here, unless Postgres is reachable). Start the engine with
``cd engine-service && npm start`` and run e.g.
``AWARD_ENGINE_URL=http://127.0.0.1:8790 pytest tests/integration/test_award_engine_live.py``.

Asserts only what ORL owns: the contract round-trips, rows land as engine
rows, re-syncing is deterministic, and reconciliation records a status. It
does not assert any $ figure -- those belong to the engine.
"""

from __future__ import annotations

import os
from datetime import date, time

import pytest
from sqlalchemy import select

from app.models.award_cost_matrix import AwardCostMatrix
from app.models.enums import EmploymentType, RosterCostStatus, RosterStatus
from app.services.award_engine.client import AwardEngineClient, EngineContext
from app.services.award_engine.matrix import populate_award_cost_matrix
from app.services.award_engine.reconcile import reconcile_roster_cost
from app.services.rostering.service import solve_and_persist_roster
from tests.integration.factories import make_shift, make_site, make_worker

ENGINE_URL = os.environ.get("AWARD_ENGINE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not ENGINE_URL, reason="AWARD_ENGINE_URL not set (live engine test)"),
]

DAY1 = date(2026, 10, 5)  # a Monday
DAY2 = date(2026, 10, 6)
CONTEXT = EngineContext(
    jurisdiction="NSW", legal_employer="Demo Security Pty Ltd", work_type="Static guarding"
)


async def _costs(session) -> dict[tuple[int, int], tuple]:
    stmt = select(AwardCostMatrix).execution_options(populate_existing=True)
    rows = (await session.execute(stmt)).scalars().all()
    return {(r.worker_id, r.shift_id): (r.pay_cost, r.eligible, r.is_placeholder) for r in rows}


async def test_live_sync_solve_and_reconcile(db_session) -> None:
    site = await make_site(db_session, "STH-01", region="South")
    for n in (1, 2):
        await make_worker(
            db_session,
            site,
            name=f"Guard {n}",
            skills=["guard"],
            award_code="MA000016",
            classification_level=f"MA000016::securityofficerlevel{n}",
            employment_type=EmploymentType.CASUAL,
        )
    await make_shift(
        db_session, site, date=DAY1, start_time=time(8), end_time=time(16), required_skill="guard"
    )
    await make_shift(
        db_session, site, date=DAY2, start_time=time(8), end_time=time(16), required_skill="guard"
    )
    await db_session.commit()

    async with AwardEngineClient(ENGINE_URL) as client:
        status, health = await client.health()
        assert status == 200 and health.status == "ok"

        summary = await populate_award_cost_matrix(
            db_session, DAY1, DAY2, client, context=CONTEXT
        )
        await db_session.commit()
        first = await _costs(db_session)
        assert summary.rows_written == 4
        assert summary.engine_commit == health.engine_commit
        assert all(is_placeholder is False for _, _, is_placeholder in first.values())

        # Determinism: an identical re-sync writes identical rows.
        await populate_award_cost_matrix(db_session, DAY1, DAY2, client, context=CONTEXT)
        await db_session.commit()
        assert await _costs(db_session) == first

        outcome = await solve_and_persist_roster(db_session, DAY1, DAY2)
        if summary.resolved_count == 0:
            pytest.skip(f"engine resolved no cells: {summary.unresolved_samples[:2]}")
        assert outcome.status is RosterStatus.SOLVED
        reconciled = await reconcile_roster_cost(
            db_session, outcome.roster_id, client, context=CONTEXT
        )

    assert reconciled.cost_status in {
        RosterCostStatus.ENGINE_EXACT,
        RosterCostStatus.ENGINE_UNRESOLVED,
    }, reconciled.cost_detail
    if reconciled.cost_status is RosterCostStatus.ENGINE_EXACT:
        assert reconciled.engine_total_cost is not None
        assert reconciled.engine_commit == health.engine_commit
    print(
        "live:",
        summary.as_dict()["resolved_count"],
        "resolved cells;",
        reconciled.cost_status,
        reconciled.engine_total_cost,
        "solver estimate",
        outcome.total_cost,
        reconciled.cost_detail,
    )
