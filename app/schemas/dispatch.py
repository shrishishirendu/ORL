"""Request schema for `POST /dispatch/solve`."""

from __future__ import annotations

from pydantic import BaseModel


class DispatchSolveRequest(BaseModel):
    """A Tier 2 solve request for one worker's shift.

    Naming this by ``roster_assignment_id`` (not, say, ``shift_id`` +
    ``worker_id``) matches ``app.services.dispatch.service.
    solve_and_persist_route``'s own single-argument shape -- the caller is
    always acting on one specific Tier 1 output row.

    Calling this for a single-site ``RosterAssignment`` (``Shift.
    is_multi_stop is False``) is not an error: the service bypasses Tier 2
    entirely and writes a ``SiteAssignment`` instead of a ``Route`` -- see
    ``DispatchOutcome.mode``.
    """

    roster_assignment_id: int
