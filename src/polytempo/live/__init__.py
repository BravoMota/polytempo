"""Live trading node: real-money execution against the Polymarket CLOB.

Everything under this package is execution-side only — analysis, strategies,
and market discovery are reused from the existing pipeline. The node runs in
``dry_run`` mode by default (no orders leave the process); ``live`` mode
requires explicit credentials and confirmation (see ``config.py``).
"""
