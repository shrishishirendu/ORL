"""FastAPI application entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health

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
