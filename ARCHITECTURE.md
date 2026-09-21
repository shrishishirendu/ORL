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

## Tier 1 — Rostering (batch)

- **Tempo**: batch, weekly or fortnightly.
- **Engine**: OR-Tools CP-SAT.
- **Inputs**: `AwardCostMatrix`, worker skills/geo data, shift requirements.
- **Output**: `Roster` — a worker → day → shift assignment.
- **Objective**: minimize total **dollar cost** (via `AwardCostMatrix.pay_cost`), not raw
  hours worked. Eligibility and min/max hour rules from `AwardCostMatrix` constrain
  feasible assignments.

Tier 1 is the slow, optimization-heavy tier: it runs infrequently and produces the roster
that Tiers 2 and 3 then operate within for the rest of the period.

## Tier 2 — Dispatch / Routing (per shift)

- **Tempo**: per shift — much faster cadence than Tier 1, run as each shift is prepared.
- **Engine**: OR-Tools Routing (VRPTW — vehicle routing with time windows).
- **Inputs**: the shift's worker assignments from the `Roster` (Tier 1's output), the job
  list for that shift, and the cached Travel Matrix.
- **Output**: `Route` — an ordered stop sequence with arrival times per worker.
- **Scope**: only **multi-stop roles** go through VRPTW routing. **Single-site roles skip
  this tier entirely** via a simple "Site Assignment" path — there is nothing to sequence
  when a worker has one site for the shift.

Tier 2 takes the Roster as fixed input: it does not reassign which worker is on shift, only
how a multi-stop worker's jobs are ordered and timed within that shift.

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

## Summary of tempo distinctions

| Tier | Tempo | Engine | Re-solve scope |
|---|---|---|---|
| 1 — Rostering | Batch, weekly/fortnightly | CP-SAT | Whole roster |
| 2 — Dispatch/Routing | Per shift | Routing (VRPTW) | One shift's routes |
| 3 — Re-optimization | Event-driven, seconds | Routing (VRPTW), scoped | Affected shift/worker(s); escalates to Tier 1 only on infeasibility |
