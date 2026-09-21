"""Request schema for `POST /events` -- validated against the three Tier 3
event shapes from ``app.services.reoptimization.solver``
(``EventType.JOB_CANCELLED`` / ``WORKER_SICK`` / ``VISIT_OVERRAN``).

``EventRequest`` is a discriminated union on ``event_type``, so a malformed
event (e.g. a ``visit_overran`` payload missing ``current_time``) is
rejected by FastAPI's request validation (422) before it is ever enqueued,
rather than failing later inside the task/solver.

``completed_job_ids`` is common to all three shapes -- see
``app.services.reoptimization.service.IncomingReoptimizationEvent``'s
docstring for why the caller supplies it explicitly (the schema has no
persisted "job completed" state to derive it from).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class JobCancelledEvent(BaseModel):
    """A job on this shift was cancelled -- drop it and re-sequence the rest."""

    event_type: Literal["job_cancelled"]
    shift_id: int
    worker_id: int
    cancelled_job_id: int
    current_site_id: int | None = None
    current_time: datetime | None = None
    completed_job_ids: list[int] = Field(default_factory=list)


class WorkerSickEvent(BaseModel):
    """The worker is unavailable for the rest of the shift -- always escalates
    to Tier 1 (see ``app.services.reoptimization.solver``'s module docstring).
    """

    event_type: Literal["worker_sick"]
    shift_id: int
    worker_id: int
    current_site_id: int | None = None
    current_time: datetime | None = None
    completed_job_ids: list[int] = Field(default_factory=list)


class VisitOverranEvent(BaseModel):
    """A visit ran long -- re-solve the remaining jobs from the worker's
    actual current position/time. ``current_site_id``/``current_time`` are
    required here (not optional, unlike the other two event types) because
    "the worker's actual current position/time" is this event's whole
    point -- mirroring
    ``app.services.reoptimization.solver.ReoptimizationEvent``'s own
    docstring on this.
    """

    event_type: Literal["visit_overran"]
    shift_id: int
    worker_id: int
    current_site_id: int
    current_time: datetime
    planned_time: datetime | None = None
    completed_job_ids: list[int] = Field(default_factory=list)


EventRequest = Annotated[
    JobCancelledEvent | WorkerSickEvent | VisitOverranEvent,
    Field(discriminator="event_type"),
]
