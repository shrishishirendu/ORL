# ORL Architecture

ORL is organized as three optimization tiers sitting on top of one external boundary
(the Award Interpretation Engine) and a set of supporting services (Travel Matrix,
Postgres, FastAPI + async task queue). This document is the durable reference for that
design; read it before adding solver logic, models, or APIs.

## The AwardCostMatrix boundary

The Award Interpretation Engine is an **external system, not part of this repo**. It is
responsible for all pay/award-interpretation logic and hands ORL a single artifact:

- **`AwardCostMatrix`** — worker × day × shift → `{ pay_cost: $, eligible: bool, min_hours,
  max_hours }`.

ORL only ever **consumes** `AwardCostMatrix`. It is treated as a given input to Tier 1;
ORL never computes pay itself, never re-derives award rules, and never mutates the matrix.
Any correction to pay or eligibility logic belongs upstream, in the Award Interpretation
Engine — not in ORL.

**Status: planned integration, not a permanent external system.** The Award Interpretation
Engine and ORL are meant to become one integrated product; "external system" above describes
today's repo boundary, not the target architecture. A separate Award Interpretation repo is
expected to be shared and folded into this system. When that happens, `AwardCostMatrix` stays
the seam: its shape (`worker × day × shift → { pay_cost, eligible, min_hours, max_hours }`) and
Tier 1's consume-only relationship to it should not change, whatever changes underneath —
whether the Award Interpretation Engine ends up populating this table via an in-process call,
an adapter/ETL step, or something else. That keeps the integration a matter of *how the table
gets filled* rather than a rewrite of Tier 1's solver contract. Revisit this note once the
Award Interpretation repo is available and its own architecture is known.

## Tier 1 — Rostering (batch)

- **Tempo**: batch, weekly or fortnightly.
- **Engine**: OR-Tools CP-SAT.
- **Inputs**: `AwardCostMatrix`, worker skills/geo data (including each worker's home
  region — see "Home location as depot" below), shift requirements.
- **Output**: `Roster` — a worker → day → shift assignment.
- **Objective**: minimize total **dollar cost** (via `AwardCostMatrix.pay_cost`), not raw
  hours worked. Eligibility and min/max hour rules from `AwardCostMatrix`, plus the
  worker-home-region-vs-shift-site-region eligibility filter, constrain feasible
  assignments — none of these add a cost term.

Tier 1 is the slow, optimization-heavy tier: it runs infrequently and produces the roster
that Tiers 2 and 3 then operate within for the rest of the period.

## Tier 2 — Dispatch / Routing (per shift)

- **Tempo**: per shift — much faster cadence than Tier 1, run as each shift is prepared.
- **Engine**: OR-Tools Routing (VRPTW — vehicle routing with time windows).
- **Inputs**: the shift's worker assignments from the `Roster` (Tier 1's output), the job
  list for that shift, and the cached Travel Matrix.
- **Output**: `Route` — an ordered stop sequence with arrival times per worker, as a
  **closed round trip** starting and ending at the worker's home site (see "Home
  location as depot" below).
- **Scope**: only **multi-stop roles** go through VRPTW routing. **Single-site roles skip
  this tier entirely** via a simple "Site Assignment" path — there is nothing to sequence
  when a worker has one site for the shift.

Tier 2 takes the Roster as fixed input: it does not reassign which worker is on shift, only
how a multi-stop worker's jobs are ordered and timed within that shift.

## Home location as depot (Tier 1 and Tier 2)

A mobile workforce's whole point is that workers travel from home: each
worker's day starts at their **home location** and, after their last job,
they return there. Every `Worker` has a mandatory `home_site_id` (FK to
`Site`) capturing this. It feeds both tiers, in different ways:

- **Tier 1 (rostering)**: home location is a **hard eligibility filter**,
  not a cost term. A worker can only be rostered onto a shift when their
  home region is compatible with the shift's site region (the solver checks
  `Worker.region` -- normally resolved from `home_site_id`'s `Site.region`
  -- against the shift's `Site.region`). Tier 1's objective remains
  **pay-cost-only**: no distance/travel term is added for this. A worker
  whose home region doesn't match simply isn't a candidate for that shift,
  exactly like a worker missing the required skill or marked ineligible in
  the `AwardCostMatrix`.
- **Tier 2 (dispatch/routing)**: home location is the **depot for a closed
  round trip**. The VRPTW route starts at the worker's home site and
  returns to it after the last job -- both legs (home -> first job, last
  job -> home) are real, travel-time-charged legs, not a free "open route"
  end. The cached Travel Matrix must therefore cover home <-> every job
  site used in a shift's solve, in both directions, the same way it must
  cover every job-site pair.

## Tier 3 — Intra-day Re-optimization (event-driven)

