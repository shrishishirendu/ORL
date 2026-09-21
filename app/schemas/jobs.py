"""Shared response schemas for reporting an async task-queue job's
enqueue/status/result -- used identically by all three routers
(``app/api/rostering.py``, ``app/api/dispatch.py``, ``app/api/events.py``).

FLAG FOR REVIEW: the exact response shape for "job status" isn't specified
by the task brief beyond "returns arq job status/result". ``JobStatusResponse``
mirrors arq's own ``JobStatus`` enum values (``deferred``/``queued``/
``in_progress``/``complete``/``not_found``) in ``status``, and surfaces
``result``/``success`` only once the job has actually finished (``result``
is ``None`` beforehand, and holds ``str(exception)`` rather than the raw
exception object when the job raised, so the response stays JSON-safe).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class JobEnqueuedResponse(BaseModel):
    """Returned immediately by every `POST .../solve` / `POST /events` call."""

    job_id: str


class JobStatusResponse(BaseModel):
    """Returned by every `GET .../jobs/{job_id}` call."""

    job_id: str
    status: str
    success: bool | None = None
    result: Any | None = None
