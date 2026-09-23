"""Exceptions raised by the admin-data service layer (``app/services/admin_data/``).

Kept as plain exceptions, not ``HTTPException`` -- this package is DB-facing
service logic, not the HTTP layer, matching the rest of ``app/services/``
(e.g. ``app/services/dispatch/service.py`` raises a plain ``ValueError`` for
"RosterAssignment not found" and leaves the HTTP status mapping to its
caller). Each ``app/api/*.py`` router built on this package catches these
and maps them to the appropriate status code.
"""

from __future__ import annotations


class AdminDataError(Exception):
    """Base class for every error this package raises."""


class DuplicateEmployeeCodeError(AdminDataError):
    """A create/update/upload row's ``employee_code`` collides with an
    existing ``Worker`` (or another row in the same upload). Maps to 409.
    """

    def __init__(self, employee_code: str | None):
        self.employee_code = employee_code
        super().__init__(f"employee_code {employee_code!r} is already in use")


class WorkerNotFoundError(AdminDataError):
    """No ``Worker`` with the given id. Maps to 404."""

    def __init__(self, worker_id: int):
        self.worker_id = worker_id
        super().__init__(f"Worker {worker_id} not found")


class ShiftNotFoundError(AdminDataError):
    """No ``Shift`` with the given id. Maps to 404."""

    def __init__(self, shift_id: int):
        self.shift_id = shift_id
        super().__init__(f"Shift {shift_id} not found")


class SiteNotFoundError(AdminDataError):
    """A ``home_site_id``/``site_id``/``home_site_code``/``site_code``
    reference (create/update body, or an upload row) does not resolve to an
    existing ``Site``. Maps to 422 -- this is a bad reference in the
    request, not a missing resource being looked up by its own id.
    """

    def __init__(self, *, site_id: int | None = None, site_code: str | None = None):
        self.site_id = site_id
        self.site_code = site_code
        super().__init__(f"Site not found (id={site_id!r}, code={site_code!r})")


class MultiStopNotSupportedError(AdminDataError):
    """Raised whenever a caller tries to create a Shift, or flip an
    existing one, to ``is_multi_stop=True`` through this admin data-entry
    surface. Jobs (the stops a multi-stop shift needs) are out of scope
    this round -- see ``app/api/shifts.py`` and the task brief's "Out of
    scope" list -- so a multi-stop shift created here would have no way to
    ever get stops. Maps to 422.
    """

    def __init__(self):
        super().__init__(
            "multi-stop shifts need Job rows, which this endpoint doesn't manage "
            "yet -- create single-site shifts here, or use the existing "
            "seed/DB path for multi-stop ones"
        )


class UploadValidationError(AdminDataError):
    """One or more rows in a ``POST /admin/data/upload`` submission failed
    validation. Carries every error found, not just the first, per that
    endpoint's "return every validation error found" contract -- so the
    whole upload can be fixed in one pass rather than one rejected row at a
    time. Maps to 422.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"{len(errors)} validation error(s): {errors!r}")
