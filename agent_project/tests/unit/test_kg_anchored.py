"""Tests for KG 3-layer model — Layer 1 anchored facts.

Verifies:
    1. ANCHORED_TYPES set composition
    2. TTL classification per layer
    3. put() is no-op for existing anchored nodes (additive only)
    4. get_anchored_corpus filters + sorts correctly
"""

import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

import pytest

from agent_project.kg.cache import (
    TTL,
    ANCHORED_TYPES,
    KGCache,
    make_node_id,
)


# ---------------------------------------------------------------------------
# 1. ANCHORED_TYPES set
# ---------------------------------------------------------------------------

def test_anchored_types_includes_filing_and_news():
    assert "filing" in ANCHORED_TYPES
    assert "news_item" in ANCHORED_TYPES


def test_anchored_types_excludes_run_artifacts():
    """Layer 3 run_* are immutable but conceptually different (historical
    record, not anchored fact). Keep them out of ANCHORED_TYPES."""
    assert "dcf_run" not in ANCHORED_TYPES
    assert "run_assumption" not in ANCHORED_TYPES


def test_anchored_types_excludes_derived():
    """Layer 2 derived must be refreshable."""
    assert "thesis" not in ANCHORED_TYPES
    assert "company_synthesis" not in ANCHORED_TYPES
    assert "company_lifecycle" not in ANCHORED_TYPES


# ---------------------------------------------------------------------------
# 2. TTL per layer
# ---------------------------------------------------------------------------

def test_anchored_layer1_infinite_ttl():
    """filing + news_item must never expire."""
    assert TTL["filing"] == float("inf")
    assert TTL["news_item"] == float("inf")


def test_refreshable_layer1_finite_ttl():
    """market_metric_* refresh — current snapshots, not historical facts."""
    assert TTL["market_metric_price"] == 3600.0
    assert TTL["market_metric_fund"] == 86400.0


def test_layer2_derived_finite_ttl():
    """Layer 2 derived inferences are rebuildable."""
    assert TTL["thesis"] < float("inf")
    assert TTL["company_synthesis"] < float("inf")
    assert TTL["company_lifecycle"] < float("inf")


def test_layer3_runs_infinite_ttl():
    """Run artifacts are historical record — never expire."""
    assert TTL["dcf_run"] == float("inf")
    assert TTL["run_assumption"] == float("inf")
    assert TTL["run_output"] == float("inf")
    assert TTL["run_scenario"] == float("inf")


# ---------------------------------------------------------------------------
# 3. put() additive guard for anchored types
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_cache(monkeypatch):
    """Fresh KGCache that doesn't hit SQLite (storage stubbed)."""
    import agent_project.kg.cache as cache_mod

    class _StubStorage:
        def upsert_kg_node(self, **_kwargs): pass
        def list_kg_nodes(self, **_kwargs): return []
        def list_kg_edges(self, **_kwargs): return []
        def insert_kg_edge(self, **_kwargs): pass
        def delete_kg_node(self, **_kwargs): pass
        def delete_kg_edge(self, **_kwargs): pass
        def insert_kg_traversal(self, **_kwargs): pass

    monkeypatch.setattr(cache_mod, "storage", _StubStorage())
    return KGCache()


def test_anchored_put_first_time_writes(isolated_cache):
    """First put creates the node."""
    node = isolated_cache.put(
        ticker="AAPL", node_type="filing", field="10-K::2024-09-28::body",
        value={"text": "first version"}, source="sec_edgar", confidence=0.95,
    )
    assert node["value"] == {"text": "first version"}


def test_anchored_put_second_time_noop(isolated_cache):
    """Second put with same ID does NOT overwrite — facts are immutable."""
    isolated_cache.put(
        ticker="AAPL", node_type="filing", field="10-K::2024-09-28::body",
        value={"text": "original"}, source="sec_edgar", confidence=0.95,
    )
    # Same field, different value — should be ignored
    isolated_cache.put(
        ticker="AAPL", node_type="filing", field="10-K::2024-09-28::body",
        value={"text": "replaced"}, source="sec_edgar", confidence=0.95,
    )
    node = isolated_cache.get("AAPL", "filing", "10-K::2024-09-28::body")
    assert node["value"] == {"text": "original"}, "Anchored fact was overwritten — additive guard broken"


