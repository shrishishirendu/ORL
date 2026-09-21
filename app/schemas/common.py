"""Small response schemas shared across the read-only GET endpoints in
``app/api/`` (Part 1 of the ops dashboard task: workers/shifts/rosters/
routes/events listings).

These are deliberately thin "nested summary" shapes (e.g. a worker's home
site, a shift's site) rather than reusing a full ``*Read`` model recursively,
so a list response doesn't pull in more relationship data than a table row
needs.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SiteSummary(BaseModel):
    """A ``Site`` row, as embedded in worker/shift/route responses."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    region: str | None = None
