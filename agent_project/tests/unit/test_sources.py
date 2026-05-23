"""Tests for DCF source registry and HITL provenance preservation."""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.payload import summarize_dcf_payload
from agent_project.graphs.workflows.dcf.review import review_assumptions_node
from agent_project.graphs.workflows.dcf.sources import (
    SourceRegistry,
    evidence_item_url,
    field_basis,
    format_reference_line,
    inline_cite_text,
    merge_hitl_provenance,
)
from agent_project.tests.helpers import build_test_payload


def test_source_registry_stable_numbering():
    items = [
        {"evidence_id": "a", "kind": "filing_excerpt", "filing_type": "10-K", "section": "MD&A", "as_of": "2024"},
        {"evidence_id": "b", "kind": "web_excerpt", "title": "News headline"},
    ]
    reg = SourceRegistry(items)
    assert reg.format_refs(["b", "a"]) == "[1][2]"
    assert reg.format_refs(["a", "a", "b"]) == "[1][2]"


def test_source_registry_references_section():
    items = [
        {
            "evidence_id": "ev1",
            "kind": "web_excerpt",
            "title": "Apple growth outlook",
            "url": "https://www.apple.com/newsroom/2026/04/apple-reports-second-quarter-results/",
            "published_date": "2026-04-01",
            "source_tier": "generic_web",
        },
    ]
    reg = SourceRegistry(items)
    reg.register("ev1")
    lines = reg.references_section_lines()
    assert lines[0] == "## References"
    assert any(line.startswith("- **[1]**") for line in lines)
    assert any("Apple growth outlook" in line and "https://www.apple.com" in line for line in lines)


def test_format_reference_line_sec_filing_link():
    item = {
        "evidence_id": "ev_sec_0000320193260000_risk_factors",
        "kind": "filing_excerpt",
        "filing_type": "10-Q",
        "section": "Risk Factors (Item 1A)",
        "as_of": "2026-05-01",
        "source_tier": "filing",
        "url": "https://www.sec.gov/Archives/edgar/data/0000320193/000032019326000013/aapl-20260328.htm",
    }
    line = format_reference_line(5, item["evidence_id"], item)
    assert line.startswith("- **[5]**")
    assert "[10-Q · Risk Factors (Item 1A)](" in line
    assert "sec.gov" in line


def test_format_reference_line_fmp_fallback_link():
    item = {
        "evidence_id": "ev_fmp_tax_rate",
        "kind": "structured_fundamental",
        "field": "tax_rate",
        "source": "fmp+yfinance",
        "ticker": "AAPL",
        "as_of": "2025-09-27",
        "source_tier": "structured_api",
        "evidence": "FMP effective tax rate from income statement.",
    }
    line = format_reference_line(4, item["evidence_id"], item)
    assert "financialmodelingprep.com/financial-summary/AAPL" in line
    assert "FMP · tax rate" in line


def test_merge_hitl_preserves_original_source():
    provenance = {
        "revenue_growth": {
            "source": "llm_memo",
            "evidence": "12% growth from iPhone momentum.",
            "evidence_refs": ["ev_web_1"],
            "confidence": 0.85,
        },
    }
    merged = merge_hitl_provenance(
        provenance,
        overrides={"revenue_growth": 0.12},
        original_assumptions={"revenue_growth": 0.12},
    )
    assert merged["revenue_growth"]["source"] == "llm_memo"
    assert merged["revenue_growth"]["evidence_refs"] == ["ev_web_1"]
    assert merged["revenue_growth"]["approved_by"] == "user"
    assert not merged["revenue_growth"].get("user_edited")


def test_merge_hitl_marks_user_edit():
    provenance = {
        "revenue_growth": {
            "source": "llm_memo",
            "evidence_refs": ["ev_web_1"],
        },
    }
    merged = merge_hitl_provenance(
        provenance,
        overrides={"revenue_growth": 0.15},
        original_assumptions={"revenue_growth": 0.12},
    )
    assert merged["revenue_growth"]["user_edited"] is True
    assert merged["revenue_growth"]["source"] == "llm_memo"


def test_field_basis_user_override_keeps_memo_context():
    prov = {
        "source": "llm_memo",
        "user_edited": True,
        "evidence": "Original memo rationale.",
    }
    basis = field_basis(
        "revenue_growth",
        prov,
        {"rationale": "Memo proposed 12% from product cycle strength."},
    )
    assert "User override at approval" in basis
    assert "Memo proposed" in basis or "product cycle" in basis


def test_extract_dcf_report_from_tool_pointer():
    from agent_project.graphs.workflows.dcf.payload import (
        extract_dcf_report_from_tool_pointer,
        summarize_dcf_payload,
    )

    payload = build_test_payload(model_validity="valid")
    report = summarize_dcf_payload(payload)
    pointer = json.dumps({
        "tool_result_id": "run_dcf_workflow_test123",
        "tool_name": "run_dcf_workflow",
        "summary": report,
        "dcf_report_verbatim": True,
    })
    extracted = extract_dcf_report_from_tool_pointer(pointer)
    assert extracted == report
    assert extracted is not None and extracted.startswith("# DCF Valuation:")


