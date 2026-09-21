"""Tier 3 — event-driven intra-day re-optimization.

Scoped re-solve back into Tier 2; escalates to Tier 1 only when a shift
becomes infeasible.

``handle_event`` (in ``solver.py``) is the pure, DB-free Tier 3 entry point;
re-exported here for convenience.
"""

from app.services.reoptimization.solver import (
    EventType,
    OutcomeType,
    ReoptimizationEvent,
    ReoptimizationResult,
    handle_event,
)

__all__ = [
    "EventType",
    "OutcomeType",
    "ReoptimizationEvent",
    "ReoptimizationResult",
    "handle_event",
]
