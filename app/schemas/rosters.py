"""Response schemas for `GET /rosters` and `GET /rosters/{id}`."""

from __future__ import annotations

from datetime import date as date_
from datetime import datetime, time

from pydantic import BaseModel, ConfigDict

from app.schemas.common import SiteSummary


class RosterSummary(BaseModel):
    """One `Roster` row for the list endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    period_start: date_
    period_end: date_
    generated_at: datetime
    status: str
    # `total_cost` is Tier 1's solver estimate; `engine_total_cost` is the
    # award engine's exact figure for the solved roster, when there is one.
    # `cost_status` says which to trust (see app.models.enums.RosterCostStatus).
    total_cost: float | None = None
    failure_reason: str | None = None
    engine_total_cost: float | None = None
    engine_commit: str | None = None
    cost_status: str | None = None
    cost_detail: str | None = None


class RosterListResponse(BaseModel):
    """`GET /rosters` response: a page of rosters plus paging metadata."""

    items: list[RosterSummary]
    total: int
    limit: int
    offset: int


class WorkerBrief(BaseModel):
    """The minimal worker identity shown on a roster assignment row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    region: str | None = None


class ShiftBrief(BaseModel):
    """The minimal shift identity shown on a roster assignment row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    date: date_
    start_time: time
    end_time: time
    required_skill: str
    site: SiteSummary
    is_multi_stop: bool


class RosterAssignmentRead(BaseModel):
    """One `RosterAssignment` row within `GET /rosters/{id}`."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    day: date_
    worker: WorkerBrief
    shift: ShiftBrief


class RosterDetail(RosterSummary):
    """`GET /rosters/{id}` response: a roster plus its assignments."""

    assignments: list[RosterAssignmentRead]
