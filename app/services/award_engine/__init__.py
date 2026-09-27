"""ORL's side of the Award Engine service integration.

See ``docs/AWARD_ENGINE_CONTRACT.md`` (the HTTP contract) and
``docs/AWARD_INTEGRATION.md`` (why it is shaped this way):

- ``client.py`` -- typed httpx client for the engine's four endpoints.
- ``matrix.py`` -- fills ``AwardCostMatrix`` from ``POST /engine/cost-matrix``
  (engine rows, ``is_placeholder=False``).
- ``reconcile.py`` -- prices a solved Tier 1 roster with
  ``POST /engine/price-roster`` and records the exact figure next to the
  solver estimate.

Nothing in this package computes pay or re-derives an award rule: every $
figure it writes comes verbatim from an engine response. Tier 1 itself is
untouched -- it still just consumes ``AwardCostMatrix``.
"""
