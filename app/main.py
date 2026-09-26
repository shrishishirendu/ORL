"""FastAPI application entrypoint."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import (
    admin_data,
    award_engine,
    dispatch,
    events,
    health,
    rostering,
    rosters,
    routes,
    shifts,
    sites,
    workers,
)

app = FastAPI(title="ORL", description="Operational Resource Logistics")

# CORS is off by default: no allowed origins are configured. Enable and
# configure this middleware explicitly for the environments that need it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=[],
    allow_headers=[],
)

app.include_router(health.router)
app.include_router(rostering.router)
app.include_router(dispatch.router)
app.include_router(events.router)
app.include_router(workers.router)
app.include_router(shifts.router)
app.include_router(rosters.router)
app.include_router(routes.router)
app.include_router(sites.router)
app.include_router(admin_data.router)
app.include_router(award_engine.router)

# The ops/admin dashboard: a static (no-build-step) HTML/CSS/JS app that
# talks to the JSON API above via same-origin fetch() calls. Mounted at
# "/admin" (not "/") so it never collides with the API's own root-level
# paths (/workers, /shifts, /rosters, ...). See app/web/static/.
_STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
app.mount("/admin", StaticFiles(directory=_STATIC_DIR, html=True), name="admin")
