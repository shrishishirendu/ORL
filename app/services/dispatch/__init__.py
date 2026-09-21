"""Tier 2 — per-shift dispatch/routing (OR-Tools Routing, VRPTW).

Consumes the Roster's shift assignments; single-site roles skip straight
to Site Assignment.

``solve_shift_route`` (in ``solver.py``) is the pure, DB-free VRPTW solver;
re-exported here for convenience.
"""

from app.services.dispatch.solver import (
    JobSpec,
    MissingTravelTimeError,
    RouteSolveResult,
    RouteStopPlan,
    TravelTimeMatrix,
    solve_shift_route,
)

__all__ = [
    "JobSpec",
    "MissingTravelTimeError",
    "RouteSolveResult",
    "RouteStopPlan",
    "TravelTimeMatrix",
    "solve_shift_route",
]
