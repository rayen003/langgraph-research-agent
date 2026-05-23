"""Tests for _humanize_evidence_refs in payload.py."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.payload import _humanize_evidence_refs


def test_unknown_ref_returned_as_is():
    result = _humanize_evidence_refs(["unknown-id"], [])
    assert result == ["unknown-id"]


def test_filing_excerpt_formatted():
    items = [{"evidence_id": "e1", "kind": "filing_excerpt",
               "filing_type": "10-K", "section": "MD&A", "as_of": "2024-09-28"}]
    result = _humanize_evidence_refs(["e1"], items)
    assert result[0] == "10-K MD&A (2024-09-28)"


def test_structured_fundamental_formatted():
    items = [{"evidence_id": "e2", "kind": "structured_fundamental",
               "source": "FMP", "field": "revenue", "value": "391_035"}]
    result = _humanize_evidence_refs(["e2"], items)
    assert result[0] == "FMP: revenue=391_035"


def test_web_excerpt_truncated_at_60():
    long_title = "A" * 100
    items = [{"evidence_id": "e3", "kind": "web_excerpt", "title": long_title}]
    result = _humanize_evidence_refs(["e3"], items)
    assert result[0].startswith("web: ")
    assert len(result[0]) <= len("web: ") + 60


def test_document_excerpt_formatted():
    items = [{"evidence_id": "e4", "kind": "document_excerpt",
               "filename": "annual_report.pdf", "page": 12}]
    result = _humanize_evidence_refs(["e4"], items)
    assert result[0] == "doc: annual_report.pdf p.12"


def test_market_data_formatted():
    items = [{"evidence_id": "e5", "kind": "market_data", "field": "beta", "value": 1.24}]
    result = _humanize_evidence_refs(["e5"], items)
    assert result[0] == "market: beta=1.24"


def test_unknown_kind_falls_back():
    items = [{"evidence_id": "e6", "kind": "exotic_source"}]
    result = _humanize_evidence_refs(["e6"], items)
    assert result[0].startswith("exotic_source: ")


def test_multiple_refs_in_order():
    items = [
        {"evidence_id": "a", "kind": "market_data", "field": "price", "value": 180},
        {"evidence_id": "b", "kind": "market_data", "field": "beta", "value": 1.2},
    ]
    result = _humanize_evidence_refs(["b", "a"], items)
    assert result[0].startswith("market: beta")
    assert result[1].startswith("market: price")


def test_empty_refs_returns_empty():
    result = _humanize_evidence_refs([], [])
    assert result == []
