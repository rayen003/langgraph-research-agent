"""Tests for _run_consistency_checks in payload.py.

Pure Python — verifies each consistency check independently.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.payload import _run_consistency_checks
from agent_project.tests.helpers import build_test_payload


# ---------------------------------------------------------------------------
# EV reconciliation
# ---------------------------------------------------------------------------

def test_ev_reconciliation_passes_when_balanced():
    payload = build_test_payload(
        valuation={
            "pv_cash_flows": 900_000.0,
            "terminal_pv": 2_100_000.0,
            "enterprise_value": 3_000_000.0,
            "equity_value": 2_950_000.0,
            "implied_share_price": 190.0,
            "current_price": 180.0,
        }
    )
    checks = {c["label"]: c for c in _run_consistency_checks(payload)}
    assert checks["EV reconciliation"]["ok"] is True


def test_ev_reconciliation_fails_when_unbalanced():
    """PV + TV_pv ≠ EV → flag."""
    payload = build_test_payload(
        valuation={
            "pv_cash_flows": 500_000.0,   # too small
            "terminal_pv": 2_100_000.0,
            "enterprise_value": 3_000_000.0,  # 2.6M ≠ 3.0M
            "equity_value": 2_950_000.0,
            "implied_share_price": 190.0,
            "current_price": 180.0,
        }
    )
    checks = {c["label"]: c for c in _run_consistency_checks(payload)}
    assert checks["EV reconciliation"]["ok"] is False


# ---------------------------------------------------------------------------
# Terminal growth vs Rf
# ---------------------------------------------------------------------------

def test_tgr_below_rf_passes():
    payload = build_test_payload()
    payload["assumptions"] = {**payload["assumptions"], "terminal_growth": 0.03}
    payload["wacc_components"] = {"risk_free_rate": 0.045}
    checks = {c["label"]: c for c in _run_consistency_checks(payload)}
    assert checks["Terminal growth vs Rf"]["ok"] is True


def test_tgr_above_rf_plus_buffer_fails():
    """TGR > Rf + 50bps → flag."""
    payload = build_test_payload()
    payload["assumptions"] = {**payload["assumptions"], "terminal_growth": 0.06}
    payload["wacc_components"] = {"risk_free_rate": 0.045}
    checks = {c["label"]: c for c in _run_consistency_checks(payload)}
    assert checks["Terminal growth vs Rf"]["ok"] is False


def test_tgr_just_below_boundary_passes():
    """TGR clearly below Rf + 50bps → ok."""
    payload = build_test_payload()
    payload["assumptions"] = {**payload["assumptions"], "terminal_growth": 0.049}
    payload["wacc_components"] = {"risk_free_rate": 0.045}
    checks = {c["label"]: c for c in _run_consistency_checks(payload)}
    assert checks["Terminal growth vs Rf"]["ok"] is True


# ---------------------------------------------------------------------------
# Evidence coverage
# ---------------------------------------------------------------------------

def test_evidence_check_skipped_when_no_memo_proposals():
    """No proposals in memo → evidence check not included."""
    payload = build_test_payload(assumption_memo={})
    checks_labels = [c["label"] for c in _run_consistency_checks(payload)]
    assert "Evidence coverage" not in checks_labels


def test_evidence_check_fails_with_zero_refs():
    payload = build_test_payload(
        assumption_memo={"proposals": [{"field": "wacc", "evidence_refs": []}]},
        _evidence_items=[],
    )
    checks = {c["label"]: c for c in _run_consistency_checks(payload)}
    assert checks["Evidence coverage"]["ok"] is False


def test_evidence_check_fails_with_refs_but_no_filings():
    payload = build_test_payload(
        assumption_memo={"proposals": [{"field": "wacc", "evidence_refs": ["ev-001"]}]},
        _evidence_items=[{"evidence_id": "ev-001", "source_tier": "api"}],
    )
    checks = {c["label"]: c for c in _run_consistency_checks(payload)}
    assert checks["Evidence coverage"]["ok"] is False


def test_evidence_check_passes_with_filing_ref():
    payload = build_test_payload(
        assumption_memo={"proposals": [{"field": "wacc", "evidence_refs": ["ev-001"]}]},
        _evidence_items=[{"evidence_id": "ev-001", "source_tier": "filing"}],
    )
    checks = {c["label"]: c for c in _run_consistency_checks(payload)}
    assert checks["Evidence coverage"]["ok"] is True
