"""Tests for explicit buyback/SBC assumption surfacing."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf import fundamentals as fundamentals_mod
from agent_project.graphs.workflows.dcf.memo import _backfill_capital_mechanics
from agent_project.graphs.workflows.dcf.review import review_assumptions_node


def test_fmp_fundamentals_extract_buyback_yield_and_sbc(monkeypatch):
    """Cash-flow data should become explicit capital-mechanics assumptions."""

    def fake_fmp_get_json(path: str, api_key: str):  # noqa: ARG001
        if path.startswith("income-statement"):
            return [{
                "date": "2026-09-30",
                "revenue": 400_000_000_000,
                "incomeBeforeTax": 120_000_000_000,
                "incomeTaxExpense": 18_000_000_000,
                "interestExpense": 0,
                "weightedAverageShsOutDil": 15_000_000_000,
            }]
        if path.startswith("balance-sheet-statement"):
            return [{
                "longTermDebt": 100_000_000_000,
                "shortTermDebt": 10_000_000_000,
                "cashAndShortTermInvestments": 80_000_000_000,
                "totalDebt": 110_000_000_000,
            }]
        if path.startswith("cash-flow-statement"):
            return [{
                "freeCashFlow": 95_000_000_000,
                "commonStockRepurchased": -90_000_000_000,
                "commonStockIssued": 5_000_000_000,
                "stockBasedCompensation": 12_000_000_000,
            }]
        if path.startswith("profile"):
            return [{
                "marketCap": 3_000_000_000_000,
                "price": 200.0,
                "sector": "Technology",
                "industry": "Consumer Electronics",
            }]
        if path.startswith("key-metrics"):
            return [{"beta": 1.05}]
        return []

    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.setattr(fundamentals_mod, "_fmp_get_json", fake_fmp_get_json)

    out = fundamentals_mod._fetch_fundamentals_fmp("AAPL")

    assert round(out["buyback_yield"]["value"], 4) == round(85_000_000_000 / 3_000_000_000_000, 4)
    assert out["buyback_yield"]["source"] == "fmp"
    assert out["sbc_pct_revenue"]["value"] == 0.03
    assert "stock-based compensation" in out["sbc_pct_revenue"]["evidence"].lower()


def test_memo_backfills_capital_mechanics_into_assumptions_and_memo():
    assumptions = {
        "revenue_growth": 0.12,
        "fcff_margin": 0.22,
        "terminal_growth": 0.025,
        "tax_rate": 0.18,
    }
    provenance = {}
    memo_dict = {
        "proposals": [
            {"field": "revenue_growth", "value": 0.12, "evidence_refs": ["ev_web"], "confidence": 0.8},
        ],
        "evidence_refs": ["ev_web"],
    }

    _backfill_capital_mechanics(
        assumptions=assumptions,
        provenance=provenance,
        fundamentals={
            "buyback_yield": {
                "value": 0.035,
                "source": "fmp",
                "field": "net share repurchases / market cap",
                "evidence": "FMP net share repurchases divided by market cap.",
                "confidence": 0.82,
            },
            "sbc_pct_revenue": {
                "value": 0.04,
                "source": "fmp",
                "field": "stockBasedCompensation / revenue",
                "evidence": "FMP stock-based compensation divided by revenue.",
                "confidence": 0.82,
            },
        },
        evidence_pack={
            "items": [
                {
                    "evidence_id": "ev_fmp_buyback_yield",
                    "kind": "structured_fundamental",
                    "field": "buyback_yield",
                },
                {
                    "evidence_id": "ev_fmp_sbc_pct_revenue",
                    "kind": "structured_fundamental",
                    "field": "sbc_pct_revenue",
                },
            ],
        },
        memo_dict=memo_dict,
    )

    assert assumptions["buyback_yield"] == 0.035
    assert assumptions["sbc_pct_revenue"] == 0.04
    assert provenance["buyback_yield"]["evidence_refs"] == ["ev_fmp_buyback_yield"]
    assert any(p["field"] == "buyback_yield" for p in memo_dict["proposals"])
    assert "ev_fmp_sbc_pct_revenue" in memo_dict["evidence_refs"]


def test_review_edit_can_add_optional_capital_fields(monkeypatch):
    """HITL edits may add optional fields that were missing from the memo."""

    def fake_interrupt(payload):  # noqa: ARG001
        return {
            "action": "edit",
            "assumptions": {
                "buyback_yield": 0.035,
                "sbc_pct_revenue": 0.04,
            },
        }

    import agent_project.graphs.workflows.dcf.review as review_mod

    monkeypatch.setattr(review_mod, "interrupt", fake_interrupt)

    result = review_assumptions_node({
        "parent_step_id": "test",
        "assumption_review_mode": True,
        "assumptions": {
            "revenue_growth": 0.10,
            "fcff_margin": 0.20,
            "terminal_growth": 0.025,
            "tax_rate": 0.20,
        },
        "assumption_provenance": {},
        "evidence_pack": {"items": []},
    })

    assert result["assumptions"]["buyback_yield"] == 0.035
    assert result["assumptions"]["sbc_pct_revenue"] == 0.04
    assert result["assumption_provenance"]["buyback_yield"]["user_edited"] is True
