"""Offline tests for the deterministic KG query executor + helpers.

These never touch the LLM or storage — they exercise ticker normalization,
fuzzy field matching, and the ancestor-path traversal that powers the UI route.
"""

from __future__ import annotations

import pytest

from kg import query as q


# ── Fake cache ────────────────────────────────────────────────────────────────


class FakeCache:
    """Mimics the bits of KGCache that _execute / helpers touch."""

    def __init__(self, nodes, edges):
        self._nodes = {n["id"]: n for n in nodes}
        self._edges_by_src: dict[str, list[dict]] = {}
        for e in edges:
            self._edges_by_src.setdefault(e["src_id"], []).append(e)

    def load_session(self, session_id):  # noqa: D401 - no-op for tests
        pass

    def load_ticker(self, ticker):  # noqa: D401 - no-op for tests
        pass

    def get_drivers(self, ticker):
        return [n for n in self._nodes.values()
                if n.get("ticker") == ticker and n.get("node_type") == "driver"]


def _node(nid, ticker, ntype, field, value, **kw):
    base = {
        "id": nid, "ticker": ticker, "node_type": ntype, "field": field,
        "value": value, "source": kw.get("source", "agent"),
        "confidence": kw.get("confidence", 1.0),
        "run_id": kw.get("run_id"), "updated_at": kw.get("updated_at", 0),
    }
    return base


def _edge(src, tgt, relation):
    return {"src_id": src, "tgt_id": tgt, "relation": relation}


@pytest.fixture
def aapl_graph(monkeypatch):
    """company → dcf_run → run_assumption(wacc) + run_output(implied_share_price)."""
    nodes = [
        _node("aapl-co", "AAPL", "company", "AAPL", "Apple Inc"),
        _node("aapl-run1", "AAPL", "dcf_run", "run", {"horizon_years": 10}, run_id="run1"),
        _node("aapl-wacc", "AAPL", "run_assumption", "wacc", 0.087, run_id="run1", updated_at=200),
        _node("aapl-rev", "AAPL", "run_assumption", "revenue_growth", 0.06, run_id="run1", updated_at=150),
        _node("aapl-isp", "AAPL", "run_output", "implied_share_price", 212.4, run_id="run1", updated_at=210),
        _node("aapl-drv", "AAPL", "driver", "services_mix", "rising", updated_at=90),
    ]
    edges = [
        _edge("aapl-co", "aapl-run1", "HAS_RUN"),
        _edge("aapl-run1", "aapl-wacc", "LOCKED_ASSUMPTION"),
        _edge("aapl-run1", "aapl-rev", "LOCKED_ASSUMPTION"),
        _edge("aapl-run1", "aapl-isp", "PRODUCES"),
        _edge("aapl-co", "aapl-drv", "HAS_DRIVER"),
    ]
    cache = FakeCache(nodes, edges)
    monkeypatch.setattr(q, "get_cache", lambda: cache)
    return cache


# ── Ticker normalization ──────────────────────────────────────────────────────


def test_alias_maps_company_name_to_ticker(aapl_graph):
    assert q._normalize_ticker("APPLE", aapl_graph) == "AAPL"
    assert q._normalize_ticker("Apple Inc", aapl_graph) == "AAPL"


def test_exact_ticker_passthrough(aapl_graph):
    assert q._normalize_ticker("AAPL", aapl_graph) == "AAPL"


def test_unknown_ticker_returns_cleaned_input(aapl_graph):
    assert q._normalize_ticker("zzz", aapl_graph) == "ZZZ"


# ── Fuzzy field matching ──────────────────────────────────────────────────────


def test_field_matches_substring_both_directions():
    assert q._field_matches("revenue", "revenue_growth")
    assert q._field_matches("revenue_growth", "revenue")
    assert not q._field_matches("wacc", "revenue_growth")


def test_field_matches_empty_query_is_wildcard():
    assert q._field_matches("", "anything")


def test_field_rank_orders_exact_first():
    assert q._field_rank("wacc", "wacc") == 0
    assert q._field_rank("rev", "revenue_growth") == 1
    assert q._field_rank("wacc", "revenue") == 2


