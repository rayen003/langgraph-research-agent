"""Opt-in DCF end-to-end trace capture.

Run manually when you want a full workflow artifact bundle:

    DCF_E2E_TICKER=AAPL uv run pytest agent_project/tests/e2e/test_dcf_app_trace.py --run-dcf-e2e -q

This test intentionally does not run in the normal unit suite because it may
call LLMs and external market-data providers. It is a regression harness and
artifact generator, not a deterministic unit test.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Override the test-suite placeholder OPENAI_API_KEY with the real local .env
# when present. This file is opt-in, so loading live credentials here is
# intentional and keeps normal unit tests deterministic.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)

from graphs.workflows.dcf.graph import run_dcf_workflow_sync, summarize_dcf_payload
from utils import get_run_dir, set_thread_id, set_ui_event_handler


ARTIFACT_ROOT = Path(__file__).parent / "artifacts" / "dcf_runs"


def _json_safe(obj):
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return str(obj)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_safe),
        encoding="utf-8",
    )


@pytest.mark.e2e
def test_dcf_workflow_e2e_trace_capture(pytestconfig: pytest.Config):
    """Run the DCF app end-to-end and persist activity + final report artifacts."""
    if not pytestconfig.getoption("--run-dcf-e2e"):
        pytest.skip("Pass --run-dcf-e2e to run live DCF workflow artifact capture.")

    ticker = os.getenv("DCF_E2E_TICKER", "AAPL").upper()
    horizon = int(os.getenv("DCF_E2E_HORIZON", "5"))
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"dcf-e2e-{run_stamp}-{ticker.lower()}-{uuid4().hex[:8]}"
    output_dir = ARTIFACT_ROOT / run_id
    output_dir.mkdir(parents=True, exist_ok=False)

    events: list[dict] = []

    def capture_event(event: dict) -> None:
        events.append(dict(event))

    set_thread_id(run_id)
    set_ui_event_handler(capture_event)

    payload = run_dcf_workflow_sync(
        ticker=ticker,
        horizon_years=horizon,
        assumption_review_mode=False,
        allow_external_assumptions=True,
        assumption_overrides={},
        parent_step_id=f"workflow_dcf_e2e_{ticker.lower()}",
        session_id=run_id,
    )
    report_md = summarize_dcf_payload(payload)

    run_dir = get_run_dir()
    result_path = payload.get("result_path")
    source_output = Path(result_path) if result_path else None

    _write_json(output_dir / "activity_events.json", events)
    _write_json(output_dir / "dcf_output.json", payload)
    (output_dir / "report.md").write_text(report_md, encoding="utf-8")

    if source_output and source_output.exists():
        shutil.copy2(source_output, output_dir / "dcf_output.raw.json")

    run_artifacts = run_dir / "artifacts"
    if run_artifacts.exists():
        artifact_copy_dir = output_dir / "run_artifacts"
        shutil.copytree(run_artifacts, artifact_copy_dir, dirs_exist_ok=True)

    summary = {
        "run_id": run_id,
        "ticker": ticker,
        "horizon_years": horizon,
        "output_dir": str(output_dir),
        "run_dir": str(run_dir),
        "activity_event_count": len(events),
        "activity_names": [
            e.get("name")
            for e in events
            if e.get("type") == "activity" and e.get("status") == "completed"
        ],
        "model_validity": payload.get("model_validity"),
        "reconciliation_status": payload.get("reconciliation_status"),
        "confidence_label": payload.get("confidence_label"),
        "confidence_assessment": payload.get("confidence_assessment"),
        "implied_share_price": (payload.get("valuation") or {}).get("implied_share_price"),
        "current_price": (payload.get("valuation") or {}).get("current_price"),
        "assumptions": payload.get("assumptions"),
        "report_path": str(output_dir / "report.md"),
    }
    _write_json(output_dir / "summary.json", summary)
    (ARTIFACT_ROOT / "LATEST").write_text(str(output_dir), encoding="utf-8")

    # Stable smoke assertions only. Exact values are intentionally not asserted
    # because this is a live LLM/API integration harness.
    assert payload.get("ticker", ticker).upper() == ticker
    assert payload.get("model_validity") in {"valid", "adjusting", "invalid"}
    assert isinstance((payload.get("valuation") or {}).get("implied_share_price"), (int, float))
    assert "## Executive Summary" in report_md
    assert len(events) > 0
    assert (output_dir / "summary.json").exists()
