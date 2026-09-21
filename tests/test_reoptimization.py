"""Tests for the Tier 3 intra-day re-optimization module
(``app/services/reoptimization``).

Covers, per the task:

- ``job_cancelled``: a clean scoped-resolve case, an escalation case, and the
  trivial "nothing left to do" edge case (cancelling the last remaining job).
- ``visit_overran``: a clean scoped-resolve case and an escalation case.
- ``worker_sick``: the automatic, unconditional escalation to Tier 1 (no
  Tier 2 call at all).

Plus a couple of malformed-event guards (``ValueError``, not a
re-optimization outcome).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services.dispatch import JobSpec, TravelTimeMatrix
from app.services.reoptimization import (
    EventType,
    OutcomeType,
    ReoptimizationEvent,
    handle_event,
)

SHIFT_START = datetime(2026, 1, 5, 8, 0, 0)


def _dt(hour: int, minute: int = 0) -> datetime:
    return SHIFT_START.replace(hour=hour, minute=minute)


# Site ids for the job_cancelled fixtures below -- mirrors
# tests/test_dispatch.py's fixture shape (distinct home vs. job sites).
SITE_A, SITE_B, SITE_C, HOME = 1, 2, 3, 4


def _job_cancelled_travel_matrix() -> TravelTimeMatrix:
    return TravelTimeMatrix.from_symmetric_pairs(
        {
            (SITE_A, SITE_B): 20,
            (SITE_B, SITE_C): 15,
            (SITE_A, SITE_C): 40,
            (HOME, SITE_A): 10,
            (HOME, SITE_B): 30,
            (HOME, SITE_C): 50,
        }
    )


class TestJobCancelled:
    """`job_cancelled`: remove the job, re-solve the reduced list."""

    def test_scoped_resolve_reroutes_around_the_cancelled_job(self) -> None:
        jobs = [
            JobSpec(
                job_id=1,
                site_id=SITE_A,
                window_start=_dt(8, 0),
                window_end=_dt(9, 0),
                duration_minutes=15,
            ),
            JobSpec(
                job_id=2,
                site_id=SITE_B,
                window_start=_dt(9, 30),
                window_end=_dt(10, 30),
                duration_minutes=20,
            ),
            JobSpec(
                job_id=3,
                site_id=SITE_C,
                window_start=_dt(11, 0),
                window_end=_dt(12, 0),
                duration_minutes=10,
            ),
        ]
        event = ReoptimizationEvent(
            event_type=EventType.JOB_CANCELLED,
            shift_id=1,
            worker_id=1,
            home_site_id=HOME,
            remaining_jobs=jobs,
            travel_matrix=_job_cancelled_travel_matrix(),
            current_time=SHIFT_START,
            cancelled_job_id=2,
        )

        result = handle_event(event)

        assert result.outcome is OutcomeType.SCOPED_RESOLVE
        assert result.event_type is EventType.JOB_CANCELLED
        assert result.escalation_reason is None
        assert result.route is not None
        assert result.route.feasible is True
        assert [stop.job_id for stop in result.route.stops] == [1, 3]
        # The cancelled job never reappears in the new plan.
        assert all(stop.job_id != 2 for stop in result.route.stops)

    def test_cancelling_the_last_remaining_job_is_a_trivial_go_straight_home_resolve(
        self,
    ) -> None:
        jobs = [
            JobSpec(
                job_id=1,
                site_id=SITE_A,
                window_start=_dt(8, 0),
                window_end=_dt(9, 0),
                duration_minutes=15,
            )
        ]
        event = ReoptimizationEvent(
            event_type=EventType.JOB_CANCELLED,
            shift_id=1,
            worker_id=1,
            home_site_id=HOME,
            remaining_jobs=jobs,
            travel_matrix=_job_cancelled_travel_matrix(),
            current_site_id=SITE_A,  # worker already there when it's cancelled
            current_time=_dt(8, 5),
            cancelled_job_id=1,
        )

        result = handle_event(event)

        assert result.outcome is OutcomeType.SCOPED_RESOLVE
        assert result.route is not None
        assert result.route.feasible is True
        assert result.route.stops == []
        assert result.route.return_to_home_arrival is None
        assert result.detail is not None and "no jobs remain" in result.detail

    def test_escalates_when_the_reduced_route_is_infeasible_from_current_position(
        self,
    ) -> None:
        # Mid-shift: the worker is currently far from home at CURRENT, and
        # job 2 (the one that survives cancellation) has a window that closes
        # long before travel from CURRENT could reach it. Cancelling job 1
        # cannot fix job 2's problem -- it is infeasible on its own.
        CURRENT = 5
        travel = TravelTimeMatrix.from_symmetric_pairs(
            {
                (CURRENT, SITE_B): 30,
                (HOME, SITE_B): 30,
            }
        )
        jobs = [
            JobSpec(
                job_id=1,
                site_id=SITE_A,
                window_start=_dt(9, 0),
                window_end=_dt(10, 0),
                duration_minutes=10,
            ),
            JobSpec(
                job_id=2,
                site_id=SITE_B,
                window_start=_dt(10, 0),
                window_end=_dt(10, 5),
                duration_minutes=10,
            ),
        ]
        event = ReoptimizationEvent(
            event_type=EventType.JOB_CANCELLED,
            shift_id=1,
            worker_id=1,
            home_site_id=HOME,
            remaining_jobs=jobs,
            travel_matrix=travel,
            current_site_id=CURRENT,
            current_time=_dt(10, 0),
            cancelled_job_id=1,
        )

        result = handle_event(event)

        assert result.outcome is OutcomeType.ESCALATED_TO_TIER1
        assert result.route is None
        assert result.escalation_reason is not None
        assert "shift 1" in result.escalation_reason
        assert "worker 1" in result.escalation_reason

    def test_missing_cancelled_job_id_raises(self) -> None:
        event = ReoptimizationEvent(
            event_type=EventType.JOB_CANCELLED,
            shift_id=1,
            worker_id=1,
            home_site_id=HOME,
            remaining_jobs=[],
            travel_matrix=_job_cancelled_travel_matrix(),
        )
        with pytest.raises(ValueError):
            handle_event(event)

    def test_cancelled_job_id_not_in_remaining_jobs_raises(self) -> None:
        jobs = [
            JobSpec(
                job_id=1,
                site_id=SITE_A,
                window_start=_dt(8, 0),
                window_end=_dt(9, 0),
                duration_minutes=15,
            )
        ]
        event = ReoptimizationEvent(
            event_type=EventType.JOB_CANCELLED,
            shift_id=1,
            worker_id=1,
            home_site_id=HOME,
            remaining_jobs=jobs,
            travel_matrix=_job_cancelled_travel_matrix(),
            cancelled_job_id=999,
        )
        with pytest.raises(ValueError):
            handle_event(event)


# Sites for the visit_overran fixtures -- a different worker mid-route,
# already away from home when the overrun is reported.
HOME2, CURRENT2, SITE_D, SITE_E = 10, 20, 30, 40


class TestVisitOverran:
    """`visit_overran`: re-solve the remaining jobs from the worker's actual
    (later than planned) position and time."""

    def test_scoped_resolve_refits_remaining_jobs_around_the_delay(self) -> None:
        travel = TravelTimeMatrix.from_symmetric_pairs(
            {
                (CURRENT2, SITE_D): 10,
                (CURRENT2, SITE_E): 25,
                (SITE_D, SITE_E): 15,
                (SITE_D, HOME2): 20,
                (SITE_E, HOME2): 10,
            }
        )
        remaining = [
            JobSpec(
                job_id=10,
                site_id=SITE_D,
                window_start=_dt(13, 0),
                window_end=_dt(15, 0),
                duration_minutes=10,
            ),
            JobSpec(
                job_id=11,
                site_id=SITE_E,
                window_start=_dt(13, 0),
                window_end=_dt(15, 0),
                duration_minutes=10,
            ),
        ]
        event = ReoptimizationEvent(
            event_type=EventType.VISIT_OVERRAN,
            shift_id=2,
            worker_id=7,
            home_site_id=HOME2,
            remaining_jobs=remaining,
            travel_matrix=travel,
            current_site_id=CURRENT2,
            current_time=_dt(13, 0),
            planned_time=_dt(12, 30),  # the plan assumed the worker would be free by 12:30
        )

        result = handle_event(event)

        assert result.outcome is OutcomeType.SCOPED_RESOLVE
        assert result.route is not None
        assert result.route.feasible is True
        assert {stop.job_id for stop in result.route.stops} == {10, 11}
        assert result.route.return_to_home_arrival is not None

    def test_escalates_when_the_overrun_makes_a_remaining_window_unreachable(self) -> None:
        travel = TravelTimeMatrix.from_symmetric_pairs(
            {
                (CURRENT2, SITE_D): 10,
                (SITE_D, HOME2): 20,
            }
        )
        # Only one job left, and the overrun has eaten almost all of its
        # window: travel alone (10 min) already exceeds the 5 minutes left.
        remaining = [
            JobSpec(
                job_id=12,
                site_id=SITE_D,
                window_start=_dt(13, 0),
                window_end=_dt(13, 5),
                duration_minutes=10,
            )
        ]
        event = ReoptimizationEvent(
            event_type=EventType.VISIT_OVERRAN,
            shift_id=2,
            worker_id=7,
            home_site_id=HOME2,
            remaining_jobs=remaining,
            travel_matrix=travel,
            current_site_id=CURRENT2,
            current_time=_dt(13, 0),
        )

        result = handle_event(event)

        assert result.outcome is OutcomeType.ESCALATED_TO_TIER1
        assert result.route is None
        assert result.escalation_reason is not None
        assert "overrun" in result.escalation_reason

    def test_missing_current_position_or_time_raises(self) -> None:
        event = ReoptimizationEvent(
            event_type=EventType.VISIT_OVERRAN,
            shift_id=2,
            worker_id=7,
            home_site_id=HOME2,
            remaining_jobs=[],
            travel_matrix=TravelTimeMatrix({}),
        )
        with pytest.raises(ValueError):
            handle_event(event)


class TestWorkerSick:
    """`worker_sick`: always an automatic escalation -- no Tier 2 call."""

    def test_always_escalates_without_calling_the_dispatch_solver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services.reoptimization import solver as reopt_solver

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError(
                "worker_sick must never call solve_shift_route -- Tier 2 cannot "
                "re-route a worker who isn't there"
            )

        monkeypatch.setattr(reopt_solver, "solve_shift_route", _boom)

        remaining = [
            JobSpec(
                job_id=20,
                site_id=SITE_A,
                window_start=_dt(9, 0),
                window_end=_dt(10, 0),
                duration_minutes=15,
            )
        ]
        event = ReoptimizationEvent(
            event_type=EventType.WORKER_SICK,
            shift_id=3,
            worker_id=99,
            home_site_id=HOME,
            remaining_jobs=remaining,
            travel_matrix=_job_cancelled_travel_matrix(),
            current_time=_dt(8, 30),
        )

        result = handle_event(event)

        assert result.outcome is OutcomeType.ESCALATED_TO_TIER1
        assert result.event_type is EventType.WORKER_SICK
        assert result.route is None
        assert result.escalation_reason is not None
        assert "worker 99" in result.escalation_reason
        assert "shift 3" in result.escalation_reason

    def test_escalates_even_with_zero_remaining_jobs(self) -> None:
        # Nothing left to re-route, but per this module's policy (see the
        # module docstring's flag), worker_sick still escalates every time --
        # it never special-cases "nothing remains" into a silent no-op.
        event = ReoptimizationEvent(
            event_type=EventType.WORKER_SICK,
            shift_id=3,
            worker_id=99,
            home_site_id=HOME,
            remaining_jobs=[],
            travel_matrix=_job_cancelled_travel_matrix(),
        )

        result = handle_event(event)

        assert result.outcome is OutcomeType.ESCALATED_TO_TIER1
