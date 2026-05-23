"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Ensure agent_project is importable (tests run from project root).
_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "agent_project"))

# Stub OPENAI_API_KEY so ChatOpenAI doesn't raise at import time.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "payloads"


def _load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / name
    if not path.exists():
        pytest.skip(f"Fixture not found: {path}. Run `make capture-fixtures` first.")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def aapl_payload() -> dict:
    """Full DCF output payload for AAPL (captured from live run)."""
    return _load_fixture("valid_aapl.json")


@pytest.fixture(scope="session")
def invalid_solver_payload() -> dict:
    """Payload with model_validity=invalid (hand-crafted)."""
    return _load_fixture("invalid_solver.json")
