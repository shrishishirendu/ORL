# Platform plan: one platform, separable modules

Status: **decisions taken 2026-09-29; nothing below is built yet.** Changes to the award-intelligence
repo still go through its owner. This builds on [AWARD_INTEGRATION.md](AWARD_INTEGRATION.md).

## Decisions (product owner, 2026-09-29)

1. **One platform.** Award Intelligence and the workforce module (ORL) are **modules of one
   platform**, not two systems.
2. **Award Intelligence stays separable.** A customer must be able to buy Award Intelligence alone,
   without the workforce module.
3. **A single employee record**, kept in a shared **platform core**, not inside either module
   (because of decision 2).
4. **General workforce product, with routing as one module.** It covers one shift a day at one site
   *and* several shifts a day at different locations, where intra-day shifts change.

## Module structure

```
Platform core     people (the single employee record), sites, organisation, identity/sign-in,
                  the payroll-import step that updates people
Award Intelligence  award library + engine, interpretation, pay run, compliance   (needs: core)
Workforce (ORL)   rostering (Tier 1), routing (Tier 2), live changes (Tier 3)    (needs: core + AI)
```

- **Dependency rule:** Workforce may depend on Award Intelligence (it prices rosters with the
  engine). Award Intelligence never depends on Workforce. Both depend only on the core.
- **Checked 2026-09-29, this already holds:** award-intelligence's domain and server code have no
  workforce imports. Only the UI shell plugs the workforce pages in (`ORL_NAV`), and a standalone
  build can leave that out.
- **Award Intelligence standalone** = core + Award Intelligence. Timesheets come from uploads, as
  they do today.
- **Full platform** = core + both. Timesheets also come from the solved roster and live actuals.

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

### 1. One employee record in the platform core (decided)
The single employee record moves into the platform core. It starts from ORL's `Worker`, the most
complete operational record, but is owned by the core, not by the workforce module. A payroll import *updates* it: blank fields are
filled and conflicts are queued for review, using the rules the Employee Sync already applies.
award-intelligence then reads employees from ORL. The Sync page becomes an "Import payroll master"
step instead of a bridge between two databases.

- Workforce-only fields (skills, home site, availability) stay in the workforce module and are keyed
  to the core person, so Award Intelligence standalone doesn't carry them.

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

## General workforce (decided): what Tier 1 needs
ORL is designed for a mobile workforce: home location as the depot, a travel matrix and Tier 2
routing. Tier 1 can roster static posts in a basic way, but a site-based workforce (wards, stores,
aged care) also needs:
- demand-based coverage (N staff per period)
- skill mix per period
- roster patterns and fatigue rules
- availability and preferences

Its region filter also assumes workers are tied to home regions.

**Several shifts a day at different locations.** Tier 1 already allows several non-overlapping
shifts per worker per day, but its no-overlap rule has **no travel time between consecutive shifts
at different sites**. A plan can end one shift at site A at 12:00 and start the next at site B at
12:00. The travel matrix exists (Tier 2 uses it); Tier 1 needs to use it too, as a gap between
consecutive shifts. Award rules triggered by such days (broken shifts, travel, minimum engagement
per shift) stay with the engine, which already prices whole days.

Terms to keep apart: a **multi-stop shift** (one shift, several job stops, sequenced by Tier 2
routing) versus **several shifts in a day** (separate shifts, possibly at different sites, assigned
by Tier 1).