- **Tempo**: event-driven, on the order of seconds — this tier reacts live, during the
  shift, not on a schedule.
- **Triggers**: a job is cancelled, a worker calls in sick, a visit overruns its allotted
  time.
- **Behavior**: performs a **scoped re-solve back into Tier 2** — it re-runs routing for
  the affected shift/worker(s) only, not the whole day or roster.
- **Escalation path**: if a scoped Tier 2 re-solve cannot produce a feasible route (the
  shift itself becomes infeasible — e.g. no combination of remaining workers/routes can
  cover the required jobs), Tier 3 escalates **up to Tier 1**, triggering a rostering
  re-solve rather than forcing an infeasible route.

This is the feedback loop that closes the system: Tier 3 watches for real-world disruption
events, resolves what it can quickly at the routing level, and only pulls the slow,
batch-level rostering tier back in when the disruption can't be absorbed at the routing
level.

## Supporting components

- **Travel Matrix** — a cached travel-time matrix between sites, feeding Tier 2 (and Tier
  3's scoped re-solves) so that VRPTW routing doesn't recompute travel times from scratch
  on every solve.
- **Postgres** — persists the output of all three tiers: rosters, routes, and the
  re-optimization history, plus supporting reference data.
- **FastAPI service layer + async task queue** — exposes rostering, dispatch, and event
  handling as APIs. Batch Tier 1 solves and per-shift Tier 2 solves are dispatched onto the
  async task queue rather than run inline on the request; Tier 3's event-driven re-solves
  are triggered by incoming events (job cancelled / worker sick / visit overran) reaching
  the API layer.

## API surface and job polling

The FastAPI service layer (`app/api/`) exposes three `POST` endpoints, one per tier, all
following the same enqueue-then-poll shape: the `POST` returns immediately with a
`{"job_id": ...}` (`JobEnqueuedResponse`), and a matching `GET .../jobs/{job_id}` reports
that job's `arq` status/result (`JobStatusResponse`: `status` is one of arq's own
`deferred`/`queued`/`in_progress`/`complete`/`not_found` values, with `success`/`result`
populated only once the job has finished). Nothing solves inline on the request — every
`POST` only enqueues a job onto the `arq` queue backed by Redis; an `arq` worker process
(`arq app.workers.tasks.WorkerSettings`) is what actually calls into the Tier 1/2/3 service
layer and persists the result.

- **`POST /rostering/solve`** / **`GET /rostering/jobs/{job_id}`** — Tier 1. Takes
  `period_start`/`period_end` and enqueues `solve_roster_task`, which runs
  `solve_and_persist_roster` and persists a `Roster`.
- **`POST /dispatch/solve`** / **`GET /dispatch/jobs/{job_id}`** — Tier 2. Takes a
  `roster_assignment_id` and enqueues `solve_route_task`, which runs
  `solve_and_persist_route`. A single-site `RosterAssignment` still resolves cleanly through
  this same endpoint — the service bypasses VRPTW and persists a `SiteAssignment` instead of
  a `Route` (see `DispatchOutcome.mode`).
- **`POST /events`** / **`GET /events/jobs/{job_id}`** — Tier 3. Takes one of the three event
  shapes discriminated on `event_type` (`job_cancelled` / `worker_sick` / `visit_overran`;
  malformed payloads for a given type — e.g. `visit_overran` missing `current_time` — are
  rejected as a 422 by request validation before ever reaching the queue) and enqueues
  `handle_reoptimization_event_task`, which runs `handle_and_persist_event`.
- **`GET /health`** — basic liveness check, not part of the job-polling pattern above.

**Escalation chaining.** When a Tier 3 event's outcome is `ESCALATED_TO_TIER1` (the scoped
Tier 2 re-solve came back infeasible — see "Tier 3" above), `handle_reoptimization_event_task`
itself enqueues a follow-up `solve_roster_task` onto the same queue, using the escalating
shift's own `date` as both `period_start` and `period_end` (the smallest period guaranteed to
re-cover the shift that triggered the escalation). This is the one path that triggers a Tier 1
re-solve automatically rather than via an explicit `POST /rostering/solve` call; the
`escalation_roster_job_id` returned alongside the event's own result is that chained job's id,
pollable the same way at `GET /rostering/jobs/{escalation_roster_job_id}`.

## Summary of tempo distinctions

| Tier | Tempo | Engine | Re-solve scope |
|---|---|---|---|
| 1 — Rostering | Batch, weekly/fortnightly | CP-SAT | Whole roster |
| 2 — Dispatch/Routing | Per shift | Routing (VRPTW) | One shift's routes |
| 3 — Re-optimization | Event-driven, seconds | Routing (VRPTW), scoped | Affected shift/worker(s); escalates to Tier 1 only on infeasibility |
