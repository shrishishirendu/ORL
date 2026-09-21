"""Request schema for `POST /rostering/solve`."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class RosterSolveRequest(BaseModel):
    """A Tier 1 batch-solve request for one rostering period."""

    period_start: date
    period_end: date
