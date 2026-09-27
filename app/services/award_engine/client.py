"""Typed HTTP client for the Award Engine service (``engine-service/``).

Mirrors ``docs/AWARD_ENGINE_CONTRACT.md`` (v1 + the v1.1 amendments) one-to-one: every request and
response shape below is the contract's JSON, as Pydantic models, and every
endpoint is called exactly as the contract specifies. Nothing here computes,
adjusts or rounds a $ figure -- money comes back as the engine's own JSON
number and is parsed straight into ``Decimal`` from the raw response bytes
(``model_validate_json``), so no float round-trip can change a cent.

Error mapping (the contract's "Errors" section):

- network error / timeout / any 5xx -> ``AwardEngineUnavailable``
- HTTP 422 ``{"error": "validation", "details": [...]}`` ->
  ``AwardEngineRejected`` (ORL sent something the engine considers
  malformed -- unknown level key, part_time without hours, ...)
- any other non-2xx, or a 2xx body that doesn't match the contract ->
  ``AwardEngineError``

Engine-level inability to price is **not** an error: it arrives as
``status: "unresolved"`` inside a normal 200 response, and callers handle it
as data (see ``matrix.py`` / ``reconcile.py``).

``GET /engine/health`` is the one exception to "non-2xx raises": a pin
mismatch is a 503 whose body is still a well-formed health document
(``status: "degraded"``), so ``health()`` parses and returns it.

**Tolerant responses.** The v1.1 additive fields ORL uses are declared
explicitly (``warnings``, ``release_blocking_gaps``, ``pricing_method``,
``public_holidays_applied``, ...); every response model also has
``extra="allow"`` so a further additive engine field never breaks parsing.
"""

from __future__ import annotations

from datetime import date as date_
from decimal import Decimal
from typing import Annotated, Any, Literal, TypeVar

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SerializerFunctionWrapHandler,
    ValidationError,
    model_serializer,
)

from app.core.settings import Settings, settings

# The contract sends money/hours as JSON *numbers*; Pydantic's JSON mode
# would render a ``Decimal`` as a string. Request-side decimals are
# serialised as numbers instead (``float(Decimal("31.50"))`` -> ``31.5``,
# exact for the 2-dp values ORL stores).
JsonNumber = Annotated[
    Decimal, PlainSerializer(lambda d: float(d), return_type=float, when_used="json")
]

# --- shared shapes -----------------------------------------------------------


class EngineContext(BaseModel):
    """Employer-level facts (contract: "Context")."""

    jurisdiction: str
    legal_employer: str | None = None
    work_type: str | None = None


class EngineWorker(BaseModel):
    """An ORL Worker plus its award fields (contract: "Worker")."""

    worker_id: int
    name: str
    award_code: str
    classification_level: str
    employment_type: Literal["full_time", "part_time", "casual"]
    over_award_rate: JsonNumber | None = None
    ordinary_hours_per_week: JsonNumber | None = None
    agreed_ordinary_hours_per_shift: JsonNumber | None = None
    roster_cycle_weeks: int = 1
    roster_cycle_start: date_ | None = None


