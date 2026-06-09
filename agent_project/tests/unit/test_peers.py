"""Peer-validation tests (DCF spec Issue #7) — fully offline via injected fetch."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from agent_project.graphs.workflows.dcf.peers import (
    validate_assumptions_against_peers,
)


def _fake_fetch(responses: dict[str, list[dict]]):
    """Build a fetch(path, api_key) that matches by path prefix."""
    def fetch(path: str, api_key: str):
        for prefix, payload in responses.items():
            if path.startswith(prefix):
                return payload
        return []
    return fetch


def test_no_api_key_returns_empty():
    res = validate_assumptions_against_peers(
        "AMZN", {"fcff_margin": 0.05}, api_key="", fetch=_fake_fetch({}),
    )
    assert res == {"peers": [], "rows": [], "flags": []}


def test_no_peers_returns_empty():
    fetch = _fake_fetch({"stock-peers": []})
    res = validate_assumptions_against_peers(
        "AMZN", {"fcff_margin": 0.05}, api_key="k", fetch=fetch,
    )
    assert res["rows"] == [] and res["flags"] == []


def test_below_peer_range_flags_warn():
    """AMZN fcff_margin 5% vs peers ~20-30% → below peers, warn flag."""
    fetch = _fake_fetch({
        "stock-peers": [{"symbol": "MSFT"}, {"symbol": "GOOGL"}, {"symbol": "AAPL"}],
        "ratios-ttm?symbol=MSFT": [{"freeCashFlowMarginTTM": 0.30, "operatingProfitMarginTTM": 0.44}],
        "ratios-ttm?symbol=GOOGL": [{"freeCashFlowMarginTTM": 0.22, "operatingProfitMarginTTM": 0.30}],
        "ratios-ttm?symbol=AAPL": [{"freeCashFlowMarginTTM": 0.26, "operatingProfitMarginTTM": 0.31}],
        "financial-growth?symbol=MSFT": [{"revenueGrowth": 0.15}],
        "financial-growth?symbol=GOOGL": [{"revenueGrowth": 0.13}],
        "financial-growth?symbol=AAPL": [{"revenueGrowth": 0.05}],
    })
    res = validate_assumptions_against_peers(
        "AMZN",
        {"fcff_margin": 0.05, "revenue_growth": 0.14},
        api_key="k", fetch=fetch,
    )
    fcff_row = next(r for r in res["rows"] if r["metric"] == "fcff_margin")
    assert fcff_row["status"] == "below peers"
    assert fcff_row["peer_min"] == 0.22 and fcff_row["peer_max"] == 0.30
    assert any(f["field"] == "fcff_margin" and f["severity"] == "warn" for f in res["flags"])
    # revenue_growth 14% is within [5%, 15%] → no flag
    rg_row = next(r for r in res["rows"] if r["metric"] == "revenue_growth")
    assert rg_row["status"] == "within range"


def test_within_range_no_flag():
    fetch = _fake_fetch({
        "stock-peers": [{"symbol": "MSFT"}, {"symbol": "GOOGL"}],
        "ratios-ttm?symbol=MSFT": [{"freeCashFlowMarginTTM": 0.30}],
        "ratios-ttm?symbol=GOOGL": [{"freeCashFlowMarginTTM": 0.20}],
        "financial-growth?symbol=MSFT": [{"revenueGrowth": 0.15}],
        "financial-growth?symbol=GOOGL": [{"revenueGrowth": 0.13}],
    })
    res = validate_assumptions_against_peers(
        "X", {"fcff_margin": 0.25}, api_key="k", fetch=fetch,
    )
    assert all(f["severity"] != "warn" for f in res["flags"])
    assert next(r for r in res["rows"] if r["metric"] == "fcff_margin")["status"] == "within range"


def test_single_peer_metric_skipped():
    """Need >=2 observations to define a range."""
    fetch = _fake_fetch({
        "stock-peers": [{"symbol": "MSFT"}],
        "ratios-ttm?symbol=MSFT": [{"freeCashFlowMarginTTM": 0.30}],
        "financial-growth?symbol=MSFT": [{"revenueGrowth": 0.15}],
    })
    res = validate_assumptions_against_peers(
        "X", {"fcff_margin": 0.05}, api_key="k", fetch=fetch,
    )
    assert res["rows"] == []


def test_fetch_exception_degrades_gracefully():
    def boom(path: str, api_key: str):
        raise RuntimeError("network down")
    res = validate_assumptions_against_peers(
        "AMZN", {"fcff_margin": 0.05}, api_key="k", fetch=boom,
    )
    assert res == {"peers": [], "rows": [], "flags": []}
