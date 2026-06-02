"""Unique, stable, lineage-aware identifiers for DCF runs.

Historically the KG ``run_id`` was ``parent_step_id`` (the activity-stream step
id, e.g. ``"workflow_dcf"``) — identical across every run, so each rerun
*overwrote* the previous run's nodes. Run identity now gets its own id, distinct
from the activity step id, so runs ACCUMULATE and can be compared.

Format
------
Root run:   ``{TICKER}_{YYYYMMDDHHMMSS}_{rand4}``
Derived:    ``{TICKER}_{YYYYMMDDHHMMSS}_{rand4}__from_{parent8}``

The ``__from_{parent8}`` suffix encodes lineage (the first 8 chars of the
parent run's id) so a clone/rerun is visibly tied to its origin without needing
an edge lookup. ``parent8`` is the parent's leading 8 chars after the ticker.
"""

from __future__ import annotations

import secrets
import time

__all__ = ["new_run_id", "parse_lineage"]


def _rand4() -> str:
    return secrets.token_hex(2)  # 4 hex chars


def new_run_id(ticker: str, *, parent_run_id: str | None = None) -> str:
    """Mint a fresh run id. When ``parent_run_id`` is given, encode lineage."""
    tk = (ticker or "RUN").strip().upper() or "RUN"
    ts = time.strftime("%Y%m%d%H%M%S", time.localtime())
    base = f"{tk}_{ts}_{_rand4()}"
    if parent_run_id:
        return f"{base}__from_{_fingerprint(parent_run_id)}"
    return base


def _fingerprint(run_id: str) -> str:
    """Short, collision-resistant fingerprint of a parent run id.

    Uses the run id's own unique tail (timestamp + rand) rather than the
    leading ticker chars, so two same-day reruns of the same ticker don't
    collapse to the same parent fingerprint.
    """
    # Legacy/non-standard ids: hash them. Standard ids: keep the time+rand tail.
    parts = run_id.split("_")
    if len(parts) >= 3:
        return f"{parts[-2][-6:]}{parts[-1][:4]}"  # ts tail + rand
    import hashlib  # noqa: PLC0415
    return hashlib.sha1(run_id.encode()).hexdigest()[:10]


def parse_lineage(run_id: str) -> str | None:
    """Return the parent fingerprint embedded in a derived run id, else None."""
    if "__from_" in run_id:
        return run_id.split("__from_", 1)[1] or None
    return None
