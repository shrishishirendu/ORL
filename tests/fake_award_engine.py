"""An in-process stand-in for the Award Engine service, for tests.

``FakeAwardEngine.client()`` returns a real ``AwardEngineClient`` whose
``httpx`` transport is a ``MockTransport`` answering the contract's four
endpoints. The figures it returns are arbitrary test fixtures (a fixed
number per worker/shift), **not** award calculations -- the point is to
check what ORL does with an engine answer, not to model the award.

Every request body is recorded in ``requests`` as ``(path, parsed_json)``
so tests can assert on exactly what ORL sent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.services.award_engine.client import AwardEngineClient

ENGINE_COMMIT = "fakecommit0000000000000000000000000000000"
LEVEL_KEYS = [f"MA000016::securityofficerlevel{n}" for n in range(1, 6)]


def fake_cell_cost(worker_id: int, shift_id: int) -> float:
    """The fixture "marginal cost" the fake engine returns for a cell."""
    return round(100 + worker_id + shift_id / 100, 2)


class FakeAwardEngine:
    def __init__(
        self,
        *,
        unresolved_cells: set[tuple[int, int]] | None = None,
        ineligible_cells: set[tuple[int, int]] | None = None,
        release_gap_cells: set[tuple[int, int]] | None = None,
        unresolved_workers: set[int] | None = None,
        fail_status: int | None = None,
        fail_paths: tuple[str, ...] = ("/engine/cost-matrix", "/engine/price-roster"),
        roster_total: float | None = None,
    ) -> None:
        self.unresolved_cells = unresolved_cells or set()
        self.ineligible_cells = ineligible_cells or set()
        self.release_gap_cells = release_gap_cells or set()
        self.unresolved_workers = unresolved_workers or set()
        self.fail_status = fail_status
        self.fail_paths = fail_paths
        self.roster_total = roster_total
        self.requests: list[tuple[str, Any]] = []

    def client(self) -> AwardEngineClient:
        return AwardEngineClient("http://engine.test", transport=httpx.MockTransport(self.handler))

    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]

    def bodies(self, path: str) -> list[Any]:
        return [body for p, body in self.requests if p == path]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((path, body))
        if self.fail_status is not None and path in self.fail_paths:
            if self.fail_status == 422:
                return httpx.Response(422, json={"error": "validation", "details": ["bad input"]})
            return httpx.Response(self.fail_status, json={"error": "boom"})
        if path == "/engine/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "pinned_commit": ENGINE_COMMIT,
                    "engine_commit": ENGINE_COMMIT,
                    "commit_matches": True,
                },
            )
        if path == "/engine/awards":
            return httpx.Response(
                200,
                json={
                    "engine_commit": ENGINE_COMMIT,
                    "awards": [
                        {
                            "code": "MA000016",
                            "name": "Security Services Industry Award 2020",
                            "levels": [{"key": k, "name": k} for k in LEVEL_KEYS],
                        }
                    ],
                },
            )
        if path == "/engine/cost-matrix":
            return httpx.Response(200, json=self._cost_matrix(body))
        if path == "/engine/price-roster":
            return httpx.Response(200, json=self._price_roster(body))
        return httpx.Response(404, json={"error": "not_found"})

    def _cost_matrix(self, body: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for worker in body["workers"]:
            for shift in body["shifts"]:
                key = (worker["worker_id"], shift["shift_id"])
                unresolved = key in self.unresolved_cells
                rows.append(
                    {
                        "worker_id": key[0],
                        "shift_id": key[1],
                        "day": shift["date"],
                        "status": "unresolved" if unresolved else "resolved",
                        "eligible": not unresolved and key not in self.ineligible_cells,
                        "pay_cost": None if unresolved else fake_cell_cost(*key),
                        "min_hours": None,
                        "max_hours": None,
                        "reasons": ["Legal employer is missing"] if unresolved else [],
                        "driving_items": [],
                        "warnings": [],
                        "release_blocking_gaps": (
                            ["Roster cycle not configured"] if key in self.release_gap_cells else []
                        ),
                        "pricing_method": "marginalCost",
                        "some_future_field": {"ignored": True},
                    }
                )
        rows.sort(key=lambda r: (r["worker_id"], r["shift_id"]))
        return {
            "engine_commit": ENGINE_COMMIT,
            "instrument_versions": ["MA000016@2026-07-01"],
            "rows": rows,
            "rate_validity": [],
        }

    def _price_roster(self, body: dict[str, Any]) -> dict[str, Any]:
        workers = []
        for assignment in body["assignments"]:
            worker_id = assignment["worker_id"]
            unresolved = worker_id in self.unresolved_workers
            pay = sum(fake_cell_cost(worker_id, s["shift_id"]) for s in assignment["shifts"])
            workers.append(
                {
                    "worker_id": worker_id,
                    "status": "unresolved" if unresolved else "resolved",
                    "total_pay": None if unresolved else round(pay, 2),
                    "ordinary_pay": None,
                    "items": [],
                    "issues": ["Legal employer is missing"] if unresolved else [],
                    "warnings": [],
                    "release_blocking_gaps": [],
                }
            )
        unresolved_ids = sorted(w["worker_id"] for w in workers if w["status"] == "unresolved")
        total = None
        if not unresolved_ids:
            total = (
                self.roster_total
                if self.roster_total is not None
                else round(sum(w["total_pay"] for w in workers), 2)
            )
        return {
            "engine_commit": ENGINE_COMMIT,
            "instrument_versions": ["MA000016@2026-07-01"],
            "total_cost": total,
            "workers": workers,
            "unresolved_worker_ids": unresolved_ids,
            "warnings": [],
            "public_holidays_applied": [],
            "rate_validity": [],
        }
