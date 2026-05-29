"""Deck outline — deterministic fallback robustness."""

from __future__ import annotations

from graphs.workflows.deck.outline import _deterministic_fallback_outline
from graphs.workflows.deck.state import SlideLayout


def _block(block_id: str, hints: list[str]) -> dict:
    return {
        "block_id": block_id,
        "kind": "narrative",
        "title": "T",
        "content": {},
        "source_type": "manual_text",
        "source_ref": "x",
        "evidence_refs": [],
        "suggested_slide_layouts": hints,
    }


def test_fallback_ignores_invalid_layout_hint():
    # An adapter hint that is NOT a valid SlideLayout must not crash the
    # no-LLM fallback (whose whole job is to never fail). It should fall back
    # to 'bullets' for that block.
    blocks = [_block("b1", ["not_a_real_layout", "also_bad"])]
    outline = _deterministic_fallback_outline(blocks)
    valid = set(SlideLayout.__args__)  # type: ignore[attr-defined]
    assert all(s.layout in valid for s in outline.slides)
    content_slide = next(s for s in outline.slides if s.layout not in {"title", "references"})
    assert content_slide.layout == "bullets"


def test_fallback_uses_first_valid_hint():
    blocks = [_block("b1", ["garbage", "thesis"])]
    outline = _deterministic_fallback_outline(blocks)
    content_slide = next(s for s in outline.slides if s.layout not in {"title", "references"})
    assert content_slide.layout == "thesis"


def test_fallback_always_has_title_and_references():
    outline = _deterministic_fallback_outline([_block("b1", [])])
    layouts = [s.layout for s in outline.slides]
    assert layouts[0] == "title"
    assert layouts[-1] == "references"