class EngineShift(BaseModel):
    """An ORL Shift as the engine sees it (contract: "Shift").

    ``start_time``/``end_time`` are ``HH:MM`` strings; an end at or before
    the start is an overnight shift (the engine's rule, and the same one
    Tier 1's ``_shift_window`` uses). The contract's optional ``flags``
    object is not sent -- ORL has no data for it and must not assert
    ``false`` for facts it doesn't know. ``break_start`` (v1.1) is omitted
    from the JSON when ``None`` rather than sent as ``null``.
    """

    shift_id: int
    date: date_
    start_time: str
    end_time: str
    break_minutes: int
    break_start: str | None = None
    site_code: str

    @model_serializer(mode="wrap")
    def _omit_unknown_break_start(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if data.get("break_start") is None:
            data.pop("break_start", None)
        return data


# --- POST /engine/cost-matrix -------------------------------------------------


class CostMatrixRequest(BaseModel):
    award_code: str
    context: EngineContext
    workers: list[EngineWorker]
    shifts: list[EngineShift]
    # JSON object keys are strings; the contract spells them "<worker_id>".
    baseline: dict[str, list[EngineShift]] = Field(default_factory=dict)
    pairs: list[tuple[int, int]] | None = None


class _Tolerant(BaseModel):
    """Base for response models: unknown (additive) engine fields are kept, not rejected."""

    model_config = ConfigDict(extra="allow")


class DrivingItem(_Tolerant):
    type: str
    amount: Decimal | None = None
    clause: str | None = None


class CostMatrixRow(_Tolerant):
    worker_id: int
    shift_id: int
    day: date_
    status: Literal["resolved", "unresolved"]
    eligible: bool
    pay_cost: Decimal | None = None
    min_hours: Decimal | None = None
    max_hours: Decimal | None = None
    reasons: list[str] = Field(default_factory=list)
    driving_items: list[DrivingItem] = Field(default_factory=list)
    # v1.1 additive fields.
    warnings: list[Any] = Field(default_factory=list)
    release_blocking_gaps: list[Any] = Field(default_factory=list)
    pricing_method: str | None = None


class CostMatrixResponse(_Tolerant):
    engine_commit: str
    instrument_versions: list[str] = Field(default_factory=list)
    rows: list[CostMatrixRow]
    rate_validity: Any = None


# --- POST /engine/price-roster ------------------------------------------------


class RosterWorkerAssignments(BaseModel):
    worker_id: int
    shifts: list[EngineShift]


class PriceRosterRequest(BaseModel):
    award_code: str
    context: EngineContext
    period_start: date_
    period_end: date_
    workers: list[EngineWorker]
    assignments: list[RosterWorkerAssignments]


class PayItem(_Tolerant):
    type: str
    category: str | None = None
    amount: Decimal | None = None
    clause: str | None = None
    detail: str | None = None


class PricedWorker(_Tolerant):
    worker_id: int
    status: Literal["resolved", "unresolved"]
    total_pay: Decimal | None = None
    ordinary_pay: Decimal | None = None
    items: list[PayItem] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    warnings: list[Any] = Field(default_factory=list)
    # v1.1 additive fields.
    total_hours: Decimal | None = None
    release_blocking_gaps: list[Any] = Field(default_factory=list)


class PriceRosterResponse(_Tolerant):
    engine_commit: str
    instrument_versions: list[str] = Field(default_factory=list)
    total_cost: Decimal | None = None
    workers: list[PricedWorker] = Field(default_factory=list)
    unresolved_worker_ids: list[int] = Field(default_factory=list)
    # v1.1 additive fields.
    warnings: list[Any] = Field(default_factory=list)
    public_holidays_applied: Any = None
    rate_validity: Any = None


# --- GET /engine/health, GET /engine/awards -----------------------------------


class EngineHealth(_Tolerant):
    status: Literal["ok", "degraded"]
    pinned_commit: str | None = None
    engine_commit: str | None = None
    commit_matches: bool | None = None
    unpinned: bool | None = None


class AwardLevel(_Tolerant):
    key: str
    name: str | None = None
    minimum_hourly: Decimal | None = None


class AwardInfo(_Tolerant):
    code: str
    name: str | None = None
    levels: list[AwardLevel] = Field(default_factory=list)
    instrument_versions: list[str] = Field(default_factory=list)


class AwardsResponse(_Tolerant):
    engine_commit: str
    awards: list[AwardInfo]


# --- errors -------------------------------------------------------------------


class AwardEngineError(Exception):
    """Base class: the engine call did not produce a usable contract response."""


class AwardEngineUnavailable(AwardEngineError):
    """Network error, timeout, or 5xx -- the engine could not be reached/used."""


class AwardEngineRejected(AwardEngineError):
    """HTTP 422: the engine rejected ORL's request as malformed."""

    def __init__(self, details: list[str]) -> None:
        self.details = details
        super().__init__("award engine rejected the request: " + "; ".join(details))


# --- client -------------------------------------------------------------------

_ResponseT = TypeVar("_ResponseT", bound=BaseModel)


class AwardEngineClient:
    """Async client for the four contract endpoints.

    ``transport`` exists for tests (``httpx.MockTransport``); production
    code builds one via ``client_from_settings``. Use as an async context
    manager, or call ``aclose()``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport
        )

    async def __aenter__(self) -> AwardEngineClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        try:
            return await self._http.request(method, path, json=json_body)
        except httpx.HTTPError as exc:  # connect errors, timeouts, protocol errors
            raise AwardEngineUnavailable(f"{method} {path}: {exc!r}") from exc

    @staticmethod
    def _parse(response: httpx.Response, model: type[_ResponseT]) -> _ResponseT:
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise AwardEngineError(
                f"{response.request.method} {response.request.url.path}: response does not "
                f"match the contract: {exc}"
            ) from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        where = f"{response.request.method} {response.request.url.path}"
        if response.status_code == 422:
            try:
                details = [str(d) for d in response.json().get("details", [])]
            except (ValueError, AttributeError):
                details = [response.text]
            raise AwardEngineRejected(details or [f"{where}: HTTP 422"])
        if response.status_code >= 500:
            raise AwardEngineUnavailable(f"{where}: HTTP {response.status_code}")
        raise AwardEngineError(f"{where}: HTTP {response.status_code}")

    async def _post(
        self,
        path: str,
        body: BaseModel,
        model: type[_ResponseT],
        exclude: set[str] | None = None,
    ) -> _ResponseT:
        response = await self._request("POST", path, body.model_dump(mode="json", exclude=exclude))
        self._raise_for_status(response)
        return self._parse(response, model)

    async def health(self) -> tuple[int, EngineHealth]:
        """``GET /engine/health`` -> (HTTP status, parsed body).

        A 503 with a ``degraded`` body is returned, not raised (see module
        docstring); any other failure raises as usual.
        """
        response = await self._request("GET", "/engine/health")
        if response.status_code == 503:
            try:
                return response.status_code, self._parse(response, EngineHealth)
            except AwardEngineError:
                pass  # not a health document -- treat as an ordinary 503 below
        self._raise_for_status(response)
        return response.status_code, self._parse(response, EngineHealth)

    async def awards(self) -> AwardsResponse:
        response = await self._request("GET", "/engine/awards")
        self._raise_for_status(response)
        return self._parse(response, AwardsResponse)

    async def cost_matrix(self, request: CostMatrixRequest) -> CostMatrixResponse:
        # `pairs` is optional in the contract ("default = every worker x
        # every shift"): omit the key rather than send an explicit null.
        exclude = {"pairs"} if request.pairs is None else None
        return await self._post("/engine/cost-matrix", request, CostMatrixResponse, exclude)

    async def price_roster(self, request: PriceRosterRequest) -> PriceRosterResponse:
        return await self._post("/engine/price-roster", request, PriceRosterResponse)


def client_from_settings(config: Settings = settings) -> AwardEngineClient | None:
    """An ``AwardEngineClient`` for ``AWARD_ENGINE_URL``, or ``None`` when
    the engine is not configured (the default -- see ``Settings``).
    """
    if not config.award_engine_url:
        return None
    return AwardEngineClient(config.award_engine_url, timeout_s=config.award_engine_timeout_s)


def context_from_settings(config: Settings = settings) -> EngineContext:
    return EngineContext(
        jurisdiction=config.award_jurisdiction,
        legal_employer=config.award_legal_employer,
        work_type=config.award_work_type,
    )
