"""Tests for deck artifact path resolution and _default adoption."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import utils as utils_mod
from utils import resolve_deck_output_path, resolve_deck_pptx_path


def test_resolve_deck_pptx_adopts_from_default(tmp_path, monkeypatch):
    thread_id = "chat_adopt_test"
    run_dir = tmp_path / thread_id
    run_dir.mkdir()

    fallback_dir = tmp_path / "_default" / "decks"
    fallback_dir.mkdir(parents=True)
    pptx = fallback_dir / "AAPL_DCF_Investment_Case.pptx"
    pptx.write_bytes(b"fake-pptx")
    (fallback_dir / "deck_output.json").write_text(
        json.dumps({"pptx_path": str(pptx)}),
        encoding="utf-8",
    )

    monkeypatch.setattr(utils_mod, "RUNS_DIR", tmp_path)

    resolved = resolve_deck_pptx_path(thread_id, "AAPL_DCF_Investment_Case.pptx")
    assert resolved is not None
    assert resolved.exists()
    assert resolved.parent == run_dir / "decks"
    assert (run_dir / "decks" / "AAPL_DCF_Investment_Case.pptx").exists()


def test_resolve_deck_output_adopts_from_default(tmp_path, monkeypatch):
    thread_id = "chat_preview_test"
    run_dir = tmp_path / thread_id
    run_dir.mkdir()

    fallback_dir = tmp_path / "_default" / "decks"
    fallback_dir.mkdir(parents=True)
    (fallback_dir / "deck_output.json").write_text(
        json.dumps({"brief": {"title": "Test Deck"}, "slides": []}),
        encoding="utf-8",
    )

    monkeypatch.setattr(utils_mod, "RUNS_DIR", tmp_path)

    resolved = resolve_deck_output_path(thread_id)
    assert resolved is not None
    assert resolved.exists()
    assert resolved.parent == run_dir / "decks"