def test_review_edit_preserves_evidence_refs(monkeypatch):
    calls: list[dict] = []

    def fake_interrupt(payload):
        calls.append(payload)
        return {"action": "edit", "assumptions": {"revenue_growth": 0.14}}

    monkeypatch.setattr(
        "agent_project.graphs.workflows.dcf.review.interrupt",
        fake_interrupt,
    )
    state = {
        "assumption_review_mode": True,
        "parent_step_id": "test",
        "assumptions": {"revenue_growth": 0.12},
        "assumption_provenance": {
            "revenue_growth": {
                "source": "llm_memo",
                "evidence_refs": ["ev_sec_1"],
                "evidence": "Memo rationale.",
                "confidence": 0.85,
            },
        },
        "evidence_pack": {"items": []},
    }
    result = review_assumptions_node(state)
    prov = result["assumption_provenance"]["revenue_growth"]
    assert prov["source"] == "llm_memo"
    assert prov["evidence_refs"] == ["ev_sec_1"]
    assert prov["user_edited"] is True


def test_summarize_includes_basis_refs_and_references():
    payload = build_test_payload(
        assumption_provenance={
            "revenue_growth": {
                "source": "llm_memo",
                "evidence": "Growth from services expansion.",
                "evidence_refs": ["ev1"],
                "confidence": 0.8,
            },
            "wacc": {
                "source": "capm",
                "evidence": "CAPM-derived WACC.",
                "confidence": 0.78,
            },
        },
        assumption_memo={
            "proposals": [
                {
                    "field": "revenue_growth",
                    "rationale": "Services mix supports mid-teens growth.",
                    "confidence": 0.8,
                    "evidence_refs": ["ev1"],
                },
            ],
        },
        _evidence_items=[
            {
                "evidence_id": "ev1",
                "kind": "filing_excerpt",
                "filing_type": "10-K",
                "section": "MD&A",
                "as_of": "2024",
                "source_tier": "filing",
                "url": "https://www.sec.gov/Archives/edgar/data/0000320193/example/aapl-10k.htm",
            },
        ],
    )
    summary = summarize_dcf_payload(payload)
    assert "| Field | Value | Basis | Refs |" in summary
    assert "Source | Confidence" not in summary
    assert "## References" in summary
    assert "[1]" in summary
    assert "10-K · MD&A" in summary
    assert "sec.gov" in summary


def test_inline_cite_replaces_embedded_evidence_ids():
    registry = SourceRegistry([
        {"evidence_id": "ev_web_1", "kind": "web_excerpt", "title": "News"},
    ])
    text = "Revenue grew per web excerpt (ev_web_1) and filings (ev_web_1)."
    cited = inline_cite_text(text, registry)
    assert "ev_web_1" not in cited
    assert cited.count("[1]") >= 1


def test_summarize_includes_profile_news_and_inline_context():
    payload = build_test_payload(
        profile_meta={
            "company_name": "Apple Inc.",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "market_cap_usd": 4_000_000_000_000,
            "spot_price": 180.0,
            "currency": "USD",
        },
        profile="mega_cap_tech",
        company_state={
            "growth_outlook": "Strong demand per web excerpt (ev_news_1).",
            "evidence_refs": [{"evidence_id": "ev_news_1", "relevance": "earnings"}],
        },
        wacc_components={
            "method": "capm",
            "risk_free_rate": 0.045,
            "equity_risk_premium": 0.055,
            "beta": 1.2,
            "cost_of_equity": 0.111,
            "equity_weight": 0.95,
            "debt_weight": 0.05,
            "marginal_tax_rate": 0.15,
        },
        features={"beta": 1.2, "equity_value_usd": 3_000_000_000_000},
        wacc_sanity={
            "capm_wacc": 0.09,
            "implied_wacc": 0.07,
            "gap_bps": 200,
            "interpretation": "Model WACC above market-implied.",
        },
        _evidence_items=[
            {
                "evidence_id": "ev_profile_classification",
                "kind": "profile",
                "company_name": "Apple Inc.",
                "sector": "Technology",
                "industry": "Consumer Electronics",
            },
            {
                "evidence_id": "ev_news_1",
                "kind": "web_excerpt",
                "source_tier": "news",
                "title": "Apple beats estimates",
                "text": "Apple reported strong quarterly results.",
                "published_date": "2026-04-01",
            },
            {
                "evidence_id": "ev_feature_beta",
                "kind": "market_data",
                "field": "beta",
                "value": 1.2,
            },
        ],
    )
    summary = summarize_dcf_payload(payload)
    assert "## Company Profile" in summary
    assert "## Recent Developments" in summary
    assert "## Market Reconciliation" in summary
    assert "## Company Context" in summary
    assert "ev_news_1" not in summary
    assert "Apple beats estimates" in summary
    assert "WACC Decomposition" in summary
