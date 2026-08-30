"""Analytics — pure, read-only derivations over the position ledger.

Nothing here writes, schedules, or touches the network: each function takes
already-loaded records and returns a value object, so the same code backs an
HTTP route, a test, and an offline analysis.
"""