# ── Traversal: ancestor path ──────────────────────────────────────────────────


def test_lookup_returns_traversal_edges_to_company(aapl_graph):
    res = q._execute(
        q.KGQuery(intent="lookup", ticker="AAPL", node_type="run_assumption", field="wacc"),
        session_id="s1",
    )
    assert len(res["matched_nodes"]) == 1
    assert res["matched_nodes"][0]["field"] == "wacc"
    # Route should trace company → run → wacc.
    edges = {(e["src_id"], e["tgt_id"]) for e in res["traversal_edges"]}
    assert ("aapl-co", "aapl-run1") in edges
    assert ("aapl-run1", "aapl-wacc") in edges
    assert "aapl-co" in res["traversal_path"]


def test_lookup_fuzzy_field_multiple_matches(aapl_graph):
    res = q._execute(
        q.KGQuery(intent="lookup", ticker="AAPL", node_type="", field="revenue"),
        session_id="s1",
    )
    fields = {n["field"] for n in res["matched_nodes"]}
    assert "revenue_growth" in fields


def test_lookup_miss_lists_available_fields(aapl_graph):
    res = q._execute(
        q.KGQuery(intent="lookup", ticker="AAPL", node_type="", field="nonexistent_zzz"),
        session_id="s1",
    )
    assert res["matched_nodes"] == []
    assert "Available fields" in res["answer"]
    assert "wacc" in res["answer"]


def test_list_drivers(aapl_graph):
    res = q._execute(
        q.KGQuery(intent="list_drivers", ticker="AAPL", node_type="", field=""),
        session_id="s1",
    )
    assert any(n["field"] == "services_mix" for n in res["matched_nodes"])
    assert "services_mix" in res["answer"]


def test_why_assumption_follows_outgoing_edges(aapl_graph):
    res = q._execute(
        q.KGQuery(intent="why_assumption", ticker="AAPL", node_type="", field="wacc"),
        session_id="s1",
    )
    assert any(n["field"] == "wacc" for n in res["matched_nodes"])
    assert len(res["traversal_edges"]) > 0


# ── Subgraph serialization (LLM-primary path inputs) ──────────────────────────


def test_serialize_subgraph_includes_nodes_and_edges(aapl_graph):
    nodes = q._subgraph_nodes(aapl_graph, "AAPL")
    text = q._serialize_subgraph(nodes, aapl_graph)
    assert "aapl-wacc" in text
    assert "wacc" in text
    assert "-[HAS_RUN]->" in text
    assert "-[LOCKED_ASSUMPTION]->" in text


def test_fmt_value_truncates_and_flattens():
    out = q._fmt_value({"direction": "positive", "conviction": "high"})
    assert out.startswith("{") and "\n" not in out
    assert len(q._fmt_value("x" * 500)) <= 120


# ── Keyword fallback (offline, no LLM) ────────────────────────────────────────


def test_keyword_lookup_matches_revenue(aapl_graph):
    rev = q._build_reverse_edges(aapl_graph)
    res = q._keyword_lookup("what is the revenue", "AAPL", aapl_graph, rev)
    fields = {n["field"] for n in res["matched_nodes"]}
    assert "revenue_growth" in fields
    # Route to company present.
    assert ("aapl-co", "aapl-run1") in {
        (e["src_id"], e["tgt_id"]) for e in res["traversal_edges"]
    }


def test_keyword_lookup_miss_lists_fields(aapl_graph):
    rev = q._build_reverse_edges(aapl_graph)
    res = q._keyword_lookup("zzznotathing", "AAPL", aapl_graph, rev)
    assert res["matched_nodes"] == []
    assert "Available fields" in res["answer"]


def test_build_traversal_dedups_edges(aapl_graph):
    rev = q._build_reverse_edges(aapl_graph)
    wacc = aapl_graph._nodes["aapl-wacc"]
    rev_node = aapl_graph._nodes["aapl-rev"]
    path, edges = q._build_traversal([wacc, rev_node], aapl_graph, rev)
    # HAS_RUN company->run appears once despite two children sharing it.
    has_run = [e for e in edges if e["relation"] == "HAS_RUN"]
    assert len(has_run) == 1
