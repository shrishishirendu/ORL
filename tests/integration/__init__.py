"""DB (and, for the queue tests, Redis)-backed integration tests.

Separated from the top-level ``tests/`` package (whose tests are all pure/
in-memory -- see ``tests/test_models.py``, ``test_rostering.py``,
``test_dispatch.py``, ``test_reoptimization.py``, ``test_health.py``) so
that requiring a live Postgres (and, for ``test_e2e_queue.py``, a live
Redis + a real ``arq`` worker loop) is scoped to exactly the tests that
actually need it, via this package's own ``conftest.py``.
"""
