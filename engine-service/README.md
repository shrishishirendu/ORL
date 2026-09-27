# Award Engine service

A thin, deterministic Node HTTP wrapper around the **award-intelligence** pay engine. It implements
[docs/AWARD_ENGINE_CONTRACT.md](../docs/AWARD_ENGINE_CONTRACT.md) (v1). The background is in
[docs/AWARD_INTEGRATION.md](../docs/AWARD_INTEGRATION.md).

- **Pure wrapper.** It imports engine modules from an award-intelligence checkout at the commit in
  `award-intelligence.pin`. It never re-implements or adjusts award rules or $ figures. Every amount
  comes from `calculateTimesheetResults` (`src/domain/payCalculator.js`), either directly, through the
  engine's `calcRow`, or through the engine's `marginalCost` (`src/engines/coverage.js`).
- **Deterministic.** There are no clocks and no randomness, and inputs are sorted into a stable order.
  The same request gives byte-identical JSON.
- **No AI.** The service makes no LLM calls.
- **No dependencies.** It uses `node:http` only, so there is no `npm install` for the service. The
  engine's own dependencies come from award-intelligence's `node_modules`.

## Run

```sh
cd engine-service
npm start                 # listens on 127.0.0.1:8790
npm test                  # node:test suite (golden, determinism, validation, unresolved, baseline)
```

Requires Node ≥ 22.9 and an award-intelligence checkout that has its `node_modules` installed.

| Env var | Default | Meaning |
|---|---|---|
| `AWARD_INTELLIGENCE_PATH` | `../../award-intelligence` (relative to `engine-service/`) | Engine checkout. It is read only and nothing is written to it. |
| `ENGINE_PORT` | `8790` | Listen port |
| `ENGINE_HOST` | `127.0.0.1` | Listen address. Use `0.0.0.0` in a container. |
| `ALLOW_UNPINNED` | unset | `1` lets the service run and price when the checkout is not at the pinned commit. Health then reports `"unpinned": true`. |
| `ENGINE_MAX_CELLS` | `20000` | Upper bound on cost-matrix cells per request (422 above it) |

## Endpoints

- `GET /engine/health` returns 200 `ok` when the checkout HEAD equals the pin. Otherwise it returns 503
  `degraded`, and `cost-matrix`/`price-roster` also return 503 unless `ALLOW_UNPINNED=1`. The commit is
  read from `.git` directly and git is not invoked. In a container, mount the checkout together with
  its `.git`.
- `GET /engine/awards` lists MA000016 only. Its levels are the library level keys
  `MA000016::securityofficerlevel1` … `level5`. A level is listed only when the verified instrument
  registry resolves it in every rule version.
- `POST /engine/cost-matrix` returns the marginal cost of each (worker, shift) against that worker's
  baseline week.
- `POST /engine/price-roster` returns the exact full-period pay run for a solved roster.

## Design notes

**Building `parsedCache`: the official text path.** The service does not hand-build the engine's
internal cache shapes. Each ORL Worker is rendered as an agreement-profile block in the "Key: value"
format that `agreementParser.parseAgreementDocument` reads (Employee, Employee ID, Award Code,
Employee Level, Employment Type, Base Pay Rate, Ordinary Hours Per Week/Shift, Roster Cycle
Weeks/Start, Legal Employer, Work Type, Source Award Code). The blocks go to the engine's own
`buildParsedCacheFromTexts` together with the bundled MA000016 award library, which is loaded with
`server/awardLibraryFs.js` (the Node loader, not the Vite `import.meta.glob` one). This is the same
path the app uses for an uploaded agreement document. Fields ORL leaves null are omitted, never
defaulted, so the engine reports the gap in its own words.

**Timesheet shifts** take the `timesheetParser.js` shape: `date`/`dateKey`, `weekBucket` (the
engine's `weekBucketFor`, i.e. Monday), `day`, `start`, `finish`, `breakMinutes`, `breakStart`,
break-net `hours`, `location` = `site_code`, and `notes: ''`. Shift facts come only from the
whitelisted `flags` (`first_aid_nominated` → `firstAidNominated`, …). Free-text notes are never sent,
because the engine regex-matches notes (for example "PH").

**Source Award Code.** The engine's employee-master check (securityCalculator.js:326) expects
`MA000016-NSW`, the state-scoped code from the source payroll system. The award code passed to the
engine is still `MA000016`, which is what routes to the security calculator and the instrument
registry. ORL's Worker is the employee master, so the service sends
`Source Award Code: <award_code>-<jurisdiction>`. Outside NSW the engine keeps reporting the gap.

**Chronological order.** The MA000016 calculator assigns weekly overtime to the latest segments *in
input order*. The service always sends shifts sorted by date and start time. The engine's
`marginalCost` appends the new shift last. When the new shift is also chronologically last (always
the case for an empty baseline), `marginalCost` is used verbatim (`pricing_method: "marginalCost"`).
Otherwise the service applies the same formula, `calc(existing ∪ new) − calc(existing)`, with both
calls through the engine's `calcRow` on chronological input (`pricing_method:
"chronological-difference"`).

**Cost-matrix rows.**
- A cell is `unresolved` when the baseline or the baseline-plus-shift calculation is unresolved.
  `reasons` holds the engine issues, and baseline issues are prefixed with `baseline:`.
- A shift that is already in the baseline is priced against the rest of the week.
- `eligible=false` on a resolved cell means the engine's own "work period exceeds the 14 hour
  maximum" warning appeared because of this shift.
- `release_blocking_gaps` and `warnings` are passed through from the engine.

**Public holidays.** The MA000016 segment engine applies public-holiday rates only when a shift is
flagged (`flags.public_holiday`). It never consults the jurisdiction calendar. The service does not
correct this. It adds a warning when the engine's own calendar marks a priced segment's date as a
holiday.

**Additive response fields** (beyond contract v1):
- cost-matrix rows: `warnings`, `release_blocking_gaps`, `instrument_versions`, `pricing_method`
- price-roster: `total_hours`, `release_blocking_gaps` and `instrument_versions` per worker, plus
  top-level `warnings`, `public_holidays_applied` and `rate_validity`
- awards: `engine_classification_id`, `minimum_hourly_by_version` and `instrument_version_ranges`

Shift also accepts an optional `break_start` ("HH:MM"). The engine needs it to place an unpaid break
that could fall in differently rated segments, such as an overnight shift. Without it the cell is
`unresolved`.

## Layout

```
engine-service/
  award-intelligence.pin   full commit hash the service is pinned to
  src/server.js            node:http routes, pin gate, 422 handling
  src/engine.js            engine loading, ORL → engine mapping, engine → contract mapping
  src/validate.js          request validation (malformed → 422)
  test/*.test.js           node:test suites
  Dockerfile
```
