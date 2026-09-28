"""A ``worker_sick`` escalation must re-roster the shift's date *without* the
sick worker. ``handle_reoptimization_event_task`` is called directly, with a
stand-in for arq's ``ctx["redis"]`` that records the chained job instead of
queueing it (the real-queue path is covered by ``test_e2e_queue.py``).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

import pytest

import app.workers.tasks as tasks_module
from app.workers.tasks import handle_reoptimization_event_task, solve_roster_task
from tests.integration.factories import (
    make_award_row,
    make_job,
    make_roster,
    make_roster_assignment,
    make_shift,
    make_site,
    make_travel_entry,
    make_worker,
)

pytestmark = pytest.mark.asyncio

SHIFT_DATE = date(2026, 3, 9)


class RecordingRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, name: str, *args: Any) -> Any:
        self.jobs.append((name, args))

        class _Job:
            job_id = f"job-{len(self.jobs)}"

        return _Job()


async def test_sick_escalation_excludes_the_sick_worker_from_the_re_roster(db_session, monkeypatch) -> None:
    # Keep the test hermetic: no award engine for post-solve reconciliation.
    monkeypatch.setattr(tasks_module, "client_from_settings", lambda: None)
    home = await make_site(db_session, "SICK-HOME", region="North")
    site_a = await make_site(db_session, "SICK-A", region="North")
    sick = await make_worker(db_session, home, name="Sick", skills=["driver"])
    cover = await make_worker(db_session, home, name="Cover", skills=["driver"])
    shift = await make_shift(
        db_session, home, date=SHIFT_DATE, start_time=time(8, 0), end_time=time(16, 0),
        required_skill="driver", is_multi_stop=True,
    )
    await make_job(
        db_session, shift, site_a,
        window_start=datetime(2026, 3, 9, 9, 0), window_end=datetime(2026, 3, 9, 10, 0),
        duration_minutes=15,
    )
    await make_travel_entry(db_session, home, site_a, 10)
    # The sick worker is the cheaper option, so a re-roster that forgot the
    # sickness would hand the shift straight back to them.
    await make_award_row(db_session, sick, shift, pay_cost=100.00)
    await make_award_row(db_session, cover, shift, pay_cost=150.00)
    roster = await make_roster(db_session, period_start=SHIFT_DATE, period_end=SHIFT_DATE)
    await make_roster_assignment(db_session, roster, sick, shift)
    await db_session.commit()

    redis = RecordingRedis()
    result = await handle_reoptimization_event_task(
        {"redis": redis},
        {"event_type": "worker_sick", "shift_id": shift.id, "worker_id": sick.id},
    )

    assert result["outcome"] == "escalated_to_tier1"
    assert redis.jobs == [("solve_roster_task", (SHIFT_DATE.isoformat(), SHIFT_DATE.isoformat(), [sick.id]))]

    # Run the chained job as arq would.
    _, args = redis.jobs[0]
    re_roster = await solve_roster_task({}, *args)
    assert re_roster["status"] == "solved"
    assert re_roster["excluded_worker_ids"] == [sick.id]
    assert re_roster["total_cost"] == pytest.approx(150.00)