def test_anchored_put_news_item_noop_on_existing(isolated_cache):
    """news_item additive guard works."""
    isolated_cache.put(
        ticker="AAPL", node_type="news_item", field="2024-01-15::abc123",
        value={"title": "v1"}, source="web_search", confidence=0.7,
    )
    isolated_cache.put(
        ticker="AAPL", node_type="news_item", field="2024-01-15::abc123",
        value={"title": "v2"}, source="web_search", confidence=0.7,
    )
    node = isolated_cache.get("AAPL", "news_item", "2024-01-15::abc123")
    assert node["value"] == {"title": "v1"}


def test_non_anchored_put_overwrites(isolated_cache):
    """Layer 2 derived types DO update on re-put (refreshable)."""
    isolated_cache.put(
        ticker="AAPL", node_type="thesis", field="full",
        value={"version": 1}, source="llm", confidence=0.8,
    )
    isolated_cache.put(
        ticker="AAPL", node_type="thesis", field="full",
        value={"version": 2}, source="llm", confidence=0.8,
    )
    node = isolated_cache.get("AAPL", "thesis", "full")
    assert node["value"] == {"version": 2}, "Derived node should update — additive guard misfired"


# ---------------------------------------------------------------------------
# 4. get_anchored_corpus
# ---------------------------------------------------------------------------

def test_get_anchored_corpus_returns_only_anchored(isolated_cache):
    isolated_cache.put(
        ticker="AAPL", node_type="filing", field="10-K::2024-09-28::body",
        value={"x": 1}, source="sec_edgar", confidence=0.95,
    )
    isolated_cache.put(
        ticker="AAPL", node_type="news_item", field="2024-01-15::abc",
        value={"x": 2}, source="web", confidence=0.7,
    )
    isolated_cache.put(
        ticker="AAPL", node_type="thesis", field="full",
        value={"x": 3}, source="llm", confidence=0.8,
    )
    corpus = isolated_cache.get_anchored_corpus("AAPL")
    types = {n["node_type"] for n in corpus}
    assert types == {"filing", "news_item"}
    assert len(corpus) == 2


def test_get_anchored_corpus_filters_by_type(isolated_cache):
    isolated_cache.put(
        ticker="AAPL", node_type="filing", field="f1",
        value={}, source="sec", confidence=0.9,
    )
    isolated_cache.put(
        ticker="AAPL", node_type="news_item", field="n1",
        value={}, source="web", confidence=0.7,
    )
    only_news = isolated_cache.get_anchored_corpus("AAPL", node_types={"news_item"})
    assert len(only_news) == 1
    assert only_news[0]["node_type"] == "news_item"


def test_get_anchored_corpus_filters_by_since_ts(isolated_cache):
    isolated_cache.put(
        ticker="AAPL", node_type="news_item", field="old",
        value={}, source="web", confidence=0.7,
    )
    # Manually rewind the timestamp on the first node
    old_node = isolated_cache.get("AAPL", "news_item", "old")
    old_node["created_at"] = time.time() - 86400 * 30  # 30 days ago

    isolated_cache.put(
        ticker="AAPL", node_type="news_item", field="new",
        value={}, source="web", confidence=0.7,
    )

    # Only items from last 24h
    recent = isolated_cache.get_anchored_corpus(
        "AAPL", since_ts=time.time() - 86400,
    )
    fields = {n["field"] for n in recent}
    assert "new" in fields
    assert "old" not in fields


def test_get_anchored_corpus_sorted_newest_first(isolated_cache):
    isolated_cache.put(
        ticker="AAPL", node_type="news_item", field="a",
        value={}, source="web", confidence=0.7,
    )
    isolated_cache.get("AAPL", "news_item", "a")["created_at"] = 1000.0
    isolated_cache.put(
        ticker="AAPL", node_type="news_item", field="b",
        value={}, source="web", confidence=0.7,
    )
    isolated_cache.get("AAPL", "news_item", "b")["created_at"] = 2000.0
    corpus = isolated_cache.get_anchored_corpus("AAPL")
    assert corpus[0]["field"] == "b"
    assert corpus[1]["field"] == "a"


def test_get_anchored_corpus_filters_by_ticker(isolated_cache):
    isolated_cache.put(
        ticker="AAPL", node_type="news_item", field="aapl-1",
        value={}, source="web", confidence=0.7,
    )
    isolated_cache.put(
        ticker="MSFT", node_type="news_item", field="msft-1",
        value={}, source="web", confidence=0.7,
    )
    aapl_only = isolated_cache.get_anchored_corpus("AAPL")
    assert len(aapl_only) == 1
    assert aapl_only[0]["ticker"] == "AAPL"
