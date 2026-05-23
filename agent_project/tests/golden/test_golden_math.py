"""Golden dataset tests — DCF math against hand-curated records.

Each record in `tests/fixtures/golden/*.json` defines:
  - Hand-vetted assumptions from real filings (Tier A factual + Damodaran WACC)
  - Expected implied-price *range* (not a point estimate — DCF inherently imprecise)
  - Analyst consensus target (for context, NOT asserted against directly)

Test contract:
  Given the assumptions, the DCF math pipeline (project_cashflows +
  compute_valuation) must produce an implied share price inside the
  expected range. Outside the range = math bug or stale assumptions.

These tests catch ERRORS (model produces nonsense numbers).
Unit tests catch REGRESSIONS (code changed). Different jobs.
"""

import sys, os, glob, json
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

import pytest

from agent_project.graphs.workflows.dcf.valuation import (
    project_cashflows_node,
    compute_valuation_node,
)
from agent_project.tests.helpers import build_test_state


GOLDEN_DIR = Path(__file__).parent.parent / "fixtures" / "golden"
GOLDEN_FILES = sorted(
    p for p in GOLDEN_DIR.glob("*.json")
    if not p.name.startswith("_")  # skip _template.json
)


def _load_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _record_id(path) -> str:
    return Path(path).stem


def _build_state_from_record(record: dict) -> dict:
    """Project assumption dict from record format → flat float dict."""
    assumptions = {k: float(v["value"]) for k, v in record["assumptions"].items()}
    spot_price = float(record["expected_output"].get("analyst_consensus_target", 100.0))
    return build_test_state(
        ticker=record["ticker"],
        horizon_years=int(record["horizon_years"]),
        assumptions=assumptions,
        market_snapshot={"price": spot_price},
    )


# ---------------------------------------------------------------------------
# Schema validation — fail fast if a record is malformed
# ---------------------------------------------------------------------------

_REQUIRED_ASSUMPTIONS = {
    "base_revenue", "revenue_growth", "fcff_margin", "wacc",
    "terminal_growth", "net_debt", "shares_outstanding", "tax_rate",
}


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=_record_id)
def test_record_has_required_top_level_keys(path):
    record = _load_record(path)
    for key in ("ticker", "horizon_years", "assumptions", "expected_output"):
        assert key in record, f"{path.name}: missing top-level key '{key}'"


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=_record_id)
def test_record_has_required_assumptions(path):
    record = _load_record(path)
    fields = set(record["assumptions"].keys())
    missing = _REQUIRED_ASSUMPTIONS - fields
    assert not missing, f"{path.name}: missing assumption fields {missing}"


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=_record_id)
def test_record_assumptions_have_value_and_source(path):
    record = _load_record(path)
    for field, spec in record["assumptions"].items():
        assert isinstance(spec, dict), f"{path.name}.{field}: expected dict, got {type(spec)}"
        assert "value" in spec, f"{path.name}.{field}: missing 'value'"
        assert "source" in spec, f"{path.name}.{field}: missing 'source'"
        assert "tier" in spec, f"{path.name}.{field}: missing 'tier'"
        assert spec["tier"] in ("factual", "derived", "consensus", "industry"), (
            f"{path.name}.{field}: invalid tier '{spec['tier']}'"
        )


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=_record_id)
def test_record_expected_range_well_formed(path):
    record = _load_record(path)
    rng = record["expected_output"]["implied_share_price_range"]
    assert "low" in rng and "high" in rng
    assert rng["low"] < rng["high"], f"{path.name}: range low >= high"
    assert rng["low"] > 0, f"{path.name}: range low must be positive"


# ---------------------------------------------------------------------------
# Math correctness — the actual ground-truth test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", GOLDEN_FILES, ids=_record_id)
def test_golden_implied_price_in_expected_range(path):
    """Run DCF math with golden assumptions → implied price ∈ expected range."""
    record = _load_record(path)
    state = _build_state_from_record(record)

    # Run the math pipeline
    cf_result = project_cashflows_node(state)
    state["projected_fcff"] = cf_result["projected_fcff"]
    val_result = compute_valuation_node(state)

    implied = val_result["valuation"]["implied_share_price"]
    rng = record["expected_output"]["implied_share_price_range"]

    assert rng["low"] <= implied <= rng["high"], (
        f"{record['ticker']} ({record['fiscal_year']}): implied=${implied:.2f} "
        f"outside expected range [${rng['low']:.2f}, ${rng['high']:.2f}]. "
        f"Analyst consensus: ${record['expected_output'].get('analyst_consensus_target', 'N/A')}. "
        f"Either math bug or stale assumptions — see {path.name}."
    )


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=_record_id)
def test_golden_terminal_growth_below_wacc(path):
    """Gordon model requires TGR < WACC. Catch this before DCF runs."""
    record = _load_record(path)
    tgr = record["assumptions"]["terminal_growth"]["value"]
    wacc = record["assumptions"]["wacc"]["value"]
    assert tgr < wacc, f"{path.name}: TGR={tgr} >= WACC={wacc} — Gordon model breaks"


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=_record_id)
def test_golden_valuation_components_balance(path):
    """EV = PV(cashflows) + PV(terminal). Sanity check on accounting identity."""
    record = _load_record(path)
    state = _build_state_from_record(record)
    cf_result = project_cashflows_node(state)
    state["projected_fcff"] = cf_result["projected_fcff"]
    val = compute_valuation_node(state)["valuation"]

    reconstructed_ev = val["pv_cash_flows"] + val["terminal_pv"]
    assert abs(val["enterprise_value"] - reconstructed_ev) < 1.0, (
        f"{path.name}: EV reconciliation off — "
        f"EV=${val['enterprise_value']:,.0f} vs PV+TV=${reconstructed_ev:,.0f}"
    )


# ---------------------------------------------------------------------------
# Discovery — confirm at least one golden record exists
# ---------------------------------------------------------------------------

def test_golden_dataset_not_empty():
    """At least one golden record must exist; otherwise this test suite is moot."""
    assert len(GOLDEN_FILES) >= 1, (
        f"No golden records found in {GOLDEN_DIR}. "
        "See fixtures/golden/README.md for how to add one."
    )
