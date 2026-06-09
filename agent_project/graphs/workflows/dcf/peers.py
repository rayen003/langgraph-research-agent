"""Peer-based assumption validation (DCF spec Issue #7).

Assumptions are otherwise evaluated in isolation against static profile bands.
This layer pulls a small set of FMP peers, reads their actual TTM growth/margins,
and compares the model's assumptions against the observed peer range. A value
outside the peer range is flagged (warn) so the user sees *"AMZN fcff_margin 5.0%
vs peer range 18%-31% — below peers"* instead of silently trusting a number that
no comparable company exhibits.

Best-effort and non-blocking: any fetch failure, missing API key, or empty peer
set degrades to an empty result. It never raises and never clamps a value — it
only annotates. Network access is injected (``fetch``) so it is fully testable
offline.
"""

from __future__ import annotations

import logging
import os
import statistics
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Cap peers to keep the validation cheap (1 + 2*N FMP calls).
MAX_PEERS = 5

# Model assumption field → the peer metric it is validated against.
# Both sides use the same FMP definition so the comparison is apples-to-apples.
_METRIC_LABELS: dict[str, str] = {
    "revenue_growth": "Revenue growth",
    "fcff_margin": "FCFF margin",
    "operating_margin": "Operating margin",
}

FetchFn = Callable[[str, str], list[dict[str, Any]]]


def _default_fetch(path: str, api_key: str) -> list[dict[str, Any]]:
    """Lazy indirection to the fundamentals FMP client (avoids import cycle)."""
    from .fundamentals import _fmp_get_json  # noqa: PLC0415

    return _fmp_get_json(path, api_key)


def _peer_symbols(ticker: str, api_key: str, fetch: FetchFn) -> list[str]:
    """Resolve up to MAX_PEERS comparable tickers from FMP."""
    rows = fetch(f"stock-peers?symbol={ticker}", api_key)
    symbols: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        # FMP shapes vary: a flat {symbol} list, or one row with a peersList.
        peer_list = row.get("peersList")
        if isinstance(peer_list, list):
            symbols.extend(str(s) for s in peer_list if s)
        sym = row.get("symbol")
        if isinstance(sym, str) and sym and sym.upper() != ticker.upper():
            symbols.append(sym)
    # De-dupe, drop self, cap.
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        su = s.upper()
        if su == ticker.upper() or su in seen:
            continue
        seen.add(su)
        out.append(s)
        if len(out) >= MAX_PEERS:
            break
    return out


def _first_num(d: dict[str, Any], *keys: str) -> float | None:
    """Return the first present numeric value among *keys* (FMP key drift)."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _peer_metrics(symbol: str, api_key: str, fetch: FetchFn) -> dict[str, float]:
    """Fetch one peer's TTM growth + margins. Missing metrics are omitted."""
    metrics: dict[str, float] = {}

    ratios = fetch(f"ratios-ttm?symbol={symbol}", api_key)
    if ratios and isinstance(ratios[0], dict):
        r = ratios[0]
        fcff = _first_num(r, "freeCashFlowMarginTTM", "freeCashFlowMargin")
        if fcff is not None:
            metrics["fcff_margin"] = fcff
        opm = _first_num(r, "operatingProfitMarginTTM", "operatingProfitMargin")
        if opm is not None:
            metrics["operating_margin"] = opm

    growth = fetch(f"financial-growth?symbol={symbol}&period=annual&limit=1", api_key)
    if growth and isinstance(growth[0], dict):
        g = _first_num(growth[0], "revenueGrowth")
        if g is not None:
            metrics["revenue_growth"] = g

    return metrics


def _status(value: float, lo: float, hi: float) -> str:
    if value < lo:
        return "below peers"
    if value > hi:
        return "above peers"
    return "within range"


def validate_assumptions_against_peers(
    ticker: str,
    assumptions: dict[str, float],
    *,
    api_key: str | None = None,
    fetch: FetchFn | None = None,
    max_peers: int = MAX_PEERS,
) -> dict[str, Any]:
    """Compare model assumptions to the observed peer range.

    Returns ``{"peers": [...], "rows": [...], "flags": [...]}``. ``rows`` carry
    per-metric ``model`` / ``peer_min`` / ``peer_max`` / ``peer_median`` /
    ``status``; ``flags`` are warn-severity entries for out-of-range metrics.
    Empty (no peers / no key / fetch failure) → ``{}``-ish empty result.
    """
    api_key = api_key or os.getenv("FMP_API_KEY") or os.getenv("FINANCIAL_MODELING_PREP_API_KEY") or ""
    fetch = fetch or _default_fetch
    empty: dict[str, Any] = {"peers": [], "rows": [], "flags": []}
    if not api_key or not ticker:
        return empty

    try:
        peers = _peer_symbols(ticker, api_key, fetch)[:max_peers]
        if not peers:
            return empty

        # Collect each metric's peer observations.
        observations: dict[str, list[float]] = {m: [] for m in _METRIC_LABELS}
        for sym in peers:
            pm = _peer_metrics(sym, api_key, fetch)
            for metric, val in pm.items():
                if metric in observations:
                    observations[metric].append(val)

        rows: list[dict[str, Any]] = []
        flags: list[dict[str, Any]] = []
        for metric, label in _METRIC_LABELS.items():
            obs = observations.get(metric) or []
            model_val = assumptions.get(metric)
            if len(obs) < 2 or not isinstance(model_val, (int, float)):
                continue  # need ≥2 peers to define a range
            lo, hi = min(obs), max(obs)
            med = statistics.median(obs)
            status = _status(float(model_val), lo, hi)
            rows.append({
                "metric": metric,
                "label": label,
                "model": float(model_val),
                "peer_min": lo,
                "peer_max": hi,
                "peer_median": med,
                "peer_n": len(obs),
                "status": status,
            })
            if status != "within range":
                flags.append({
                    "code": f"{metric}_outside_peer_range",
                    "severity": "warn",
                    "field": metric,
                    "value": float(model_val),
                    "expected": {"min": lo, "max": hi},
                    "profile": "peers",
                    "message": (
                        f"{label} {float(model_val)*100:.1f}% is {status} "
                        f"(peer range {lo*100:.1f}%–{hi*100:.1f}%, "
                        f"n={len(obs)}: {', '.join(peers)})."
                    ),
                })

        logger.info(
            "DCF peer validation ticker=%s peers=%d rows=%d flags=%d",
            ticker, len(peers), len(rows), len(flags),
        )
        return {"peers": peers, "rows": rows, "flags": flags}
    except Exception as exc:  # noqa: BLE001 — validation must never break a run
        logger.warning("DCF peer validation failed ticker=%s error=%s", ticker, exc)
        return empty
