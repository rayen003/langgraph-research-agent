"""Tests for evidence item persistence through HITL and citation drawer metadata."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.hitl_snapshot import build_hitl_snapshot
from agent_project.graphs.workflows.dcf.payload import dcf_source_metadata
from agent_project.graphs.workflows.dcf.sources import extract_evidence_items, resolve_evidence_item


def test_build_hitl_snapshot_reads_evidence_pack_items():
    web_item = {
        "evidence_id": "ev_web_0_evb_123",
        "kind": "web_excerpt",
        "source_tier": "news",
        "title": "Apple reports quarterly results",
        "url": "https://www.apple.com/newsroom/2026/04/apple-reports-second-quarter-results/",
        "text": "Apple posted quarterly revenue of $111.2 billion.",
    }
    snapshot = build_hitl_snapshot({"evidence_pack": {"items": [web_item]}})

    assert snapshot["evidence_items"][0]["title"] == "Apple reports quarterly results"
    assert snapshot["evidence_items"][0]["url"].startswith("https://www.apple.com/")


def test_dcf_source_metadata_returns_real_web_item_for_cited_id():
    web_item = {
        "evidence_id": "ev_web_2_evb_123",
        "kind": "web_excerpt",
        "source_tier": "news",
        "title": "Apple (AAPL) Q2 2026 earnings report",
        "url": "https://www.cnbc.com/2026/04/30/apple-aapl-q2-2026-earnings-report.html",
        "text": "Apple reported 17% revenue growth.",
    }
    payload = {
        "ticker": "AAPL",
        "_evidence_items": [web_item],
        "assumption_provenance": {
            "revenue_growth": {
                "reference": "ev_web_2_evb_123",
            },
        },
    }

    metadata = dcf_source_metadata(payload)
    by_id = {item["evidence_id"]: item for item in metadata["evidence_items"]}

    assert metadata["citation_map"]["1"] == "ev_web_2_evb_123"
    assert by_id["ev_web_2_evb_123"]["title"] == "Apple (AAPL) Q2 2026 earnings report"
    assert by_id["ev_web_2_evb_123"].get("inferred") is not True
    assert "not preserved" not in by_id["ev_web_2_evb_123"].get("text", "")


def test_resolve_evidence_item_matches_fmp_field_alias():
    fmp_item = {
        "evidence_id": "ev_fmp+fallback:yfinance_fcff_margin",
        "kind": "structured_fundamental",
        "field": "fcff_margin",
        "value": 0.237,
        "evidence": "FMP annual free cash flow margin.",
    }
    by_id = {fmp_item["evidence_id"]: fmp_item}

    resolved = resolve_evidence_item(
        "ev_fmp+fallback:yfinance_fcff_margin",
        by_id,
        all_items=[fmp_item],
    )

    assert resolved is not None
    assert resolved["evidence"] == "FMP annual free cash flow margin."


def test_extract_evidence_items_prefers_explicit_list_then_pack():
    pack_item = {"evidence_id": "ev_web_1", "title": "From pack"}
    explicit_item = {"evidence_id": "ev_web_2", "title": "Explicit"}

    from_pack = extract_evidence_items({"evidence_pack": {"items": [pack_item]}})
    from_explicit = extract_evidence_items({
        "evidence_items": [explicit_item],
        "evidence_pack": {"items": [pack_item]},
    })

    assert from_pack[0]["title"] == "From pack"
    assert from_explicit[0]["title"] == "Explicit"
