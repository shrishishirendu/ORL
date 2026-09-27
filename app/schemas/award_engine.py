"""Request/response schemas for the `/award-engine/*` endpoints."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, model_validator


class MatrixSyncRequest(BaseModel):
    """`POST /award-engine/sync-matrix`: price every shift in the period."""

    period_start: date
    period_end: date

    @model_validator(mode="after")
    def _period_in_order(self) -> MatrixSyncRequest:
        if self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        return self


class AwardEngineHealthResponse(BaseModel):
    """`GET /award-engine/health`.

    `configured` is false when `AWARD_ENGINE_URL` is unset (engine disabled).
    `status` is the engine's own `ok`/`degraded`, or `disabled` /
    `unreachable` from ORL's side. `engine` is the engine's health body,
    passed through unchanged when there is one.
    """

    configured: bool
    status: str
    engine: dict[str, Any] | None = None
    detail: str | None = None
