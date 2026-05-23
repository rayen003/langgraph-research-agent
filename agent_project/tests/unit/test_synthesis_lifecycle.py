"""Tests for CompanyState lifecycle signals + KG company_lifecycle node type.

Verifies:
    1. CompanyState schema has 4 new required lifecycle fields
    2. _fmt_synthesis_line surfaces lifecycle signals
    3. KG TTL dict has company_lifecycle with 30d TTL
    4. State initializer creates kg_lifecycle_hint slot
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

import pytest

from agent_project.graphs.workflows.dcf.synthesis import (
    CompanyState,
    EvidenceRef,
    _fmt_synthesis_line,
)
from agent_project.graphs.workflows.dcf.graph import _build_initial_state


# ---------------------------------------------------------------------------
# 1. CompanyState schema
# ---------------------------------------------------------------------------

def _minimal_state(**overrides):
    base = dict(
        ticker="TEST",
        business_summary="A test company",
        lifecycle_stage="mature",
        margin_trajectory="stable",
        capital_return_policy="Modest buybacks alongside 2% dividend",
        sbc_intensity="low",
        growth_outlook="Steady mid-single-digit growth",
        growth_drivers=["driver A", "driver B", "driver C"],
        margin_trend="stable",
        margin_narrative="Stable margins via pricing power",
        key_risks=["risk A", "risk B", "risk C"],
        competitive_position="Strong moat",
        macro_context="Neutral",
        conflicts=[],
        evidence_refs=[EvidenceRef(evidence_id="ev-1", relevance="test")],
        confidence_self_assessment="medium",
    )
    base.update(overrides)
    return CompanyState(**base)


def test_companystate_has_lifecycle_stage():
    s = _minimal_state()
    assert s.lifecycle_stage == "mature"


def test_companystate_has_margin_trajectory():
    s = _minimal_state(margin_trajectory="expanding")
    assert s.margin_trajectory == "expanding"


def test_companystate_has_capital_return_policy():
    s = _minimal_state(capital_return_policy="Aggressive $50B buyback program")
    assert "buyback" in s.capital_return_policy.lower()


def test_companystate_has_sbc_intensity():
    s = _minimal_state(sbc_intensity="high")
    assert s.sbc_intensity == "high"


def test_companystate_lifecycle_stage_required():
    """Missing lifecycle_stage should fail validation."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CompanyState(
            ticker="TEST",
            business_summary="x",
            # missing lifecycle_stage
            margin_trajectory="stable",
            capital_return_policy="none",
            sbc_intensity="low",
            growth_outlook="x", growth_drivers=[],
            margin_trend="stable", margin_narrative="x",
            key_risks=[], competitive_position="x",
            macro_context="x", evidence_refs=[],
            confidence_self_assessment="low",
        )


# ---------------------------------------------------------------------------
# 2. _fmt_synthesis_line
# ---------------------------------------------------------------------------

def test_fmt_synthesis_line_shows_lifecycle():
    state_dict = {
        "lifecycle_stage": "hypergrowth",
        "margin_trajectory": "compressing",
        "sbc_intensity": "high",
        "key_risks": ["a", "b"],
        "confidence_self_assessment": "high",
    }
    line = _fmt_synthesis_line(state_dict)
    assert "lifecycle: hypergrowth" in line
    assert "margins: compressing" in line
    assert "sbc: high" in line
    assert "2 risks" in line
    assert "self-assessed: high" in line


def test_fmt_synthesis_line_empty_state():
    line = _fmt_synthesis_line({})
    # Just has "0 risks" — no lifecycle data
    assert "0 risks" in line
    assert "lifecycle" not in line


# ---------------------------------------------------------------------------
# 3. KG company_lifecycle TTL
# ---------------------------------------------------------------------------

def test_kg_lifecycle_ttl_is_30_days():
    from agent_project.kg.cache import TTL
    assert "company_lifecycle" in TTL
    assert TTL["company_lifecycle"] == 2592000.0  # 30 days


def test_kg_lifecycle_ttl_longer_than_synthesis():
    """Lifecycle (30d) should outlive synthesis (7d) — lifecycle changes slower."""
    from agent_project.kg.cache import TTL
    assert TTL["company_lifecycle"] > TTL["company_synthesis"]


# ---------------------------------------------------------------------------
# 4. Initial state has kg_lifecycle_hint slot
# ---------------------------------------------------------------------------

def test_initial_state_has_kg_lifecycle_hint():
    s = _build_initial_state(
        ticker="TEST",
        horizon_years=5,
        assumption_review_mode=False,
        allow_external_assumptions=True,
        assumption_overrides={},
        parent_step_id="test",
        session_id="test",
    )
    assert "kg_lifecycle_hint" in s
    assert s["kg_lifecycle_hint"] == {}


# ---------------------------------------------------------------------------
# 5. Lifecycle stage enum-like values are documented
# ---------------------------------------------------------------------------

def test_documented_lifecycle_stages_all_accepted():
    """All 5 documented stages should pass Pydantic validation."""
    for stage in ("hypergrowth", "scaling", "mature", "declining", "cyclical"):
        s = _minimal_state(lifecycle_stage=stage)
        assert s.lifecycle_stage == stage


def test_documented_margin_trajectories_all_accepted():
    for traj in ("expanding", "stable", "compressing"):
        s = _minimal_state(margin_trajectory=traj)
        assert s.margin_trajectory == traj


def test_documented_sbc_intensities_all_accepted():
    for intensity in ("high", "moderate", "low"):
        s = _minimal_state(sbc_intensity=intensity)
        assert s.sbc_intensity == intensity
