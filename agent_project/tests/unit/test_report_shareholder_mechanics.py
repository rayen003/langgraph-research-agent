"""Regression tests for shareholder mechanics report section (Steps 3–4)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.payload import summarize_dcf_payload
from agent_project.tests.helpers import build_test_payload


def test_shares_outstanding_not_formatted_as_dollars():
    payload = build_test_payload()
    summary = summarize_dcf_payload(payload)
    assert "| shares_outstanding | 15,500M |" in summary
    assert "| shares_outstanding | $15,500M |" not in summary


def test_shareholder_mechanics_section_from_valuation_fields():
    payload = build_test_payload(
        assumptions={
            **build_test_payload()["assumptions"],
            "buyback_yield": 0.03,
            "sbc_pct_revenue": 0.02,
            "fcff_margin": 0.25,
            "terminal_growth": 0.03,
        },
        valuation={
            **build_test_payload()["valuation"],
            "shares_initial": 15_500.0,
            "shares_end": 13_350.0,
            "buyback_yield": 0.03,
            "perpetual_buyback_yield": 0.025,
            "effective_terminal_growth": 0.055,
            "perpetual_buyback_cap_source": "fcff_yield_cap",
        },
    )
    summary = summarize_dcf_payload(payload)

    assert "## Shareholder Mechanics" in summary
    assert "Initial shares outstanding | 15,500M" in summary
    assert "Shares after 5-year buybacks | 13,350M" in summary
    assert "SBC drag on FCFF margin | −2.00% of revenue" in summary
    assert "25.00% → 23.00%" in summary
    assert "Perpetual buyback yield (terminal) | 2.50%" in summary
    assert "3.00% + 2.50% = 5.50%" in summary
    assert "Perpetual buyback capped by terminal FCF yield." in summary


def test_shareholder_mechanics_derives_shares_end_when_missing():
    payload = build_test_payload(
        assumptions={
            **build_test_payload()["assumptions"],
            "buyback_yield": 0.04,
            "shares_outstanding": 10_000.0,
        },
        valuation={
            **build_test_payload()["valuation"],
            "shares_initial": 10_000.0,
        },
    )
    summary = summarize_dcf_payload(payload)

    # 10_000 * (1 - 0.04)^5 ≈ 8,154M
    assert "## Shareholder Mechanics" in summary
    assert "Shares after 5-year buybacks | 8,154M" in summary


def test_references_use_human_titles_for_inferred_fmp_ids():
    payload = build_test_payload(
        assumption_provenance={
            "fcff_margin": {
                "source": "fmp",
                "reference": "ev_fmp+fallback:yfinance_fcff_margin",
            },
        },
        _evidence_items=[],
    )
    summary = summarize_dcf_payload(payload)

    assert "## References" in summary
    assert "FMP · fcff margin" in summary
    assert "ev_fmp+fallback:yfinance_fcff_margin" not in summary
