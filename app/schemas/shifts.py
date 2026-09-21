"""Response schemas for `GET /shifts`."""

from __future__ import annotations

from datetime import date as date_
from datetime import time

from pydantic import BaseModel, ConfigDict

from app.schemas.common import SiteSummary


class ShiftRead(BaseModel):
    """One `Shift` row for the list endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    date: date_
    start_time: time
    end_time: time
    required_skill: str
    site: SiteSummary
    is_multi_stop: bool


class ShiftListResponse(BaseModel):
    """`GET /shifts` response: a page of shifts plus paging metadata."""

    items: list[ShiftRead]
    total: int
    limit: int
    offset: int
