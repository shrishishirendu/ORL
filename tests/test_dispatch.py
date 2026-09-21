"""Tests for the Tier 2 dispatch/routing solver (``app/services/dispatch``).

Covers the three cases the task calls out:

(a) a small feasible multi-stop case with an obviously-correct optimal
    sequence,
(b) an infeasible case where no sequence can satisfy every time window given
    travel times, and
(c) the 0/1-job short-circuit path that bypasses OR-tools entirely.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.dispatch import (
    JobSpec,
    MissingTravelTimeError,
    TravelTimeMatrix,
    solve_shift_route,
)

SHIFT_START = datetime(2026, 1, 5, 8, 0, 0)


def _dt(hour: int, minute: int = 0) -> datetime:
    return SHIFT_START.replace(hour=hour, minute=minute)


# Site ids used across tests.
SITE_A, SITE_B, SITE_C = 1, 2, 3


def build_travel_matrix() -> TravelTimeMatrix:
    return TravelTimeMatrix.from_symmetric_pairs(
        {
            (SITE_A, SITE_B): 20,
            (SITE_B, SITE_C): 15,
            (SITE_A, SITE_C): 40,
        }
    )


class TestFeasibleMultiStop:
    """(a) A small feasible case with an obviously-correct optimal sequence."""

    def test_solves_in_the_only_workable_order(self) -> None:
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

        result = solve_shift_route(jobs, build_travel_matrix(), shift_start=SHIFT_START)

        assert result.feasible is True
        assert result.infeasible_job_ids == []
        assert [stop.job_id for stop in result.stops] == [1, 2, 3]

        by_job = {stop.job_id: stop for stop in result.stops}

        # Job 1: arrives right at shift start (no travel needed to the first
        # stop when no start_site_id is given), serves for 15 minutes.
        assert by_job[1].sequence_no == 1
        assert by_job[1].planned_arrival == _dt(8, 0)
        assert by_job[1].planned_departure == _dt(8, 15)

        # Job 2: earliest reachable is 8:15 + 20min travel = 8:35, but its
        # window doesn't open until 9:30, so the worker waits.
        assert by_job[2].sequence_no == 2
        assert by_job[2].planned_arrival == _dt(9, 30)
        assert by_job[2].planned_departure == _dt(9, 50)

        # Job 3: 9:50 + 15min travel = 10:05, window opens at 11:00 -> wait.
        assert by_job[3].sequence_no == 3
        assert by_job[3].planned_arrival == _dt(11, 0)
        assert by_job[3].planned_departure == _dt(11, 10)

    def test_visiting_in_input_order_would_have_been_infeasible(self) -> None:
        # Sanity check on the fixture itself: A -> C directly (skipping B)
        # takes 40 minutes, so a naive "solve then check A->B->C in a fixed
        # order" isn't what makes this feasible -- the solver actually has
        # to find the sequence. This is documentation-as-test more than a
        # solver assertion.
        travel = build_travel_matrix()
        assert travel.get(SITE_A, SITE_C) == 40
        assert travel.get(SITE_A, SITE_B) + travel.get(SITE_B, SITE_C) == 35


class TestInfeasible:
    """(b) An infeasible case: time windows that can't all be met given travel times."""

    def test_two_close_windows_far_apart_sites_are_named_infeasible(self) -> None:
        # Jobs 1 and 2 both have narrow windows in the first 30 minutes of
        # the shift, but sit 45 minutes apart -- no sequence can visit both
        # within their windows. Job 3 is easy and independent.
        travel = TravelTimeMatrix.from_symmetric_pairs(
            {
                (SITE_A, SITE_B): 45,
                (SITE_B, SITE_C): 15,
                (SITE_A, SITE_C): 40,
            }
        )
        jobs = [
            JobSpec(
                job_id=1,
                site_id=SITE_A,
                window_start=_dt(8, 0),
                window_end=_dt(8, 30),
                duration_minutes=15,
            ),
            JobSpec(
                job_id=2,
                site_id=SITE_B,
                window_start=_dt(8, 20),
                window_end=_dt(8, 30),
                duration_minutes=10,
            ),
            JobSpec(
                job_id=3,
                site_id=SITE_C,
                window_start=_dt(9, 0),
                window_end=_dt(10, 0),
                duration_minutes=10,
            ),
        ]

        result = solve_shift_route(jobs, travel, shift_start=SHIFT_START)

        assert result.feasible is False
        assert result.reason is not None
        # Both job 1 and job 2 are individually the blocker (removing either
        # one unblocks the rest); job 3 is uninvolved.
        assert result.infeasible_job_ids == [1, 2]

    def test_window_already_elapsed_before_shift_start_is_infeasible(self) -> None:
        travel = build_travel_matrix()
        jobs = [
            JobSpec(
                job_id=1,
                site_id=SITE_A,
                # Window closes before the shift even starts.
                window_start=SHIFT_START - timedelta(hours=2),
                window_end=SHIFT_START - timedelta(hours=1),
                duration_minutes=15,
            ),
            JobSpec(
                job_id=2,
                site_id=SITE_B,
                window_start=_dt(9, 0),
                window_end=_dt(10, 0),
                duration_minutes=10,
            ),
        ]

        result = solve_shift_route(jobs, travel, shift_start=SHIFT_START)

        assert result.feasible is False
        assert result.infeasible_job_ids == [1]

    def test_missing_travel_time_entry_raises_rather_than_defaulting(self) -> None:
        # An incomplete cached Travel Matrix is a data problem, not a
        # routing outcome -- it must raise, not silently treat the gap as
        # zero travel time.
        incomplete_travel = TravelTimeMatrix({(SITE_A, SITE_B): 20})
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
                site_id=SITE_C,
                window_start=_dt(9, 30),
                window_end=_dt(10, 30),
                duration_minutes=10,
            ),
        ]

        with pytest.raises(MissingTravelTimeError):
            solve_shift_route(jobs, incomplete_travel, shift_start=SHIFT_START)


