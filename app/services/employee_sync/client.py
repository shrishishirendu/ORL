"""Typed HTTP client for award-intelligence's ``GET /api/employee-master``.

That endpoint returns the employee master behind award-intelligence's latest
payroll import, keyed by the **raw** payroll employee ID (every other
award-intelligence route pseudonymises it), so it always requires the API
token. It is read-only: award-intelligence has no employee store ORL could
write to, so the sync reads from it and reports on ORL's side.

Errors:

- network error / timeout / any 5xx -> ``EmployeeMasterUnavailable``
- any other non-2xx (403 bad token, 404 no payroll import yet), or a 2xx body
  that doesn't match ``employee-master-export/v1`` -> ``EmployeeMasterError``
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.settings import Settings, settings

SCHEMA_VERSION = "employee-master-export/v1"


class MasterSource(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    audit_id: str | None = None
    created_at: str | None = None
    source_name: str = ""
    last_date: str = ""

    @classmethod
    def from_json(cls, data: dict) -> MasterSource:
        return cls(
            audit_id=data.get("auditId"),
            created_at=data.get("createdAt"),
            source_name=data.get("sourceName") or "",
            last_date=data.get("lastDate") or "",
        )


class MasterEmployee(BaseModel):
    """One employee-master record, as award-intelligence reports it."""

    model_config = ConfigDict(extra="allow")

    employee_id: str
    employment_type: str = ""
    award_code: str = ""
    state_code: str = ""
    area: str = ""
    employee_rank: str = ""
    employment_start: str = ""
    source_classification: str = ""


class EmployeeMasterExport(BaseModel):
    source: MasterSource
    employees: list[MasterEmployee]


class EmployeeMasterError(Exception):
    """award-intelligence answered, but not with a usable employee master."""


class EmployeeMasterUnavailable(EmployeeMasterError):
    """award-intelligence could not be reached, or failed (5xx)."""


_FIELDS = {
    "employee_id": "employeeId",
    "employment_type": "employmentType",
    "award_code": "awardCode",
    "state_code": "stateCode",
    "area": "area",
    "employee_rank": "employeeRank",
    "employment_start": "employmentStart",
    "source_classification": "sourceClassification",
}


def parse_export(body: object) -> EmployeeMasterExport:
    if not isinstance(body, dict) or body.get("schemaVersion") != SCHEMA_VERSION:
        raise EmployeeMasterError(f"award-intelligence did not return {SCHEMA_VERSION}")
    try:
        employees = [
            MasterEmployee(**{ours: row.get(theirs) or "" for ours, theirs in _FIELDS.items()})
            for row in body.get("employees") or []
        ]
        return EmployeeMasterExport(
            source=MasterSource.from_json(body.get("source") or {}), employees=employees
        )
    except (ValidationError, AttributeError, TypeError) as exc:
        raise EmployeeMasterError(f"malformed employee master: {exc}") from exc


class EmployeeMasterClient:
    def __init__(
        self,
        base_url: str,
        api_token: str | None,
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s, transport=transport
        )

    async def __aenter__(self) -> EmployeeMasterClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._http.aclose()

    async def employee_master(self) -> EmployeeMasterExport:
        try:
            response = await self._http.get("/api/employee-master")
        except httpx.HTTPError as exc:
            raise EmployeeMasterUnavailable(f"award-intelligence unreachable: {exc}") from exc
        if response.status_code >= 500:
            raise EmployeeMasterUnavailable(f"award-intelligence returned {response.status_code}")
        if response.status_code != 200:
            try:
                detail = response.json().get("error") or response.text
            except ValueError:
                detail = response.text
            raise EmployeeMasterError(
                f"award-intelligence returned {response.status_code}: {detail}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise EmployeeMasterError("award-intelligence returned a non-JSON body") from exc
        return parse_export(body)


def client_from_settings(config: Settings = settings) -> EmployeeMasterClient | None:
    """A client for ``AWARD_INTELLIGENCE_URL``, or ``None`` when unset."""
    if not config.award_intelligence_url:
        return None
    return EmployeeMasterClient(
        config.award_intelligence_url,
        config.award_intelligence_api_token,
        timeout_s=config.award_intelligence_timeout_s,
    )
