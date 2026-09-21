"""Seed a small, complete demo dataset for the ops dashboard (Part 2 of the
"ops/admin web dashboard" task).

Creates: 3 sites (one per region), 5 workers (varied skills/regions/home
sites), a week of shifts mixing single-site and multi-stop (each matching
its workers' region), `AwardCostMatrix` rows making the right workers
eligible with sensible pay costs, `Job` rows for the multi-stop shifts, and
`TravelMatrixEntry` rows covering every site pair used (including each
worker's home site, in both directions) -- enough for a real Tier 1
(`POST /rostering/solve`) and Tier 2 (`POST /dispatch/solve`) solve to
actually run against this data end to end.

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
# Workers -- varied skills/regions/home sites.
# ---------------------------------------------------------------------------
_WORKER_DEFS = [
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
        "name": "Carla Diaz",
        "skills": ["cleaning", "nursing"],
        "region": "south",
        "home_site_code": "STH-01",
        "hourly_rate": 38.0,
    },
    {
        "name": "Dev Kapoor",
        "skills": ["driving", "cleaning"],
        "region": "central",
        "home_site_code": "CBD-01",
        "hourly_rate": 50.0,
    },
    {
        "name": "Ella Osei",
        "skills": ["nursing", "driving"],
        "region": "south",
        "home_site_code": "STH-01",
        "hourly_rate": 40.0,
    },
]

# ---------------------------------------------------------------------------
# A week of shifts. `day_offset` is relative to the seeded period's Monday.
# `eligible_workers` names which _WORKER_DEFS["name"] entries get an
# AwardCostMatrix row (eligible=True) for this shift -- i.e. exactly the
# workers whose region/skills make them a real Tier 1 candidate, per
# app/services/rostering/solver.py's eligibility filter (required_skill in
# worker.skills, AND worker.region == shift.site.region, AND
# AwardCostMatrix.eligible).
#
# `jobs` (only set for multi-stop shifts) is a list of
# (site_code, window_start_time, window_end_time, duration_minutes) stops,
# each on the shift's own `date`.
# ---------------------------------------------------------------------------
_SHIFT_DEFS = [
    {
        "day_offset": 0,  # Monday
        "site_code": "NTH-01",
        "start_time": time(8, 0),
        "end_time": time(16, 0),
        "required_skill": "nursing",
        "is_multi_stop": False,
        "eligible_workers": ["Alice Nguyen", "Ben Carter"],
    },
    {
        "day_offset": 1,  # Tuesday
        "site_code": "NTH-01",
        "start_time": time(8, 0),
        "end_time": time(16, 0),
        "required_skill": "nursing",
        "is_multi_stop": True,
        "eligible_workers": ["Alice Nguyen", "Ben Carter"],
        "jobs": [
            ("NTH-01", time(8, 30), time(10, 0), 60),
            ("CBD-01", time(11, 0), time(13, 0), 60),
        ],
    },
    {
        "day_offset": 2,  # Wednesday
        "site_code": "STH-01",
        "start_time": time(9, 0),
        "end_time": time(17, 0),
        "required_skill": "cleaning",
        "is_multi_stop": False,
        "eligible_workers": ["Carla Diaz"],
    },
    {
        "day_offset": 3,  # Thursday
        "site_code": "STH-01",
        "start_time": time(8, 0),
        "end_time": time(16, 0),
        "required_skill": "nursing",
        "is_multi_stop": True,
        "eligible_workers": ["Carla Diaz", "Ella Osei"],
        "jobs": [
            ("STH-01", time(8, 30), time(10, 0), 60),
            ("NTH-01", time(11, 30), time(13, 0), 60),
        ],
    },
    {
        "day_offset": 4,  # Friday
        "site_code": "CBD-01",
        "start_time": time(7, 0),
        "end_time": time(15, 0),
        "required_skill": "driving",
        "is_multi_stop": False,
        "eligible_workers": ["Dev Kapoor"],
    },
    {
        "day_offset": 5,  # Saturday
        "site_code": "CBD-01",
        "start_time": time(8, 0),
        "end_time": time(14, 0),
        "required_skill": "cleaning",
        "is_multi_stop": True,
        "eligible_workers": ["Dev Kapoor"],
        "jobs": [
            ("CBD-01", time(8, 30), time(9, 30), 60),
            ("STH-01", time(10, 30), time(11, 30), 60),
        ],
    },
    {
        "day_offset": 6,  # Sunday
        "site_code": "NTH-01",
        "start_time": time(8, 0),
        "end_time": time(16, 0),
        "required_skill": "first_aid",
        "is_multi_stop": False,
        "eligible_workers": ["Alice Nguyen"],
    },
]

# A generous weekly cap, well above any single shift's hours in this demo --
# see _WORKER_DEFS/_SHIFT_DEFS above; not meant to bind, just to demonstrate
# a populated AwardCostMatrix.max_hours.
_MAX_HOURS_PER_WEEK = 40.0


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
                    max_hours=_MAX_HOURS_PER_WEEK,
                )
            )
            award_row_count += 1

    await session.flush()
    print(
        f"Created {shift_count} shifts ({period_start} .. "
        f"{period_start + timedelta(days=6)}), {job_count} jobs, "
        f"{award_row_count} AwardCostMatrix rows"
    )

    await session.commit()
    print("Demo data committed.")
    print(
        f"Try: curl -X POST http://localhost:8000/rostering/solve "
        f'-H "Content-Type: application/json" '
        f'-d \'{{"period_start": "{period_start}", '
        f'"period_end": "{period_start + timedelta(days=6)}"}}\''
    )


async def main() -> None:
    async with async_session_factory() as session:
        await seed(session)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
