"""Unit tests for the DCF expectations-first block builders.

Anchored on the real ``chat_a9jdpef7`` payload (AAPL, score 10/11) so the
tests fail loudly if upstream payload shapes change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphs.workflows.deck.adapters.dcf_expectations_blocks import (
    build_capital_flow,
    build_debate,
    build_decision,
    build_expectations_blocks,
    build_expectations_table,
    build_three_box,
    build_variable_impact,
    _consolidated_market_signals,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def aapl_payload() -> dict:
    """Rich AAPL run — has reverse-DCF, divergences, thesis, company_state."""
    path = (
        Path(__file__).resolve().parents[2]
        / "runs" / "chat_a9jdpef7" / "dcf_output.json"
    )
    if not path.exists():
        pytest.skip(f"Fixture run not present at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def poor_payload() -> dict:
    """Synthetic minimal payload — only assumptions + valuation."""
    return {
        "ticker": "TEST",
        "assumptions": {
            "wacc": 0.08,
            "revenue_growth": 0.10,
            "fcff_margin": 0.20,
            "terminal_growth": 0.02,
            "buyback_yield": 0.0,
            "shares_outstanding": 1000.0,
        },
        "valuation": {
            "implied_share_price": 100.0,
            "current_price": 110.0,
            "enterprise_value": 200000.0,
            "equity_value": 180000.0,
            "terminal_pv": 130000.0,
            "shares_initial": 1000.0,
            "shares_end": 1000.0,
            "effective_terminal_growth": 0.02,
        },
        "sensitivity_table": [
            {"wacc": 0.07, "terminal_growth": 0.02, "implied_share_price": 130.0},
            {"wacc": 0.08, "terminal_growth": 0.02, "implied_share_price": 100.0},
            {"wacc": 0.09, "terminal_growth": 0.02, "implied_share_price": 80.0},
        ],
    }


# ---------------------------------------------------------------------------
# _consolidated_market_signals
# ---------------------------------------------------------------------------


def test_consolidated_signals_extracts_growth_gap(aapl_payload):
    signals = _consolidated_market_signals(aapl_payload)
    assert signals["model_wacc"] == pytest.approx(0.078979, rel=1e-3)
    assert signals["implied_wacc"] == pytest.approx(0.0703, rel=1e-3)
    assert signals["wacc_gap_bps"] == pytest.approx(86.0, abs=1)
    # growth_vs_implied divergence: modeled 0.195 vs implied 0.2895 → 9.45pp
    assert signals["growth_gap_pp"] == pytest.approx(9.45, abs=0.2)
    assert signals["plausibility_label"] == "aggressive"


def test_consolidated_signals_handles_missing_data(poor_payload):
    signals = _consolidated_market_signals(poor_payload)
    assert signals["implied_wacc"] is None
    assert signals["model_wacc"] is None
    assert signals["growth_gap_pp"] is None


# ---------------------------------------------------------------------------
# expectations_table
# ---------------------------------------------------------------------------


def test_expectations_table_built_on_rich_payload(aapl_payload):
    block = build_expectations_table(aapl_payload, "ref", "AAPL")
    assert block is not None
    assert block.kind == "expectations_table"
    rows = block.content["rows"]
    # WACC + growth + terminal rows all populated
    metric_names = {r["metric"] for r in rows}
    assert "Discount rate (WACC)" in metric_names
    assert "Near-term revenue growth" in metric_names
    assert "Effective terminal compounding" in metric_names


def test_expectations_table_skipped_when_no_reverse_dcf(poor_payload):
    block = build_expectations_table(poor_payload, "ref", "TEST")
    # Poor payload has no implied_wacc nor implied_growth → block skipped.
    assert block is None


# ---------------------------------------------------------------------------
# three_box
# ---------------------------------------------------------------------------


def test_three_box_emits_priced_assumed_required(aapl_payload):
    block = build_three_box(aapl_payload, "ref", "AAPL")
    assert block is not None
    assert block.kind == "three_box"
    c = block.content
    assert set(c.keys()) == {"priced", "assumed", "required"}
    assert c["priced"]["implied_wacc"] == "7.03%"
    assert c["assumed"]["revenue_growth_near"] == "19.5%"
    assert c["required"]["reconciliation_status"] == "structural_gap"
    assert len(c["required"]["divergences"]) >= 1


def test_three_box_skipped_without_market_context(poor_payload):
    block = build_three_box(poor_payload, "ref", "TEST")
    assert block is None


# ---------------------------------------------------------------------------
# debate
# ---------------------------------------------------------------------------


def test_debate_extracts_bull_bear(aapl_payload):
    block = build_debate(aapl_payload, "ref", "AAPL")
    assert block is not None
    assert block.kind == "debate"
    assert block.content["bull"]
    assert block.content["bear"]


def test_debate_skipped_when_thesis_empty(poor_payload):
    assert build_debate(poor_payload, "ref", "TEST") is None


# ---------------------------------------------------------------------------
# capital_flow
# ---------------------------------------------------------------------------


def test_capital_flow_derives_per_share_growth(aapl_payload):
    block = build_capital_flow(aapl_payload, "ref", "AAPL")
    assert block is not None
    assert block.kind == "capital_flow"
    c = block.content
    # business 2.5% + buyback 2.0% → effective per-share 4.5%
    assert c["display"]["business_growth"] == "2.5%"
    assert c["display"]["buyback_yield"] == "2.0%"
    assert c["display"]["effective_per_share"] == "4.5%"
    # Share count shrinks ~9.6% over horizon (15004 → 13563)
    assert c["shares_shrinkage_pct"] == pytest.approx(9.6, abs=0.5)


def test_capital_flow_skipped_without_buyback(poor_payload):
    # poor payload has buyback_yield = 0 → block skipped
    assert build_capital_flow(poor_payload, "ref", "TEST") is None


# ---------------------------------------------------------------------------
# variable_impact
# ---------------------------------------------------------------------------


def test_variable_impact_uses_exact_wacc_from_sensitivity(aapl_payload):
    block = build_variable_impact(aapl_payload, "ref", "AAPL")
    assert block is not None
    rows = block.content["rows"]
    wacc_row = next(r for r in rows if "WACC −100 bps" in r["variable"])
    assert wacc_row["method"].startswith("exact")
    # AAPL: WACC −100 bps from sensitivity grid → ~+42% (verified empirically)
    assert wacc_row["impact_pct"] > 20  # should be a large positive number


def test_variable_impact_produces_multiple_rows(aapl_payload):
    block = build_variable_impact(aapl_payload, "ref", "AAPL")
    assert block is not None
    # AAPL has buyback + terminal_margin_terminal + non-trivial near growth
    # → should produce all 4 rows.
    assert len(block.content["rows"]) == 4


def test_variable_impact_caveat_present(aapl_payload):
    block = build_variable_impact(aapl_payload, "ref", "AAPL")
    assert block is not None
    caveat = block.content["caveat"].lower()
    # Caveat must flag that magnitudes are directional, not point estimates.
    assert "directional" in caveat
    assert "one-variable" in caveat


def test_variable_impact_minimal_payload(poor_payload):
    # Poor payload has WACC + sensitivity_table → at least the WACC row builds.
    block = build_variable_impact(poor_payload, "ref", "TEST")
    assert block is not None
    assert any("WACC" in r["variable"] for r in block.content["rows"])


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------


def test_decision_built_on_rich_payload(aapl_payload):
    block = build_decision(aapl_payload, "ref", "AAPL")
    assert block is not None
    assert block.kind == "decision"
    c = block.content
    assert c["reconciliation_status"] == "structural_gap"
    assert c["framing_context"]["lifecycle_stage"]
    assert len(c["divergences"]) >= 1


def test_decision_skipped_when_no_market_signal(poor_payload):
    assert build_decision(poor_payload, "ref", "TEST") is None


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def test_build_expectations_blocks_full_set_on_rich_payload(aapl_payload):
    blocks = build_expectations_blocks(aapl_payload, "ref", "AAPL")
    kinds = [b.kind for b in blocks]
    # All six builders should produce on the most-complete AAPL run.
    assert kinds == [
        "three_box",
        "expectations_table",
        "variable_impact",
        "capital_flow",
        "debate",
        "decision",
    ]


def test_build_expectations_blocks_partial_on_poor_payload(poor_payload):
    blocks = build_expectations_blocks(poor_payload, "ref", "TEST")
    kinds = {b.kind for b in blocks}
    # Only the analytical blocks that don't need market signals should build.
    # poor_payload has WACC + sensitivity_table → variable_impact builds.
    # No reverse-DCF, no thesis, no buyback → other blocks skip.
    assert "variable_impact" in kinds
    assert "debate" not in kinds
    assert "capital_flow" not in kinds


def test_block_ids_stable_across_runs(aapl_payload):
    a = build_expectations_blocks(aapl_payload, "ref", "AAPL")
    b = build_expectations_blocks(aapl_payload, "ref", "AAPL")
    assert [x.block_id for x in a] == [x.block_id for x in b]


# ---------------------------------------------------------------------------
# Slide structural validation + fallback prettify
# ---------------------------------------------------------------------------


def test_missing_required_field_detects_three_box_without_columns():
    from graphs.workflows.deck.slides import _missing_required_field
    from graphs.workflows.deck.state import SlideContent

    blank = SlideContent(slide_id="x", layout="three_box", title="t")
    assert _missing_required_field(blank) == "columns"


def test_missing_required_field_clears_when_columns_populated():
    from graphs.workflows.deck.slides import _missing_required_field
    from graphs.workflows.deck.state import SlideContent

    ok = SlideContent(
        slide_id="x", layout="three_box", title="t",
        columns=[{"heading": "a", "bullets": ["b"]}],
    )
    assert _missing_required_field(ok) is None


def test_missing_required_field_detects_flow_diagram_without_steps():
    from graphs.workflows.deck.slides import _missing_required_field
    from graphs.workflows.deck.state import SlideContent

    blank = SlideContent(slide_id="x", layout="flow_diagram", title="t")
    assert _missing_required_field(blank) == "flow_steps"


def test_three_box_fallback_humanizes_keys(aapl_payload):
    """Deterministic fallback must not leak `revenue_growth_near:` raw keys."""
    from graphs.workflows.deck.slides import _deterministic_slide_content
    from graphs.workflows.deck.state import OutlineSlide

    blocks = build_expectations_blocks(aapl_payload, "ref", "AAPL")
    three_box_block = next(b for b in blocks if b.kind == "three_box")
    spec = OutlineSlide(
        slide_id="s", layout="three_box", title="t",
        block_refs=[three_box_block.block_id],
    )
    content = _deterministic_slide_content(spec, [three_box_block.model_dump()])
    assert len(content.columns) == 3
    headings = [c.get("heading") for c in content.columns]
    assert headings == ["Market Is Pricing", "We Assume", "What Must Be True"]

    # Verify no raw snake_case identifiers leak through.
    all_bullets = " ".join(
        bullet for col in content.columns for bullet in (col.get("bullets") or [])
    )
    for forbidden in ("revenue_growth_near:", "implied_wacc:", "wacc_plausibility:", "lifecycle_stage:"):
        assert forbidden not in all_bullets, f"Raw key leaked: {forbidden}"


def test_decision_fallback_excludes_framing_context(aapl_payload):
    """Decision fallback must not dump framing_context raw."""
    from graphs.workflows.deck.slides import _deterministic_slide_content
    from graphs.workflows.deck.state import OutlineSlide

    blocks = build_expectations_blocks(aapl_payload, "ref", "AAPL")
    decision_block = next(b for b in blocks if b.kind == "decision")
    spec = OutlineSlide(
        slide_id="s", layout="decision_summary", title="t",
        block_refs=[decision_block.block_id],
    )
    content = _deterministic_slide_content(spec, [decision_block.model_dump()])
    all_bullets = " ".join(content.body_bullets)
    # These appeared in the broken slide 8 — must not surface in fallback now.
    for forbidden in ("key_risks:", "growth_drivers:", "macro_context:", "competitive_position:"):
        assert forbidden not in all_bullets, f"Raw framing_context leaked: {forbidden}"
