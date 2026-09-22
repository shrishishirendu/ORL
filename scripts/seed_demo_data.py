"""Seed a full, lively demo dataset for the ops dashboard (Part 2 of the
"ops/admin web dashboard" task).

Creates: 3 sites (one per region), 15 workers (varied skills/regions/home
sites, 5 per region), three weeks of shifts mixing single-site and
multi-stop (each matching its workers' region), `AwardCostMatrix` rows
making the right workers eligible with sensible pay costs, `Job` rows for
the multi-stop shifts, and `TravelMatrixEntry` rows covering every site pair
used (including each worker's home site, in both directions) -- enough for
a real Tier 1 (`POST /rostering/solve`) and Tier 2 (`POST /dispatch/solve`)
solve to actually run against this data end to end, and to look like a
genuinely busy roster rather than a bare-minimum fixture.

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
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory, engine
from app.models.award_cost_matrix import AwardCostMatrix
from app.models.job import Job
from app.models.shift import Shift
from app.models.site import Site
from app.models.travel_matrix import TravelMatrixEntry
from app.models.worker import Worker

# ---------------------------------------------------------------------------
# Sites -- one per region.
# ---------------------------------------------------------------------------
_SITE_DEFS = [
    {"code": "NTH-01", "name": "Northside Community Centre", "region": "north"},
    {"code": "STH-01", "name": "Southside Community Centre", "region": "south"},
    {"code": "CBD-01", "name": "CBD Hub", "region": "central"},
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
# Workers -- varied skills/regions/home sites. Five per region so Tier 1's
# region-eligibility filter (worker.region == shift.site.region) always has
# real slack to choose from, and a solved roster has enough assignments to
# look like an actual working week rather than a token example.
# ---------------------------------------------------------------------------
_WORKER_DEFS = [
    # --- north (home NTH-01) ---
    {
        "name": "Alice Nguyen",
        "skills": ["nursing", "first_aid"],
        "region": "north",
        "home_site_code": "NTH-01",
        "hourly_rate": 45.0,
    },
    {
        "name": "Ben Carter",
        "skills": ["nursing"],
        "region": "north",
        "home_site_code": "NTH-01",
        "hourly_rate": 42.0,
    },
    {
        "name": "Priya Sharma",
        "skills": ["nursing", "driving"],
        "region": "north",
        "home_site_code": "NTH-01",
        "hourly_rate": 47.0,
    },
    {
        "name": "Liam O'Connor",
        "skills": ["cleaning", "driving"],
        "region": "north",
        "home_site_code": "NTH-01",
        "hourly_rate": 39.0,
    },
    {
        "name": "Noah Williams",
        "skills": ["first_aid", "cleaning"],
        "region": "north",
        "home_site_code": "NTH-01",
        "hourly_rate": 41.0,
    },
    # --- south (home STH-01) ---
    {
        "name": "Carla Diaz",
        "skills": ["cleaning", "nursing"],
        "region": "south",
        "home_site_code": "STH-01",
        "hourly_rate": 38.0,
    },
    {
        "name": "Ella Osei",
        "skills": ["nursing", "driving"],
        "region": "south",
        "home_site_code": "STH-01",
        "hourly_rate": 40.0,
    },
    {
        "name": "Mia Thompson",
        "skills": ["nursing", "first_aid"],
        "region": "south",
        "home_site_code": "STH-01",
        "hourly_rate": 44.0,
    },
    {
        "name": "Jack Wilson",
        "skills": ["cleaning", "driving"],
        "region": "south",
        "home_site_code": "STH-01",
        "hourly_rate": 37.0,
    },
    {
        "name": "Zara Ahmed",
        "skills": ["first_aid", "nursing"],
        "region": "south",
        "home_site_code": "STH-01",
        "hourly_rate": 43.0,
    },
    # --- central (home CBD-01) ---
    {
        "name": "Dev Kapoor",
        "skills": ["driving", "cleaning"],
        "region": "central",
        "home_site_code": "CBD-01",
        "hourly_rate": 50.0,
    },
    {
        "name": "Sophie Chen",
        "skills": ["nursing", "first_aid"],
        "region": "central",
        "home_site_code": "CBD-01",
        "hourly_rate": 46.0,
    },
    {
        "name": "Ryan Murphy",
        "skills": ["driving"],
        "region": "central",
        "home_site_code": "CBD-01",
        "hourly_rate": 41.0,
    },
    {
        "name": "Grace Kim",
        "skills": ["cleaning"],
        "region": "central",
        "home_site_code": "CBD-01",
        "hourly_rate": 36.0,
    },
    {
        "name": "Tom Anderson",
        "skills": ["nursing", "driving"],
        "region": "central",
        "home_site_code": "CBD-01",
        "hourly_rate": 48.0,
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
# `jobs` (only set for multi-stop shifts) is a list of
# (site_code, window_start_time, window_end_time, duration_minutes) stops,
# each on the shift's own `date`. A job's site does not have to match the
# shift's own site -- Tier 2 routes from the worker's home, through the job
# list, and back home (see app/services/dispatch/solver.py), and the demo
# travel matrix below covers every pair of the three sites -- so stops are
# deliberately sent to the *other* two sites to exercise real multi-site
# routing.
# ---------------------------------------------------------------------------
_NUM_DEMO_DAYS = 21
_REGION_SITE_CODE = {"north": "NTH-01", "south": "STH-01", "central": "CBD-01"}
_REGIONS = ["north", "south", "central"]
_SKILL_CYCLE = ["nursing", "cleaning", "driving", "first_aid"]


def _eligible_for_skill(region: str, skill: str) -> list[str]:
    return [w["name"] for w in _WORKERS_BY_REGION[region] if skill in w["skills"]]


def _build_shift_defs(num_days: int = _NUM_DEMO_DAYS) -> list[dict]:
    """Deterministically generate a multi-week shift roster demand.

    Every site gets (almost) daily day shifts across the period, most
    regions also get periodic evening shifts, and roughly a third of shifts
    are multi-stop -- enough volume and variety for a solved roster and a
    solved route to both look like a real operation rather than a fixture.
    """
    shift_defs: list[dict] = []
    shift_counter = 0
    for day_offset in range(num_days):
        for region_idx, region in enumerate(_REGIONS):
            site_code = _REGION_SITE_CODE[region]

            # Skip roughly one day in seven per region -- a rostered quiet
            # day for that site, so the period isn't perfectly uniform.
            if (day_offset + region_idx) % 7 == 6:
                continue

            primary_skill = _SKILL_CYCLE[(day_offset + region_idx) % len(_SKILL_CYCLE)]
            primary_eligible = _eligible_for_skill(region, primary_skill)
            if not primary_eligible:
                continue

            is_multi_stop = shift_counter % 3 == 0
            shift_def = {
                "day_offset": day_offset,
                "site_code": site_code,
                "start_time": time(8, 0),
                "end_time": time(16, 0),
                "required_skill": primary_skill,
                "is_multi_stop": is_multi_stop,
                "eligible_workers": primary_eligible,
            }
            if is_multi_stop:
                other_sites = [c for c in _REGION_SITE_CODE.values() if c != site_code]
                stop_a, stop_b = other_sites[0], other_sites[1]
                if shift_counter % 2:
                    stop_a, stop_b = stop_b, stop_a
                shift_def["jobs"] = [
                    (stop_a, time(9, 0), time(10, 30), 60),
                    (stop_b, time(12, 0), time(13, 30), 60),
                ]
            shift_defs.append(shift_def)
            shift_counter += 1

            # A periodic second, evening shift at this site with a
            # different required skill -- busier days without every site
            # running two shifts every single day.
            if (day_offset // 3 + region_idx) % 3 == 0:
                evening_skill = _SKILL_CYCLE[(day_offset + region_idx + 2) % len(_SKILL_CYCLE)]
                evening_eligible = _eligible_for_skill(region, evening_skill)
                if evening_eligible:
                    shift_defs.append(
                        {
                            "day_offset": day_offset,
                            "site_code": site_code,
                            "start_time": time(16, 0),
                            "end_time": time(22, 0),
                            "required_skill": evening_skill,
                            "is_multi_stop": False,
                            "eligible_workers": evening_eligible,
                        }
                    )
                    shift_counter += 1

    return shift_defs


_SHIFT_DEFS = _build_shift_defs()

# A generous cap, well above what any worker would actually be assigned in
# this demo -- see _WORKER_DEFS/_SHIFT_DEFS above; not meant to bind, just to
# demonstrate a populated AwardCostMatrix.max_hours. IMPORTANT: Tier 1
# enforces max_hours over the *entire* rostering period passed to
# `POST /rostering/solve`, not per calendar week (see
# app/services/rostering/solver.py's module docstring, point 1) -- so this
# must scale with `_NUM_DEMO_DAYS`, not stay a flat "weekly" figure, or a
# multi-week demo period silently turns this into a real (and quickly
# violated) constraint instead of a non-binding one.
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
    for worker_def in _WORKER_DEFS:
        worker = Worker(
            name=worker_def["name"],
            skills=list(worker_def["skills"]),
            region=worker_def["region"],
            home_site_id=sites_by_code[worker_def["home_site_code"]].id,
            active=True,
        )
        session.add(worker)
        await session.flush()
        workers_by_name[worker_def["name"]] = worker
    print(f"Created {len(workers_by_name)} workers: {sorted(workers_by_name)}")

    # --- Shifts, Jobs, AwardCostMatrix ---
    period_start = _next_monday(date.today())
    shift_count = 0
    job_count = 0
    award_row_count = 0
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
        for worker_name in shift_def["eligible_workers"]:
            worker = workers_by_name[worker_name]
            worker_def = next(w for w in _WORKER_DEFS if w["name"] == worker_name)
            pay_cost = round(worker_def["hourly_rate"] * hours, 2)
            session.add(
                AwardCostMatrix(
                    worker_id=worker.id,
                    day=shift_date,
                    shift_id=shift.id,
                    pay_cost=pay_cost,
                    eligible=True,
                    min_hours=None,
                    max_hours=_MAX_HOURS_CAP,
                )
            )
            award_row_count += 1

    period_end = period_start + timedelta(days=_NUM_DEMO_DAYS - 1)
    await session.flush()
    print(
        f"Created {shift_count} shifts ({period_start} .. {period_end}), "
        f"{job_count} jobs, {award_row_count} AwardCostMatrix rows"
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
