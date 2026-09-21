"""Tier 1 — batch rostering (OR-Tools CP-SAT). Consumes AwardCostMatrix; never computes pay.

``solver`` is the pure CP-SAT solver (no DB access). ``service`` is the
DB-facing boundary that loads inputs, calls it, and persists the result --
see its docstring.
"""

from app.services.rostering.solver import (
    Assignment,
    AwardCostMatrixEntry,
    RosterSolution,
    ShiftInput,
    SolveStatus,
    WorkerInput,
    solve_roster,
)

__all__ = [
    "Assignment",
    "AwardCostMatrixEntry",
    "RosterSolution",
    "ShiftInput",
    "SolveStatus",
    "WorkerInput",
    "solve_roster",
]
