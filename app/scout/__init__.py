"""The Scout: standalone trader-discovery service. See docs/SCOUT_DESIGN.md.

Deployed as its own Railway service, reading the shared Postgres database -
never imports app/paper/* (design flag 1). Freely reuses this project's
already-proven pure math (app/risk/calibration.py's wilson_interval,
app/optimization/clv.py's clv_value) rather than re-deriving it.
"""
