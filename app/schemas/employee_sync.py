"""Request/response schemas for `POST /workers/sync-employees` (see
app/services/employee_sync/)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class SyncEmployeesRequest(BaseModel):
    """`apply: false` (the default) previews. `apply: true` also fills blank
    ORL fields from award-intelligence. It never overwrites a value ORL
    already has."""

    apply: bool = False


class FieldOutcome(StrEnum):
    SAME = "same"
    # ORL is blank and award-intelligence has a value: apply fills it.
    FILL = "fill"
    # Both have a value and they differ: reported, never overwritten.
    CONFLICT = "conflict"
    # award-intelligence's value can't be expressed as an ORL value
    # (e.g. an employment type ORL doesn't know): reported, never written.
    UNMAPPABLE = "unmappable"
    # award-intelligence has no value: nothing to compare.
    MASTER_BLANK = "master_blank"


class FieldComparison(BaseModel):
    field: str
    orl_value: str | None
    master_value: str | None
    outcome: FieldOutcome
    applied: bool = False


class MatchedEmployee(BaseModel):
    worker_id: int
    worker_name: str
    employee_code: str
    fields: list[FieldComparison]
    notes: list[str] = []


class OrlOnlyWorker(BaseModel):
    worker_id: int
    worker_name: str
    employee_code: str | None


class MasterOnlyEmployee(BaseModel):
    employee_id: str
    employment_type: str
    award_code: str
    state_code: str
    source_classification: str


class SyncSource(BaseModel):
    audit_id: str | None
    created_at: str | None
    source_name: str
    last_date: str


class SyncSummary(BaseModel):
    orl_workers: int
    master_employees: int
    matched: int
    fills: int
    conflicts: int
    unmappable: int
    orl_only: int
    orl_without_code: int
    master_only: int


class SyncEmployeesResponse(BaseModel):
    applied: bool
    source: SyncSource
    summary: SyncSummary
    matched: list[MatchedEmployee]
    # ORL workers whose employee_code isn't in the employee master.
    orl_only: list[OrlOnlyWorker]
    # ORL workers with no employee_code: they can't be matched at all.
    orl_without_code: list[OrlOnlyWorker]
    # In the employee master but not in ORL. ORL can't create these itself:
    # skills and a home site don't exist in award-intelligence.
    master_only: list[MasterOnlyEmployee]
    next_steps: list[str]
