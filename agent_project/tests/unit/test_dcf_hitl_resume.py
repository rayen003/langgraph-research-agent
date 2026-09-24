from __future__ import annotations

import json


def test_native_hitl_resume_restores_result_path_in_returned_payload(monkeypatch, tmp_path):
    import tools

    result_path = tmp_path / "dcf_output.json"
    result_path.write_text(json.dumps({
        "ticker": "AAPL",
        "assumptions": {"wacc": 0.09},
        "valuation": {"implied_share_price": 200.0},
    }), encoding="utf-8")
    monkeypatch.setattr(
        tools.dcf_workflow_app,
        "invoke",
        lambda _command, config: {"result_path": str(result_path)},
    )
    persisted: list[dict] = []
    monkeypatch.setattr(
        tools,
        "_persist_dcf_payload",
        lambda payload, _args: persisted.append(dict(payload)) or "{}",
    )

    payload, _pointer, _report = tools.resume_dcf_workflow_after_hitl(
        resume_payload={"action": "approve"},
        thread_id="thread-test",
    )

    assert payload["result_path"] == str(result_path)
    assert persisted[0]["result_path"] == str(result_path)
