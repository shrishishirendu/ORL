"""Read-only site listing: `GET /sites`.

The one Sites-related addition in the admin data-entry feature (see
app/services/admin_data/'s package docstring) -- purely to populate the
`site_id`/`home_site_id` dropdowns in the new Worker/Shift admin forms
(app/web/static/index.html's Manage tab). Sites themselves stay out of
scope as a directly-editable entity: no create/update/upload here, just
this one listing. Queried directly here (no separate service module) since
there's no logic worth extracting -- matching how app/api/workers.py's and
app/api/shifts.py's own `GET` listings are built directly in the router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models.site import Site
from app.schemas.sites import SiteListResponse, SiteRead

router = APIRouter(tags=["sites"])


@router.get("/sites", response_model=SiteListResponse)
async def list_sites(session: AsyncSession = Depends(get_session)) -> SiteListResponse:
    """List every Site, ordered by code. No pagination -- see
    `SiteListResponse`'s docstring.
    """
    rows = (await session.execute(select(Site).order_by(Site.code))).scalars().all()
    return SiteListResponse(items=[SiteRead.model_validate(s) for s in rows])
