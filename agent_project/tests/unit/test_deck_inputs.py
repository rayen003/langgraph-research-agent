"""Tests for deck workflow input normalization."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from agent_project.graphs.workflows.deck.inputs import (
    is_full_dcf_payload,
    resolve_deck_workflow_inputs,
    sanitize_sources,
)
def test_sanitize_sources_drops_placeholder_strings():
    raw = [
        {"type": "dcf_output", "payload_inline": {"summary": {"model_validity": "VALID"}}},
        "briefing_info_summary_or_body_text",
        {"type": "manual_text", "title": "Notes", "body": "hello"},
    ]
    valid = sanitize_sources(raw)
    assert len(valid) == 2
    assert all(isinstance(s, dict) for s in valid)
    assert valid[0].get("payload_inline") is None
    assert valid[1]["type"] == "manual_text"


def test_resolve_injects_dcf_from_disk(tmp_path, monkeypatch):
    import utils as utils_mod

    run_id = "chat_test"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    payload = {
        "ticker": "AAPL",
        "valuation": {"implied_share_price": 200},
        "assumptions": {"wacc": 0.09},
    }
    (run_dir / "dcf_output.json").write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(utils_mod, "RUNS_DIR", tmp_path)
    utils_mod.set_thread_id(run_id)

    sources, brief = resolve_deck_workflow_inputs(
        ["briefing_info_summary_or_body_text"],
        {"title": "AAPL — DCF Investment Case", "audience": "ic"},
        dcf_payload=payload,
    )

    assert brief["title"].startswith("AAPL")
    assert any(s["type"] == "dcf_output" for s in sources)
    assert is_full_dcf_payload(payload)


def test_resolve_recovers_from_llm_placeholder_sources(tmp_path, monkeypatch):
    """Regression: model passes partial DCF inline + placeholder strings."""
    import utils as utils_mod

    run_id = "chat_bad_args"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    payload = {
        "ticker": "AAPL",
        "valuation": {"implied_share_price": 200},
        "assumptions": {"wacc": 0.09},
    }
    (run_dir / "dcf_output.json").write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(utils_mod, "RUNS_DIR", tmp_path)
    utils_mod.set_thread_id(run_id)

    raw_sources = [
        {
            "type": "dcf_output",
            "payload_inline": {"summary": {"model_validity": "VALID", "market_reconciliation": ""}},
        },
        "briefing_info_summary_or_body_text",
    ]
    sources, brief = resolve_deck_workflow_inputs(
        raw_sources,
        {"title": "AAPL — DCF Investment Case", "audience": "ic"},
    )

    assert brief["title"].startswith("AAPL")
    dcf_sources = [s for s in sources if s["type"] == "dcf_output"]
    assert len(dcf_sources) == 1
    assert dcf_sources[0].get("payload_path")
    assert not any(isinstance(s, str) for s in sources)


def test_is_full_dcf_payload_rejects_summary_only():
    assert not is_full_dcf_payload({"summary": {"model_validity": "VALID"}})
    assert is_full_dcf_payload({"ticker": "AAPL", "valuation": {}, "assumptions": {}})
