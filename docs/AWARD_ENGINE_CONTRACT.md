# Award Engine service: API contract (v1)

The HTTP contract between ORL (Python) and the **Award Engine service** (Node, `engine-service/`),
which wraps the award-intelligence pay engine **unchanged**. Background:
[AWARD_INTEGRATION.md](AWARD_INTEGRATION.md). Scope for the demo: **MA000016 Security Services
Industry Award 2020, NSW** (owner decision, 2026-09-25).

## Ground rules

- **Pure wrapper.** The service imports engine modules from award-intelligence at the **pinned
  commit** in `engine-service/award-intelligence.pin`. It never re-implements, patches or
  post-adjusts award rules or $ figures. Every $ value in a response comes straight from
  `calculateTimesheetResults` (or the engine's `marginalCost`, which calls it).
- **Deterministic.** Same request → byte-identical response (no clocks, no randomness, stable ordering).
- **No AI.** The service makes no LLM calls.
- **Honest gaps.** When the engine can't price something, it returns `status: "unresolved"` with the
  engine's own issue strings and `pay_cost: null`. Never a guessed number.
- Money is a JSON number rounded to 2 dp, in AUD. Dates are `YYYY-MM-DD`. Times are `HH:MM` 24h.
  An end time ≤ the start time means an overnight shift.

## Shared shapes

```jsonc
// Context: employer-level facts the engine needs (release-gap fields for MA000016)
"context": { "jurisdiction": "NSW", "legal_employer": "string", "work_type": "string|null" }

// Worker: ORL Worker + award fields
{ "worker_id": 12, "name": "Sophie Chen",
  "award_code": "MA000016",
  "classification_level": "string",        // must be a level key from GET /engine/awards
  "employment_type": "full_time|part_time|casual",
  "over_award_rate": 31.50,                 // nullable; engine uses max(this, award minimum)
  "ordinary_hours_per_week": 30,            // required for part_time, else null
  "agreed_ordinary_hours_per_shift": 7.5,   // required for part_time, else null
  "roster_cycle_weeks": 1, "roster_cycle_start": null }

// Shift: an ORL Shift as the engine sees it
{ "shift_id": 41, "date": "2026-10-10", "start_time": "18:00", "end_time": "06:00",
  "break_minutes": 30, "site_code": "STH-01",
  "flags": { "first_aid_nominated": false, "firearm_required": false } }  // optional engine shift flags
```

## Endpoints

### `GET /engine/health`
`{ "status": "ok"|"degraded", "pinned_commit": "…", "engine_commit": "…", "commit_matches": true }`.
`degraded` + HTTP 503 when the award-intelligence checkout is not at the pinned commit (unless
`ALLOW_UNPINNED=1`, which must be reported in the response as `"unpinned": true`).

### `GET /engine/awards`
`{ "engine_commit", "awards": [ { "code": "MA000016", "name": "…", "levels": [ { "key", "name", "minimum_hourly" } ], "instrument_versions": [ "…" ] } ] }`
For the demo, only awards whose calculation path is fully supported are listed (MA000016).

### `POST /engine/cost-matrix`
Builds `AwardCostMatrix` rows. Each cell is the **marginal cost** of adding the shift to that
worker's baseline week (see AWARD_INTEGRATION.md "cost depends on the whole week").

Request:
```jsonc
{ "award_code": "MA000016", "context": {…}, "workers": [Worker], "shifts": [Shift],
  "baseline": { "<worker_id>": [Shift, …] },   // optional; missing = empty week
  "pairs": [[worker_id, shift_id], …] }         // optional; default = every worker × every shift
```
Response:
```jsonc
{ "engine_commit": "…", "instrument_versions": ["…"],
  "rows": [ { "worker_id": 12, "shift_id": 41, "day": "2026-10-10",
              "status": "resolved|unresolved",
              "eligible": true,                 // false when unresolved or the shift breaches an engine hard limit
              "pay_cost": 412.37,               // null when unresolved
              "min_hours": null, "max_hours": null,
              "reasons": ["…engine issue strings…"],
              "driving_items": [ { "type": "Night shift penalty", "amount": 55.10, "clause": "cl. …" } ] } ],
  "rate_validity": [ … engine rateValidity passthrough … ] }
```
`min_hours` / `max_hours` stay `null` in v1. ORL's solver treats them as *period-level* worker
bounds, and the engine exposes no period-level figure. (Shift-level limits such as maximum shift
length are applied as `eligible=false` with a reason.) Rows are sorted by (worker_id, shift_id).

### `POST /engine/price-roster`
Exact, full-period pay for a solved roster. This is the **reconciliation** figure ORL reports as the
roster's true cost.

Request: `{ "award_code", "context", "period_start", "period_end", "workers": [Worker], "assignments": [ { "worker_id", "shifts": [Shift] } ] }`

Response:
```jsonc
{ "engine_commit", "instrument_versions",
  "total_cost": 8123.45,                       // null if any worker is unresolved
  "workers": [ { "worker_id", "status": "resolved|unresolved", "total_pay": 612.30,
                 "ordinary_pay": 540.00,
                 "items": [ { "type", "category", "amount", "clause", "detail" } ],
                 "issues": [], "warnings": [] } ],
  "unresolved_worker_ids": [] }
```

### Errors
HTTP 422 `{ "error": "validation", "details": ["…"] }` for malformed requests (unknown award, unknown
level key, bad time format, part_time without its required hours). Engine-level inability to price
is **not** an error: it comes back as `status: "unresolved"`.

## ORL side

- `Worker` gains: `award_code`, `classification_level`, `employment_type`, `over_award_rate`,
  `ordinary_hours_per_week`, `agreed_ordinary_hours_per_shift` (all nullable).
- Settings: `AWARD_ENGINE_URL`; the employer context (`AWARD_JURISDICTION=NSW`, `AWARD_LEGAL_EMPLOYER`,
  `AWARD_WORK_TYPE`).
- `AwardEngineClient` (httpx) populates `AwardCostMatrix` with `is_placeholder=false`. If the engine is
  unavailable, ORL keeps placeholder rows and **flags** that costs are placeholders. It never
  silently mixes the two in one solve.
- After a Tier 1 solve, ORL calls `price-roster` and stores the engine's exact `total_cost` alongside
  the solver estimate.

## v1.1 amendments (as implemented in `engine-service/`, 2026-09-25)

These are additive: no v1 field changed meaning. See `engine-service/README.md` for full field lists.

- **Level keys** are the engine's library keys, e.g. `MA000016::securityofficerlevel1` … `level5`.
- **Shift gains optional `break_start` (`HH:MM`).** Without it, a shift whose unpaid break could fall in
  differently-paid time (overnight, or across 06:00/18:00) comes back `unresolved`. The engine does
  not guess where the break was. Shift `flags` accept only the engine's fixed flag list
  (unknown flag → 422). Free-text notes are never forwarded.
- **Chronological pricing.** The engine is input-order dependent (see "Engine findings" in
  AWARD_INTEGRATION.md). The service always feeds shifts in time order. Cells are priced with the
  engine's `marginalCost` when the new shift is chronologically last, otherwise with the same
  difference formula via the engine's `calcRow`. Each row reports `pricing_method`:
  `marginalCost` | `chronological-difference`.
- **Extra response fields:** cost-matrix rows add `warnings`, `release_blocking_gaps`, `pricing_method`.
  price-roster adds `warnings`, `public_holidays_applied`, `rate_validity`.
- **Release-blocking gaps don't block pricing.** They are reported alongside a resolved cost, and ORL
  shows them rather than hiding the cost (policy to confirm with the engine owner).
- **Null vs invalid.** A null award field on a worker → that worker's cells are `unresolved` with the
  engine's reason. A non-null invalid value (unknown level, bad type) → 422 for the request.
- **Source award code.** The service sends the engine `Source Award Code: <award_code>-<jurisdiction>`
  (e.g. `MA000016-NSW`), treating ORL's Worker as the employee master. **Engine owner to confirm.**
- **Edge cases:**
  - A baseline shift with the same `shift_id` as the cell is excluded when pricing that cell.
  - Workers with no shifts are omitted from price-roster.
  - A pin mismatch returns 503 from both pricing endpoints.
