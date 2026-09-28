"""Seed a full, lively demo dataset for the ops dashboard: a **security
workforce** under the MA000016 Security Services Industry Award 2020, NSW
(product-owner decision, 2026-09-25 -- see docs/AWARD_INTEGRATION.md and
docs/AWARD_ENGINE_CONTRACT.md).

Creates: 3 client sites (one per region -- a distribution centre, a retail
park and a CBD office tower), 15 security officers (5 per region, varied
security skills, award classification level and employment type), three
weeks of shifts mixing single-site static guarding / control-room posts
(day, night and weekend crowd-control) with multi-stop **mobile patrol**
runs (lock-up checks and alarm response across the other two sites -- the
natural Tier 2 VRPTW case), `Job` rows for those patrol runs,
`TravelMatrixEntry` rows covering every site pair used (including each
worker's home site, in both directions), and **placeholder**
`AwardCostMatrix` rows making the right workers eligible -- enough for a
real Tier 1 (`POST /rostering/solve`) and Tier 2 (`POST /dispatch/solve`)
solve to actually run against this data end to end, with or without the
Award Engine service.

**Award data honesty.** This script contains NO award rates, penalty
percentages or clause numbers, and never will (see docs/UI_REDESIGN.md's
"Award data honesty rule"). Every `AwardCostMatrix` row it writes is a
placeholder estimate (`is_placeholder=True`, `pay_cost = shift hours x
Settings.placeholder_hourly_rate`, one flat demo rate for everyone -- the
same stopgap `app/services/admin_data/eligibility.py` uses), which exists
only so the demo can roster without the engine. `POST
/award-engine/sync-matrix` replaces these rows with real, engine-priced
rows (`is_placeholder=False`). The worker award fields set here
(`award_code`, `classification_level`, `employment_type`, part-time
contract hours) are the *inputs* the engine prices from, not pay figures;
`over_award_rate` is deliberately left `None` for everyone. Those fields
are only set when the `Worker` model has them (the migration adding them
may land after this script -- see `_award_fields`), so the script runs
either way.

Usage (from the repo root, with `DATABASE_URL` pointing at a migrated
Postgres -- see README.md's "Running locally"):

    python -m scripts.seed_demo_data

**Idempotency.** This is a demo/dev-only script, not a real data-migration
tool: it does not try to be safely re-runnable in the general case (it does
not diff/upsert against whatever's already there). It does check up front
whether the demo data looks like it's already been seeded (by the first
site's code) and, if so, exits cleanly with a message rather than crashing
on a `UniqueViolation` -- but it does NOT attempt to reconcile partial or
edited-since-seeding state. If you want a fresh demo dataset, truncate the
app tables first (e.g. `docker compose down -v && docker compose up
postgres -d && alembic upgrade head`, or `TRUNCATE ... RESTART IDENTITY
CASCADE` the app tables directly) and re-run this script.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory, engine
from app.core.settings import settings
from app.models.award_cost_matrix import AwardCostMatrix
from app.models.job import Job
from app.models.shift import Shift
from app.models.site import Site
from app.models.travel_matrix import TravelMatrixEntry
from app.models.worker import Worker

# ---------------------------------------------------------------------------
# Award scope for the demo workforce.
# ---------------------------------------------------------------------------
_AWARD_CODE = "MA000016"

# MA000016 classification level keys, exactly as the Award Engine service's
# `GET /engine/awards` returns them (`levels[].key`, engine pin ba3aaee -- see
# docs/AWARD_ENGINE_CONTRACT.md). The engine rejects an unknown key with HTTP
# 422. Every worker below refers to a level only through this list (by index).
_MA000016_LEVELS = [
    "MA000016::securityofficerlevel1",
    "MA000016::securityofficerlevel2",
    "MA000016::securityofficerlevel3",
    "MA000016::securityofficerlevel4",
    "MA000016::securityofficerlevel5",
]

# ---------------------------------------------------------------------------
# Sites -- one client site per region. Codes are unchanged from the earlier
# demo dataset so docs/tests/bookmarks that reference them keep working.
# ---------------------------------------------------------------------------
_SITE_DEFS = [
    {"code": "NTH-01", "name": "Northside Distribution Centre", "region": "north"},
    {"code": "STH-01", "name": "Southside Retail Park", "region": "south"},
    {"code": "CBD-01", "name": "CBD Office Tower", "region": "central"},
]

# Travel times (minutes) between the three sites -- symmetric for this demo,
# though TravelMatrixEntry itself does not require that (see that model's
# docstring). Every pair actually used by a job or a worker's home site in
# the shifts below must appear here, in both directions, or a real Tier 2
# solve involving that pair will fail with MissingTravelTimeError.
_TRAVEL_MINUTES = {
    ("north", "south"): 45,
    ("north", "central"): 30,
    ("south", "central"): 40,
}

# ---------------------------------------------------------------------------
# Workers -- security officers with varied skills/regions/home sites. Five
# per region so Tier 1's region-eligibility filter (worker.region ==
# shift.site.region) always has real slack to choose from, and a solved
# roster has enough assignments to look like an actual working week.
#
# Skills:
#   static_guard     -- fixed-post guarding at one site
#   cctv_monitoring  -- control-room / CCTV post
#   first_aid        -- first-aid-qualified officer (required on some posts)
#   crowd_control    -- crowd control (weekend retail-park coverage)
#   mobile_patrol    -- licensed patrol driver (multi-stop patrol runs)
#
# Award fields: `level` is an index into _MA000016_LEVELS (never a literal
# key, so the keys can be reconciled in one place). Part-time workers carry
# their agreed contract hours (`ordinary_hours_per_week`,
# `agreed_ordinary_hours_per_shift`) as the engine contract requires; these
# are employment-contract inputs chosen for the demo, not award figures.
# Full-time and casual workers leave both as None.
# ---------------------------------------------------------------------------
_WORKER_DEFS = [
    # --- north (home NTH-01, Northside Distribution Centre) ---
    {
        "name": "Alice Nguyen",
        "employee_code": "SEC-001",
        "skills": ["static_guard", "first_aid"],
        "region": "north",
        "home_site_code": "NTH-01",
        "level": 2,
        "employment_type": "full_time",
    },
    {
        "name": "Ben Carter",
        "employee_code": "SEC-002",
        "skills": ["static_guard", "cctv_monitoring"],
        "region": "north",
        "home_site_code": "NTH-01",
        "level": 1,
        "employment_type": "part_time",
        "ordinary_hours_per_week": 32.0,
        "agreed_ordinary_hours_per_shift": 8.0,
    },
    {
        "name": "Priya Sharma",
        "employee_code": "SEC-003",
        "skills": ["mobile_patrol", "static_guard", "first_aid"],
        "region": "north",
        "home_site_code": "NTH-01",
        "level": 2,
        "employment_type": "casual",
    },
    {
        "name": "Liam O'Connor",
        "employee_code": "SEC-004",
        "skills": ["mobile_patrol", "static_guard"],
        "region": "north",
        "home_site_code": "NTH-01",
        "level": 1,
        "employment_type": "casual",
    },
    {
        "name": "Noah Williams",
        "employee_code": "SEC-005",
        "skills": ["cctv_monitoring", "static_guard"],
        "region": "north",
        "home_site_code": "NTH-01",
        "level": 0,
        "employment_type": "casual",
    },
    # --- south (home STH-01, Southside Retail Park) ---
    {
        "name": "Carla Diaz",
        "employee_code": "SEC-006",
        "skills": ["static_guard", "crowd_control", "cctv_monitoring"],
        "region": "south",
        "home_site_code": "STH-01",
        "level": 1,
        "employment_type": "casual",
    },
    {
        "name": "Ella Osei",
        "employee_code": "SEC-007",
        "skills": ["mobile_patrol", "static_guard"],
        "region": "south",
        "home_site_code": "STH-01",
        "level": 2,
        "employment_type": "part_time",
        "ordinary_hours_per_week": 24.0,
        "agreed_ordinary_hours_per_shift": 8.0,
    },
    {
        "name": "Mia Thompson",
        "employee_code": "SEC-008",
        "skills": ["crowd_control", "first_aid", "static_guard"],
        "region": "south",
        "home_site_code": "STH-01",
        "level": 1,
        "employment_type": "casual",
    },
    {
        "name": "Jack Wilson",
        "employee_code": "SEC-009",
        "skills": ["mobile_patrol", "crowd_control"],
        "region": "south",
        "home_site_code": "STH-01",
        "level": 0,
        "employment_type": "casual",
    },
    {
        "name": "Zara Ahmed",
        "employee_code": "SEC-010",
        "skills": ["cctv_monitoring", "first_aid", "static_guard"],
        "region": "south",
        "home_site_code": "STH-01",
        "level": 3,
        "employment_type": "full_time",
    },
    # --- central (home CBD-01, CBD Office Tower) ---
    {
        "name": "Dev Kapoor",
        "employee_code": "SEC-011",
        "skills": ["mobile_patrol", "static_guard"],
        "region": "central",
        "home_site_code": "CBD-01",
        "level": 2,
        "employment_type": "full_time",
    },
    {
        "name": "Sophie Chen",
        "employee_code": "SEC-012",
        "skills": ["cctv_monitoring", "first_aid"],
        "region": "central",
        "home_site_code": "CBD-01",
        "level": 4,
        "employment_type": "full_time",
    },
    {
        "name": "Ryan Murphy",
        "employee_code": "SEC-013",
        "skills": ["mobile_patrol", "static_guard"],
        "region": "central",
        "home_site_code": "CBD-01",
        "level": 1,
        "employment_type": "casual",
    },
    {
        "name": "Grace Kim",
        "employee_code": "SEC-014",
        "skills": ["static_guard", "first_aid"],
        "region": "central",
        "home_site_code": "CBD-01",
        "level": 0,
        "employment_type": "part_time",
        "ordinary_hours_per_week": 24.0,
        "agreed_ordinary_hours_per_shift": 8.0,
    },
    {
        "name": "Tom Anderson",
        "employee_code": "SEC-015",
        "skills": ["cctv_monitoring", "static_guard", "crowd_control"],
        "region": "central",
        "home_site_code": "CBD-01",
        "level": 1,
        "employment_type": "casual",
    },
]

_WORKERS_BY_REGION: dict[str, list[dict]] = {}
for _w in _WORKER_DEFS:
    _WORKERS_BY_REGION.setdefault(_w["region"], []).append(_w)

# ---------------------------------------------------------------------------
# Three weeks of shifts, generated (not hand-listed) so the demo period is
# genuinely busy rather than one token shift per day. `day_offset` is
# relative to the seeded period's Monday. `eligible_workers` names which
# _WORKER_DEFS["name"] entries get an AwardCostMatrix row (eligible=True)
# for this shift -- i.e. exactly the workers whose region/skills make them a
# real Tier 1 candidate, per app/services/rostering/solver.py's eligibility
# filter (required_skill in worker.skills, AND worker.region ==
# shift.site.region, AND AwardCostMatrix.eligible).
#
# Shift patterns (8-hour shifts throughout; `pattern` is only used for the
# summary printout, it is not persisted):
#   day      07:00-15:00  single-site static / control-room / first-aid post
#   patrol   15:00-23:00  multi-stop mobile patrol run (Tier 2 routed)
#   night    23:00-07:00  single-site static / control-room post, overnight
#                         (end_time <= start_time -- Shift, Tier 1 and the
#                         dispatch service all treat that as ending the
#                         next day)
#   weekend  10:00-18:00  crowd control at the retail park, Sat and Sun
#
# `jobs` (only set for patrol shifts) is a list of
# (site_code, window_start_time, window_end_time, duration_minutes) stops,
# each on the shift's own `date` and inside its 15:00-23:00 span, so no job
# window ever crosses midnight. A job's site does not have to match the
# shift's own site -- Tier 2 routes from the worker's home, through the job
# list, and back home (see app/services/dispatch/solver.py), and the demo
# travel matrix above covers every pair of the three sites -- so a patrol
# run's stops are deliberately the *other* two sites (an evening lock-up
# check at one, an alarm-response / perimeter check at the other) to
# exercise real multi-site routing.
# ---------------------------------------------------------------------------
_NUM_DEMO_DAYS = 21
_REGION_SITE_CODE = {"north": "NTH-01", "south": "STH-01", "central": "CBD-01"}
_REGIONS = ["north", "south", "central"]
_DAY_POST_SKILL_CYCLE = ["static_guard", "cctv_monitoring", "first_aid", "static_guard"]
_NIGHT_POST_SKILL_CYCLE = ["static_guard", "cctv_monitoring"]
_WEEKEND_CROWD_REGION = "south"  # the retail park

_DAY = (time(7, 0), time(15, 0))
_PATROL = (time(15, 0), time(23, 0))
_NIGHT = (time(23, 0), time(7, 0))
_WEEKEND = (time(10, 0), time(18, 0))


def _eligible_for_skill(region: str, skill: str) -> list[str]:
    return [w["name"] for w in _WORKERS_BY_REGION[region] if skill in w["skills"]]


def _build_shift_defs(num_days: int = _NUM_DEMO_DAYS) -> list[dict]:
    """Deterministically generate a multi-week security roster demand.

    Every site gets (almost) daily cover in its primary slot -- usually a
    day post, and roughly every third primary slot a mobile patrol run
    instead -- plus periodic overnight posts and weekend crowd control at
    the retail park: enough volume and variety for a solved roster and a
    solved route to both look like a real operation rather than a fixture.
    """
    shift_defs: list[dict] = []
    shift_counter = 0

    def add(
        day_offset: int,
        site_code: str,
        window: tuple[time, time],
        skill: str,
        region: str,
        pattern: str,
        jobs: list | None = None,
    ) -> None:
        nonlocal shift_counter
        eligible = _eligible_for_skill(region, skill)
        if not eligible:
            return
        shift_def = {
            "day_offset": day_offset,
            "site_code": site_code,
            "start_time": window[0],
            "end_time": window[1],
            "required_skill": skill,
            "is_multi_stop": jobs is not None,
            "eligible_workers": eligible,
            "pattern": pattern,
        }
        if jobs is not None:
            shift_def["jobs"] = jobs
        shift_defs.append(shift_def)
        shift_counter += 1

    for day_offset in range(num_days):
        weekday = day_offset % 7  # the period starts on a Monday
        for region_idx, region in enumerate(_REGIONS):
            site_code = _REGION_SITE_CODE[region]

            # Primary slot. Skip roughly one day in seven per region -- a
            # quiet day for that client (site closed, alarm-monitored only),
            # so the period isn't perfectly uniform.
            if (day_offset + region_idx) % 7 != 6:
                if shift_counter % 3 == 0:
                    other_sites = [c for c in _REGION_SITE_CODE.values() if c != site_code]
                    stop_a, stop_b = other_sites[0], other_sites[1]
                    if shift_counter % 2:
                        stop_a, stop_b = stop_b, stop_a
                    add(
                        day_offset,
                        site_code,
                        _PATROL,
                        "mobile_patrol",
                        region,
                        "patrol",
                        jobs=[
                            # evening lock-up check
                            (stop_a, time(17, 0), time(18, 30), 30),
                            # alarm response / perimeter check
                            (stop_b, time(20, 0), time(21, 30), 45),
                        ],
                    )
                else:
                    skill = _DAY_POST_SKILL_CYCLE[
                        (day_offset + region_idx) % len(_DAY_POST_SKILL_CYCLE)
                    ]
                    add(day_offset, site_code, _DAY, skill, region, "day")

            # A periodic overnight post at this site -- busier nights
            # without every site being staffed around the clock.
            if (day_offset // 3 + region_idx) % 3 == 0:
                skill = _NIGHT_POST_SKILL_CYCLE[
                    (day_offset + region_idx) % len(_NIGHT_POST_SKILL_CYCLE)
                ]
                add(day_offset, site_code, _NIGHT, skill, region, "night")

            # Weekend crowd control at the retail park.
            if region == _WEEKEND_CROWD_REGION and weekday in (5, 6):
                add(day_offset, site_code, _WEEKEND, "crowd_control", region, "weekend")

    return shift_defs


_SHIFT_DEFS = _build_shift_defs()

# A generous cap, well above what any worker would actually be assigned in
# this demo -- see _WORKER_DEFS/_SHIFT_DEFS above; not meant to bind, just to
# demonstrate a populated AwardCostMatrix.max_hours. It is a demo solver
# bound, NOT an award figure. IMPORTANT: Tier 1 enforces max_hours over the
# *entire* rostering period passed to `POST /rostering/solve`, not per
# calendar week (see app/services/rostering/solver.py's module docstring,
# point 1) -- so this must scale with `_NUM_DEMO_DAYS`, not stay a flat
# "weekly" figure, or a multi-week demo period silently turns this into a
# real (and quickly violated) constraint instead of a non-binding one.
_MAX_HOURS_CAP = 40.0 * (_NUM_DEMO_DAYS / 7)


def _next_monday(today: date) -> date:
    """The next Monday strictly after `today` (so re-running the script on a
    different day always lands the demo week in the near future, never in
    the past).
    """
    days_ahead = (7 - today.weekday()) % 7
    return today + timedelta(days=days_ahead or 7)


def _shift_hours(start_time: time, end_time: time) -> float:
    start_dt = datetime.combine(date(2000, 1, 1), start_time)
    end_dt = datetime.combine(date(2000, 1, 1), end_time)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return (end_dt - start_dt).total_seconds() / 3600


def _award_fields(worker_def: dict) -> dict:
    """Worker award-field kwargs, limited to the columns `Worker` actually
    has. The migration adding these fields is being built separately; before
    it lands `hasattr(Worker, "award_code")` is False and this returns `{}`,
    so the seed still runs (workers are then simply "not yet mapped" to an
    award). Each field is checked individually so a partial model still
    works.
    """
    if not hasattr(Worker, "award_code"):
        return {}
    candidate = {
        "award_code": _AWARD_CODE,
        "classification_level": _MA000016_LEVELS[worker_def["level"]],
        "employment_type": worker_def["employment_type"],
        # Deliberately None: no over-award rates are invented for the demo.
        "over_award_rate": None,
        "ordinary_hours_per_week": worker_def.get("ordinary_hours_per_week"),
        "agreed_ordinary_hours_per_shift": worker_def.get("agreed_ordinary_hours_per_shift"),
    }
    return {field: value for field, value in candidate.items() if hasattr(Worker, field)}


async def _already_seeded(session: AsyncSession) -> bool:
    existing = (
        await session.execute(select(Site).where(Site.code == _SITE_DEFS[0]["code"]))
    ).scalar_one_or_none()
    return existing is not None


async def seed(session: AsyncSession) -> None:
    if await _already_seeded(session):
        print(
            f"Demo data already present (a Site with code {_SITE_DEFS[0]['code']!r} "
            "already exists) -- skipping. Truncate the app tables first if you want a "
            "fresh demo dataset (see this script's module docstring)."
        )
        return

    # --- Sites ---
    sites_by_region: dict[str, Site] = {}
    sites_by_code: dict[str, Site] = {}
    for site_def in _SITE_DEFS:
        site = Site(code=site_def["code"], name=site_def["name"], region=site_def["region"])
        session.add(site)
        await session.flush()
        sites_by_region[site_def["region"]] = site
        sites_by_code[site_def["code"]] = site
    print(f"Created {len(sites_by_code)} sites: {sorted(sites_by_code)}")

    # --- Travel matrix (both directions for every pair) ---
    travel_count = 0
    for (region_a, region_b), minutes in _TRAVEL_MINUTES.items():
        site_a, site_b = sites_by_region[region_a], sites_by_region[region_b]
        session.add(
            TravelMatrixEntry(from_site_id=site_a.id, to_site_id=site_b.id, travel_minutes=minutes)
        )
        session.add(
            TravelMatrixEntry(from_site_id=site_b.id, to_site_id=site_a.id, travel_minutes=minutes)
        )
        travel_count += 2
    await session.flush()
    print(f"Created {travel_count} travel matrix entries (covering every site pair, both ways)")

    # --- Workers ---
    workers_by_name: dict[str, Worker] = {}
    award_fields_set = False
    for worker_def in _WORKER_DEFS:
        award_fields = _award_fields(worker_def)
        award_fields_set = award_fields_set or bool(award_fields)
        worker = Worker(
            name=worker_def["name"],
            employee_code=worker_def["employee_code"],
            skills=list(worker_def["skills"]),
            region=worker_def["region"],
            home_site_id=sites_by_code[worker_def["home_site_code"]].id,
            active=True,
            **award_fields,
        )
        session.add(worker)
        await session.flush()
        workers_by_name[worker_def["name"]] = worker
    print(f"Created {len(workers_by_name)} workers: {sorted(workers_by_name)}")
    if award_fields_set:
        print(f"  award fields set ({_AWARD_CODE}, levels {_MA000016_LEVELS[0]!r}..)")
    else:
        print("  Worker has no award fields yet (migration not applied) -- skipped them")

    # --- Shifts, Jobs, placeholder AwardCostMatrix ---
    # Every AwardCostMatrix row here is a PLACEHOLDER estimate: one flat
    # demo rate x shift hours, is_placeholder=True. No award rates, penalty
    # loadings or clauses are applied (or known) here -- `POST
    # /award-engine/sync-matrix` replaces these rows with engine-priced
    # ones.
    placeholder_rate = settings.placeholder_hourly_rate
    period_start = _next_monday(date.today())
    shift_count = 0
    job_count = 0
    award_row_count = 0
    pattern_counts: Counter[str] = Counter()
    for shift_def in _SHIFT_DEFS:
        shift_date = period_start + timedelta(days=shift_def["day_offset"])
        shift = Shift(
            date=shift_date,
            start_time=shift_def["start_time"],
            end_time=shift_def["end_time"],
            required_skill=shift_def["required_skill"],
            site_id=sites_by_code[shift_def["site_code"]].id,
            is_multi_stop=shift_def["is_multi_stop"],
        )
        session.add(shift)
        await session.flush()
        shift_count += 1
        pattern_counts[shift_def["pattern"]] += 1

        for site_code, window_start_t, window_end_t, duration_minutes in shift_def.get("jobs", []):
            session.add(
                Job(
                    shift_id=shift.id,
                    site_id=sites_by_code[site_code].id,
                    window_start=datetime.combine(shift_date, window_start_t),
                    window_end=datetime.combine(shift_date, window_end_t),
                    duration_minutes=duration_minutes,
                )
            )
            job_count += 1

        hours = _shift_hours(shift_def["start_time"], shift_def["end_time"])
        pay_cost = round(placeholder_rate * hours, 2)
        for worker_name in shift_def["eligible_workers"]:
            session.add(
                AwardCostMatrix(
                    worker_id=workers_by_name[worker_name].id,
                    day=shift_date,
                    shift_id=shift.id,
                    pay_cost=pay_cost,
                    eligible=True,
                    min_hours=None,
                    max_hours=_MAX_HOURS_CAP,
                    is_placeholder=True,
                )
            )
            award_row_count += 1

    period_end = period_start + timedelta(days=_NUM_DEMO_DAYS - 1)
    await session.flush()
    print(
        f"Created {shift_count} shifts ({period_start} .. {period_end}; "
        f"{', '.join(f'{k}={v}' for k, v in sorted(pattern_counts.items()))}), "
        f"{job_count} jobs, {award_row_count} placeholder AwardCostMatrix rows "
        f"(flat demo rate {placeholder_rate}/h -- run POST /award-engine/sync-matrix "
        "for engine-priced rows)"
    )

    await session.commit()
    print("Demo data committed.")
    print(
        f"Try: curl -X POST http://localhost:8000/rostering/solve "
        f'-H "Content-Type: application/json" '
        f'-d \'{{"period_start": "{period_start}", '
        f'"period_end": "{period_end}"}}\''
    )


async def main() -> None:
    async with async_session_factory() as session:
        await seed(session)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
