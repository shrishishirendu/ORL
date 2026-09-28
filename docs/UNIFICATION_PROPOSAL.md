# Unification proposal: one product, not two platforms (draft for review)

Status: **draft, 2026-09-29**. Nothing here is built. The decisions marked **Decision** belong to the
product owner, and changes to award-intelligence also need its owner. This builds on the decisions
recorded in [AWARD_INTEGRATION.md](AWARD_INTEGRATION.md).

## Where we are

The UI shell and the pay engine are unified. The product underneath is not.

| Area | Unified? | Today |
|---|---|---|
| UI shell | Yes | ORL pages run inside the award-intelligence React app |
| Pay engine | Yes | ORL never calculates pay; roster costs come from award-intelligence's engine, unchanged |
| Navigation | No | Grouped by which system a page came from (Plan / Dispatch / Live / Workforce, then Award / Roster / AI Engines), not by the user's job |
| Features | No | Two ways to roster (award-intelligence's Roster Optimiser, Unallocated Duty and Bulk Ad-Hoc Shifts next to ORL's tiers) and two employee lists |
| Employee data | No | Two records, reconciled by the Employee Sync page |
| Core loop | No | Pay Run uses uploaded timesheets, never ORL's solved roster or Tier 3 actuals |
| Backends | No | FastAPI + Postgres and Express + an audit log, each with its own sign-in |

## Proposal, in order

### 1. One employee record (**Decision**, reverses 2026-09-28)
ORL's `Worker` becomes the single system of record. A payroll import *updates* it: blank fields are
filled and conflicts are queued for review, using the rules the Employee Sync already applies.
award-intelligence then reads employees from ORL. The Sync page becomes an "Import payroll master"
step instead of a bridge between two databases.

- Why ORL: it's the operational database (skills, home site, availability) that rosters are built from.
- Alternative: keep two records and the sync (today's state). It works, but it's a two-system design.

### 2. One navigation, organised by job
Plan · Dispatch · Live · Workforce · Pay · Compliance. Retire award-intelligence's duplicate
roster pages and its separate Employees page. Their useful rules (rest periods, leave, weekly caps
from the Roster Optimiser) move into ORL's Tier 1 solver first (see "Solver gaps").
Needs: the product owner and the award-intelligence owner (it removes their pages).

### 3. Close the loop: roster → actuals → pay
Solved roster plus Tier 3 actuals (cancellations, overruns, sick calls) → timesheets → Pay Run,
through the same engine. This is phase 4 of the integration plan, and the step that makes the demo
one story.

### 4. Later: one gateway and one sign-in
A single origin and sign-in in front of both backends. Merging the backends themselves isn't needed
for the product to feel like one.

## Solver gaps (parked, to be picked up)
- **Tier 1:** no rest-between-shifts rule, no daily-hours maximum, no leave or availability, and no
  soft goals (fairness, preferences, continuity, fatigue). The objective is total award cost only.
- **Rule values** (rest hours, daily maximums) must come from the award engine or verified award
  text, never be hard-coded (award data honesty rule).

## Open question: mobile-first or general workforce? (**Decision**)
ORL is designed for a mobile workforce: home location as the depot, a travel matrix and Tier 2
routing. Tier 1 can roster static posts in a basic way, but a site-based workforce (wards, stores,
aged care) also needs:
- demand-based coverage (N staff per period)
- skill mix per period
- roster patterns and fatigue rules
- availability and preferences

Its region filter also assumes workers are tied to home regions. The healthcare awards in
award-intelligence point to static workforces. The answer decides the Tier 1 data model, so it
should come before more roster UI work.
