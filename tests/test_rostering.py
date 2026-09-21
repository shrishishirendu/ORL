"""Tests for the Tier 1 rostering solver (app/services/rostering/solver.py).

All inputs here are small, hand-built dataclasses -- no DB, no fixtures --
per the task's "pure solver" requirement. Each test's expected result is
worked out by hand in its docstring/comments so the assertion is a check
against a known-correct answer, not just "it returned something".
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


def test_feasible_case_has_known_optimal_assignment() -> None:
    """Two non-overlapping shifts, two equally-skilled/eligible workers, no
    hour bounds. The only thing that decides the assignment is cost, and the
    costs are set up so exactly one assignment is cheapest:

        pay_cost:         shift A (08-12)   shift B (13-17)
        worker 1 (W1):        50                60
        worker 2 (W2):        70                40

    Optimal: W1 -> A (50), W2 -> B (40). Total = 90.
    (W1 -> B, W2 -> A would cost 60 + 70 = 130; W1 doing both is infeasible
    only if hours/overlap forbid it, which they don't here -- but it's not
    cheaper than splitting, so the optimal split is still unambiguous.)
    """
    w1 = WorkerInput(id=1, skills=frozenset({"care"}))
    w2 = WorkerInput(id=2, skills=frozenset({"care"}))
    shift_a = ShiftInput(
        id=100, date=DAY, start_time=time(8, 0), end_time=time(12, 0), required_skill="care"
    )
    shift_b = ShiftInput(
        id=101, date=DAY, start_time=time(13, 0), end_time=time(17, 0), required_skill="care"
    )
    matrix = [
        AwardCostMatrixEntry(worker_id=1, day=DAY, shift_id=100, pay_cost=50, eligible=True),
        AwardCostMatrixEntry(worker_id=1, day=DAY, shift_id=101, pay_cost=60, eligible=True),
        AwardCostMatrixEntry(worker_id=2, day=DAY, shift_id=100, pay_cost=70, eligible=True),
        AwardCostMatrixEntry(worker_id=2, day=DAY, shift_id=101, pay_cost=40, eligible=True),
    ]

    result = solve_roster([w1, w2], [shift_a, shift_b], matrix)

    assert result.status is SolveStatus.OPTIMAL
    assert result.is_feasible
    assert result.total_cost == 90.0
    assignments = {(a.worker_id, a.shift_id) for a in result.assignments}
    assert assignments == {(1, 100), (2, 101)}
    assert result.unfilled_shifts == []
    assert result.diagnostics == []


def test_infeasible_when_no_worker_has_the_required_skill() -> None:
    """One shift requires a skill ("forklift") that neither worker has.
    That shift has zero eligible+skilled candidates, so the whole roster is
    infeasible -- and the solver must say so cleanly (not raise), naming the
    offending shift.
    """
    w1 = WorkerInput(id=1, skills=frozenset({"care"}))
    w2 = WorkerInput(id=2, skills=frozenset({"care"}))
    shift = ShiftInput(
        id=200,
        date=DAY,
        start_time=time(8, 0),
        end_time=time(12, 0),
        required_skill="forklift",
    )
    # Eligible in the matrix, but neither worker has the "forklift" skill --
    # skill mismatch, not an eligibility problem, must still be caught.
    matrix = [
        AwardCostMatrixEntry(worker_id=1, day=DAY, shift_id=200, pay_cost=50, eligible=True),
        AwardCostMatrixEntry(worker_id=2, day=DAY, shift_id=200, pay_cost=50, eligible=True),
    ]

    result = solve_roster([w1, w2], [shift], matrix)

    assert result.status is SolveStatus.INFEASIBLE
    assert not result.is_feasible
    assert result.assignments == []
    assert result.total_cost is None
    assert result.unfilled_shifts == [200]
    assert len(result.diagnostics) == 1
    assert "200" in result.diagnostics[0]


def test_infeasible_when_eligible_matches_have_no_skill_and_ineligible_have_skill() -> None:
    """A worker can have the skill but be ineligible (award rule says no),
    and vice versa -- only the intersection counts as a candidate. Here
    worker 1 has the skill but is ineligible; worker 2 is eligible but
    lacks the skill. Zero candidates either way.
    """
    w1 = WorkerInput(id=1, skills=frozenset({"nursing"}))
    w2 = WorkerInput(id=2, skills=frozenset({"care"}))
    shift = ShiftInput(
        id=300,
        date=DAY,
        start_time=time(8, 0),
        end_time=time(12, 0),
        required_skill="nursing",
    )
    matrix = [
        AwardCostMatrixEntry(worker_id=1, day=DAY, shift_id=300, pay_cost=50, eligible=False),
        AwardCostMatrixEntry(worker_id=2, day=DAY, shift_id=300, pay_cost=50, eligible=True),
    ]

    result = solve_roster([w1, w2], [shift], matrix)

    assert result.status is SolveStatus.INFEASIBLE
    assert result.unfilled_shifts == [300]


def test_max_hours_binds_and_forces_a_more_expensive_assignment() -> None:
    """One cheap worker (W1) is eligible and skilled for both of two
    non-overlapping 4-hour shifts, and could do both on time grounds alone.
    But W1's max_hours is 4 (from the matrix), so taking both shifts (8h
    total) would violate it. A second, more expensive worker (W2) is the
    only way to cover the second shift.

        pay_cost:         shift A (08-12, 4h)   shift B (13-17, 4h)
        W1 (max 4h):             10                    10
        W2 (no bound):          100                   100

    Without the max_hours bound, the cheapest solution would be W1 on both
    shifts (cost 20). With it enforced, W1 can only take one shift and W2
    must cover the other: total = 10 + 100 = 110. This is the assertion that
    actually exercises the bound -- if the constraint were silently ignored,
    total_cost would come back as 20, not 110.
    """
    w1 = WorkerInput(id=1, skills=frozenset({"care"}))
    w2 = WorkerInput(id=2, skills=frozenset({"care"}))
    shift_a = ShiftInput(
        id=400, date=DAY, start_time=time(8, 0), end_time=time(12, 0), required_skill="care"
    )
    shift_b = ShiftInput(
        id=401, date=DAY, start_time=time(13, 0), end_time=time(17, 0), required_skill="care"
    )
    matrix = [
        AwardCostMatrixEntry(
            worker_id=1, day=DAY, shift_id=400, pay_cost=10, eligible=True, max_hours=4
        ),
        AwardCostMatrixEntry(
            worker_id=1, day=DAY, shift_id=401, pay_cost=10, eligible=True, max_hours=4
        ),
        AwardCostMatrixEntry(worker_id=2, day=DAY, shift_id=400, pay_cost=100, eligible=True),
        AwardCostMatrixEntry(worker_id=2, day=DAY, shift_id=401, pay_cost=100, eligible=True),
    ]

    result = solve_roster([w1, w2], [shift_a, shift_b], matrix)

    assert result.status is SolveStatus.OPTIMAL
    assert result.total_cost == 110.0
    # W1 takes exactly one of the two shifts (whichever -- symmetric cost),
    # W2 takes the other.
    w1_shifts = {a.shift_id for a in result.assignments if a.worker_id == 1}
    w2_shifts = {a.shift_id for a in result.assignments if a.worker_id == 2}
    assert len(w1_shifts) == 1
    assert len(w2_shifts) == 1
    assert w1_shifts | w2_shifts == {400, 401}


def test_min_hours_forces_worker_off_a_shift_they_would_otherwise_take() -> None:
    """W1 is by far the cheapest option for shift A, but W1's min_hours (8h)
    means *using W1 at all* commits to at least 8h of assigned shifts. The
    two shifts don't overlap, so W1 could legally take both -- but W1 is
    prohibitively expensive on shift B, so taking both to satisfy min_hours
    costs far more than not using W1 at all.

        shift A: 08:00-12:00 (4h), shift B: 13:00-17:00 (4h) -- no overlap.

        pay_cost:        shift A     shift B
        W1 (min 8h):         1         1000
        W2 (no bound):       50          50

    Without min_hours, the cheapest roster is W1->A (1) + W2->B (50) = 51.
    With min_hours=8h enforced, W1 can only be used by also taking B (since
    A alone is 4h < 8h, and A+B = 8h exactly meets it), which costs
    1 + 1000 = 1001 -- far worse than simply not using W1 and giving both
    shifts to W2: 50 + 50 = 100. So the constraint must force W1 out of the
    roster entirely, raising total_cost from 51 (unconstrained optimum) to
    100.
    """
    w1 = WorkerInput(id=1, skills=frozenset({"care"}))
    w2 = WorkerInput(id=2, skills=frozenset({"care"}))
    shift_a = ShiftInput(
        id=500, date=DAY, start_time=time(8, 0), end_time=time(12, 0), required_skill="care"
    )
    shift_b = ShiftInput(
        id=501, date=DAY, start_time=time(13, 0), end_time=time(17, 0), required_skill="care"
    )
    matrix = [
        AwardCostMatrixEntry(
            worker_id=1, day=DAY, shift_id=500, pay_cost=1, eligible=True, min_hours=8
        ),
        AwardCostMatrixEntry(
            worker_id=1, day=DAY, shift_id=501, pay_cost=1000, eligible=True, min_hours=8
        ),
        AwardCostMatrixEntry(worker_id=2, day=DAY, shift_id=500, pay_cost=50, eligible=True),
        AwardCostMatrixEntry(worker_id=2, day=DAY, shift_id=501, pay_cost=50, eligible=True),
    ]

    result = solve_roster([w1, w2], [shift_a, shift_b], matrix)

    assert result.status is SolveStatus.OPTIMAL
    assert result.total_cost == 100.0
    assignments = {(a.worker_id, a.shift_id) for a in result.assignments}
    assert assignments == {(2, 500), (2, 501)}


def test_infeasible_when_overlap_and_hours_conflict_with_no_zero_candidate_shift() -> None:
    """Every shift has at least one eligible/skilled candidate (so the
    "no candidates at all" fast path does NOT trigger), but the only
    candidate for two overlapping shifts is the same single worker -- so no
    combination of assignments can cover both. The solver must detect this
    via the CP-SAT solve itself (not the upfront candidate check) and still
    report cleanly, naming a shift that cannot be covered.
    """
    w1 = WorkerInput(id=1, skills=frozenset({"care"}))
    shift_a = ShiftInput(
        id=600, date=DAY, start_time=time(8, 0), end_time=time(12, 0), required_skill="care"
    )
    shift_b = ShiftInput(
        id=601, date=DAY, start_time=time(10, 0), end_time=time(14, 0), required_skill="care"
    )
    matrix = [
        AwardCostMatrixEntry(worker_id=1, day=DAY, shift_id=600, pay_cost=10, eligible=True),
        AwardCostMatrixEntry(worker_id=1, day=DAY, shift_id=601, pay_cost=10, eligible=True),
    ]

    result = solve_roster([w1], [shift_a, shift_b], matrix)

    assert result.status is SolveStatus.INFEASIBLE
    assert result.assignments == []
    assert result.total_cost is None
    # Exactly one of the two overlapping shifts is left uncovered -- W1 can
    # take the other, so the relaxed diagnostic solve drops only one.
    assert len(result.unfilled_shifts) == 1
    assert result.unfilled_shifts[0] in {600, 601}
    assert len(result.diagnostics) == 1


def test_empty_shift_list_is_trivially_optimal() -> None:
    result = solve_roster([], [], [])
    assert result.status is SolveStatus.OPTIMAL
    assert result.assignments == []
    assert result.total_cost == 0.0
