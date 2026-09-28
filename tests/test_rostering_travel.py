"""Tier 1: travel time between a worker's shifts at different sites
(app/services/rostering/solver.py, module docstring point 6).

Pure dataclass inputs, no DB. Each expected result is worked out by hand in
the test's docstring, as in tests/test_rostering.py.
"""

from __future__ import annotations

from datetime import date, time

from app.services.rostering.solver import (
    AwardCostMatrixEntry,
    ShiftInput,
    SolveStatus,
    WorkerInput,
    solve_roster,
)

DAY = date(2026, 1, 5)
NEXT_DAY = date(2026, 1, 6)
SITE_A, SITE_B = 1, 2


def shift(shift_id, start, end, site, day=DAY) -> ShiftInput:
    return ShiftInput(
        id=shift_id, date=day, start_time=start, end_time=end, required_skill="care", site_id=site
    )


def cost(worker_id, shift_obj, pay_cost) -> AwardCostMatrixEntry:
    return AwardCostMatrixEntry(
        worker_id=worker_id,
        day=shift_obj.date,
        shift_id=shift_obj.id,
        pay_cost=pay_cost,
        eligible=True,
    )


CHEAP = WorkerInput(id=1, skills=frozenset({"care"}))
PRICEY = WorkerInput(id=2, skills=frozenset({"care"}))


def two_shift_day(first_site, second_site, gap_end=time(12, 0), next_start=time(12, 30)):
    """Worker 1 is cheapest for both shifts (50 + 50); worker 2 costs 80 each."""
    first = shift(100, time(8, 0), gap_end, first_site)
    second = shift(101, next_start, time(17, 0), second_site)
    matrix = [cost(1, first, 50), cost(1, second, 50), cost(2, first, 80), cost(2, second, 80)]
    return [first, second], matrix


def assigned(result) -> set[tuple[int, int]]:
    return {(a.worker_id, a.shift_id) for a in result.assignments}


def test_too_little_time_to_travel_splits_the_shifts() -> None:
    """Gap 12:00 -> 12:30 is 30 minutes; A -> B takes 45. Worker 1 can't do
    both, so the cheapest roster is worker 1 on one shift (50) and worker 2 on
    the other (80): total 130, not 100."""
    shifts, matrix = two_shift_day(SITE_A, SITE_B)
    result = solve_roster([CHEAP, PRICEY], shifts, matrix, travel_minutes={(SITE_A, SITE_B): 45})
    assert result.status is SolveStatus.OPTIMAL
    assert result.total_cost == 130.0
    workers_per_shift = {shift_id: worker_id for worker_id, shift_id in assigned(result)}
    assert sorted(workers_per_shift.values()) == [1, 2]
    assert result.warnings == []


def test_a_gap_equal_to_the_travel_time_is_enough() -> None:
    """Gap 30, travel 30: worker 1 can make it, so they take both (100)."""
    shifts, matrix = two_shift_day(SITE_A, SITE_B)
    result = solve_roster([CHEAP, PRICEY], shifts, matrix, travel_minutes={(SITE_A, SITE_B): 30})
    assert result.total_cost == 100.0
    assert assigned(result) == {(1, 100), (1, 101)}


def test_shifts_at_the_same_site_need_no_travel() -> None:
    """Both shifts at site A: no travel, so worker 1 takes both (100), even
    with a long A <-> B travel time in the matrix."""
    shifts, matrix = two_shift_day(SITE_A, SITE_A)
    result = solve_roster(
        [CHEAP, PRICEY],
        shifts,
        matrix,
        travel_minutes={(SITE_A, SITE_B): 90, (SITE_B, SITE_A): 90},
    )
    assert assigned(result) == {(1, 100), (1, 101)}


def test_travel_time_is_directed() -> None:
    """First shift at B, second at A. B -> A takes 20 (fits the 30-minute
    gap); A -> B takes 60 but isn't the direction travelled. Worker 1 takes
    both (100)."""
    shifts, matrix = two_shift_day(SITE_B, SITE_A)
    result = solve_roster(
        [CHEAP, PRICEY],
        shifts,
        matrix,
        travel_minutes={(SITE_A, SITE_B): 60, (SITE_B, SITE_A): 20},
    )
    assert assigned(result) == {(1, 100), (1, 101)}


def test_an_overnight_shift_counts_its_real_finish() -> None:
    """Night shift at A 22:00 -> 06:00 (next day), then 06:30 -> 14:30 at B
    the next day. Gap 30, travel 45: worker 1 can't do both. Total 130."""
    night = shift(100, time(22, 0), time(6, 0), SITE_A)
    morning = shift(101, time(6, 30), time(14, 30), SITE_B, day=NEXT_DAY)
    matrix = [cost(1, night, 50), cost(1, morning, 50), cost(2, night, 80), cost(2, morning, 80)]
    result = solve_roster(
        [CHEAP, PRICEY], [night, morning], matrix, travel_minutes={(SITE_A, SITE_B): 45}
    )
    assert result.total_cost == 130.0


def test_a_missing_travel_time_is_reported_not_guessed() -> None:
    """No A -> B entry (only an unrelated 45-minute pair, so the longest known
    travel time is 45 and the 30-minute gap is within range). The pair is left
    unconstrained, so worker 1 takes both (100), and a warning names it."""
    shifts, matrix = two_shift_day(SITE_A, SITE_B)
    result = solve_roster([CHEAP, PRICEY], shifts, matrix, travel_minutes={(SITE_B, 3): 45})
    assert assigned(result) == {(1, 100), (1, 101)}
    assert result.warnings == [
        "No travel time from site 1 to site 2: shifts there were not checked for travel "
        "between them. Add the pair to the travel matrix."
    ]


def test_no_travel_data_at_all_is_one_warning() -> None:
    shifts, matrix = two_shift_day(SITE_A, SITE_B)
    result = solve_roster([CHEAP, PRICEY], shifts, matrix, travel_minutes={})
    assert assigned(result) == {(1, 100), (1, 101)}
    assert result.warnings == [
        "No travel times were loaded, so travel between a worker's shifts at different "
        "sites was not checked."
    ]


def test_callers_that_pass_no_travel_argument_keep_the_old_behaviour() -> None:
    shifts, matrix = two_shift_day(SITE_A, SITE_B)
    result = solve_roster([CHEAP, PRICEY], shifts, matrix)
    assert assigned(result) == {(1, 100), (1, 101)}
    assert result.warnings == []


def test_travel_alone_can_make_a_roster_infeasible() -> None:
    """Only worker 1 exists; they can't get from A to B in 30 minutes, so one
    shift is unfilled and the diagnostic names travel as a possible cause."""
    shifts, matrix = two_shift_day(SITE_A, SITE_B)
    matrix = [m for m in matrix if m.worker_id == 1]
    result = solve_roster([CHEAP], shifts, matrix, travel_minutes={(SITE_A, SITE_B): 45})
    assert result.status is SolveStatus.INFEASIBLE
    assert len(result.unfilled_shifts) == 1
    assert "travel-time-between-sites" in result.diagnostics[0]
