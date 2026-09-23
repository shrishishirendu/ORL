"""Admin data-entry service layer: Workers/Shifts manual entry + bulk upload.

Per the task brief scoping this feature down deliberately: **Workers and
Shifts only**. Sites, Jobs, Travel Matrix and Award Cost Matrix are NOT
directly-editable entities here (Sites gets one read-only helper endpoint,
``GET /sites``, built directly in ``app/api/sites.py`` since it's a trivial
listing with no service-layer logic worth extracting -- see that module).

This package is the DB-facing boundary behind ``app/api/workers.py``'s
POST/PATCH additions, ``app/api/shifts.py``'s POST/PATCH additions, and
``app/api/admin_data.py`` (bulk upload + template), the same way
``app/services/rostering/service.py`` / ``app/services/dispatch/service.py``
sit behind their routers -- keeping the ``app/api/*.py`` files thin
(parse request -> call service -> shape response).

Modules:

- ``errors.py`` -- plain exceptions this package raises; the API layer maps
  them to HTTP status codes (same pattern as the rest of ``app/services/``,
  e.g. ``dispatch/service.py`` raising ``ValueError`` for "not found").
- ``eligibility.py`` -- the placeholder ``AwardCostMatrix`` generation that
  bridges the AwardCostMatrix boundary for newly admin-entered data (see
  that module's docstring for the full rationale).
- ``workers.py`` / ``shifts.py`` -- single-row create/update, and the
  full-replace bulk-upsert logic for each entity.
- ``upload.py`` -- parses the multipart upload (xlsx/csv) into validated
  rows and orchestrates ``POST /admin/data/upload``.
- ``template.py`` -- builds the ``.xlsx`` template ``GET /admin/data/template``
  returns.
"""
