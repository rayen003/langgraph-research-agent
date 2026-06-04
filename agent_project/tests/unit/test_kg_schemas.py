"""Tests for KG node-value schema validation (kg/schemas.py)."""

import pytest

from kg.schemas import (
    validate_kg_value,
    is_scalar_node,
    SCALAR_NODE_TYPES,
    RUN_SCOPED_NODE_TYPES,
    DocumentFactValue,
    FilingValue,
)


@pytest.mark.unit
def test_scalar_node_classification():
    assert is_scalar_node("market_metric_fund")
    assert is_scalar_node("run_assumption")
    assert is_scalar_node("run_output")
    assert not is_scalar_node("document_fact")
    assert not is_scalar_node("filing")


@pytest.mark.unit
def test_scalar_value_accepts_float():
    # The exact case that used to crash ingest_fact (float, not dict).
    value, warnings = validate_kg_value("market_metric_fund", 8717.1)
    assert value == 8717.1
    assert warnings == []


@pytest.mark.unit
def test_scalar_value_tolerates_wrapped_dict():
    value, warnings = validate_kg_value("run_assumption", {"value": 0.087, "source": "capm"})
    assert warnings == []


@pytest.mark.unit
def test_scalar_value_warns_on_wrong_shape():
    value, warnings = validate_kg_value("market_metric_fund", {"text": "no number"})
    assert warnings  # flagged, but...
    assert value == {"text": "no number"}  # never dropped/mutated


@pytest.mark.unit
def test_document_fact_valid_dict_passes():
    payload = {
        "value": 29578.0, "text": "Net income 29,578", "as_of": "Q2 2026",
        "period": "Q2 2026", "fact_type": "net_income",
        "source_doc_id": "doc_1", "confidence": 0.95,
    }
    value, warnings = validate_kg_value("document_fact", payload)
    assert warnings == []
    assert value is payload  # returned unchanged (no mutation)


@pytest.mark.unit
def test_document_fact_extra_keys_allowed():
    # Additive KG — unknown keys must not be rejected.
    value, warnings = validate_kg_value(
        "document_fact", {"value": 1.0, "custom_key": "keep me"}
    )
    assert warnings == []


@pytest.mark.unit
def test_dict_node_warns_on_scalar_value():
    value, warnings = validate_kg_value("filing", 123.0)
    assert warnings
    assert value == 123.0


@pytest.mark.unit
def test_unmodeled_node_type_passes_through():
    value, warnings = validate_kg_value("some_future_type", {"anything": 1})
    assert warnings == []
    assert value == {"anything": 1}


@pytest.mark.unit
def test_validate_never_raises():
    # Adversarial inputs must not raise — KG writes stay resilient.
    for bad in (None, [], "", object(), {"value": object()}):
        validate_kg_value("document_fact", bad)
        validate_kg_value("market_metric_fund", bad)


@pytest.mark.unit
def test_run_scoped_set_covers_run_nodes():
    for nt in ("dcf_run", "run_assumption", "run_output"):
        assert nt in RUN_SCOPED_NODE_TYPES


@pytest.mark.unit
def test_doc_fact_subtypes_share_schema():
    # guidance / risk_factor / etc. validate with the document-fact shape.
    for nt in ("guidance", "risk_factor", "competitive_moat", "capital_allocation"):
        _, warnings = validate_kg_value(nt, {"value": None, "text": "x", "fact_type": nt})
        assert warnings == []


@pytest.mark.unit
def test_models_default_construct():
    assert DocumentFactValue().fact_type == "other"
    assert FilingValue().filing_type == "filing"
