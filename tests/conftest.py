"""Point the whole test run at a separate database and Redis index.

The integration tests TRUNCATE every app table after each test (see
``tests/integration/conftest.py``). Against the dev database that wiped the
seeded demo, so every test run now uses its own database: ``TEST_DATABASE_URL``
if set, otherwise the dev ``DATABASE_URL`` with ``_test`` appended to the
database name (``orl`` -> ``orl_test``). Redis moves to index 15 (or
``TEST_REDIS_URL``), so test jobs never land on a dev arq worker's queue.

This has to run before anything imports ``app.core.settings``: ``settings``
and ``app.core.db.engine`` are module-level singletons built at import time.
pytest imports this root conftest before any test module, so setting the
environment here is early enough.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

_DEFAULT_DATABASE_URL = "postgresql+asyncpg://orl:orl@localhost:5432/orl"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_TEST_REDIS_DB = 15


def _configured(name: str, default: str) -> str:
    dotenv = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    return os.environ.get(name) or dotenv.get(name) or default


def _test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    url = make_url(explicit or _configured("DATABASE_URL", _DEFAULT_DATABASE_URL))
    if not explicit and not (url.database or "").endswith("_test"):
        url = url.set(database=f"{url.database}_test")
    if not (url.database or "").endswith("_test"):
        raise RuntimeError(
            f"Refusing to run tests against database {url.database!r}: the integration tests "
            "truncate every table, so the test database name must end in '_test'."
        )
    return url.render_as_string(hide_password=False)


def _test_redis_url() -> str:
    explicit = os.environ.get("TEST_REDIS_URL")
    if explicit:
        return explicit
    base = _configured("REDIS_URL", _DEFAULT_REDIS_URL)
    return (
        f"{base.rsplit('/', 1)[0]}/{_TEST_REDIS_DB}"
        if base.count("/") >= 3
        else f"{base}/{_TEST_REDIS_DB}"
    )


os.environ["DATABASE_URL"] = _test_database_url()
os.environ["REDIS_URL"] = _test_redis_url()