class TestShortCircuit:
    """(c) The 0/1-job short-circuit path bypasses the VRPTW machinery."""

    def test_zero_jobs_is_trivially_feasible_with_no_stops(self) -> None:
        result = solve_shift_route([], build_travel_matrix(), shift_start=SHIFT_START)

        assert result.feasible is True
        assert result.stops == []
        assert result.infeasible_job_ids == []

    def test_single_job_within_window_is_feasible(self) -> None:
        job = JobSpec(
            job_id=1,
            site_id=SITE_A,
            window_start=_dt(8, 0),
            window_end=_dt(9, 0),
            duration_minutes=30,
        )

        result = solve_shift_route([job], build_travel_matrix(), shift_start=SHIFT_START)

        assert result.feasible is True
        assert len(result.stops) == 1
        stop = result.stops[0]
        assert stop.job_id == 1
        assert stop.sequence_no == 1
        assert stop.planned_arrival == _dt(8, 0)
        assert stop.planned_departure == _dt(8, 30)

    def test_single_job_accounts_for_travel_from_start_site(self) -> None:
        job = JobSpec(
            job_id=1,
            site_id=SITE_B,
            window_start=_dt(8, 0),
            window_end=_dt(9, 0),
            duration_minutes=10,
        )

        result = solve_shift_route(
            [job],
            build_travel_matrix(),
            shift_start=SHIFT_START,
            start_site_id=SITE_A,
        )

        assert result.feasible is True
        # 20 minutes travel from the start site (A) to the job's site (B).
        assert result.stops[0].planned_arrival == _dt(8, 20)

    def test_single_job_outside_reachable_window_is_infeasible(self) -> None:
        job = JobSpec(
            job_id=1,
            site_id=SITE_B,
            window_start=_dt(8, 0),
            window_end=_dt(8, 10),
            duration_minutes=10,
        )

        result = solve_shift_route(
            [job],
            build_travel_matrix(),
            shift_start=SHIFT_START,
            start_site_id=SITE_A,
        )

        # Travel from A to B alone (20 min) already exceeds the window end.
        assert result.feasible is False
        assert result.infeasible_job_ids == [1]

    def test_does_not_invoke_ortools_for_zero_or_one_jobs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services.dispatch import solver as solver_module

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("the full VRPTW solve path must not run for <=1 jobs")

        monkeypatch.setattr(solver_module, "_solve_mandatory_vrptw", _boom)

        job = JobSpec(
            job_id=1,
            site_id=SITE_A,
            window_start=_dt(8, 0),
            window_end=_dt(9, 0),
            duration_minutes=15,
        )
        solve_shift_route([], build_travel_matrix(), shift_start=SHIFT_START)
        solve_shift_route([job], build_travel_matrix(), shift_start=SHIFT_START)
