"""Readable citations + title subtitle — references slide must not leak raw KG IDs."""

from __future__ import annotations

from graphs.workflows.deck.adapters.dcf_output import DcfOutputAdapter
from graphs.workflows.deck.slides import (
    _build_references_slide,
    _build_title_slide,
    _format_citation,
)
from graphs.workflows.deck.state import DcfOutputSource, OutlineSlide


# ── _format_citation ─────────────────────────────────────────────────────────


def test_format_structured_fundamental():
    item = {"kind": "structured_fundamental", "source": "fmp",
            "field": "fcff_margin", "as_of": "2025-09-27"}
    assert _format_citation("ev_fmp_fcff_margin", item) == "FMP · FCFF Margin · 2025-09-27"


def test_format_filing_excerpt():
    item = {"kind": "filing_excerpt", "source": "sec_edgar", "filing_type": "10-Q",
            "section": "Risk Factors (Item 1A)", "as_of": "2026-01-30"}
    out = _format_citation("ev_sec_x", item)
    assert out == "SEC EDGAR 10-Q · Risk Factors (Item 1A) · 2026-01-30"


def test_format_web_trims_iso_timestamp():
    item = {"kind": "web", "source": "exa", "title": "Apple outlook",
            "as_of": "2026-05-01T14:53:28.000Z"}
    out = _format_citation("ev_web_1", item)
    assert out == "Exa · Apple outlook · 2026-05-01"
    assert "T14" not in out  # no raw timestamp leak


def test_format_unknown_id_degrades_gracefully():
    # No item → never leak the bare ID; humanize it instead.
    out = _format_citation("ev_fmp_buyback_yield", None)
    assert out == "Buyback Yield"


# ── references slide ─────────────────────────────────────────────────────────


def _ref_slide() -> OutlineSlide:
    return OutlineSlide(slide_id="s8", layout="references", title="Sources", block_refs=[])


def test_references_resolve_against_index():
    blocks = [{"block_id": "b1", "evidence_refs": ["ev_fmp_tax_rate"]}]
    idx = {"ev_fmp_tax_rate": {"kind": "structured_fundamental", "source": "fmp",
                               "field": "tax_rate", "as_of": "2025-09-27"}}
    sc = _build_references_slide(_ref_slide(), blocks, idx)
    assert sc.body_bullets == ["[1] FMP · Tax Rate · 2025-09-27"]
    # No raw evidence ID leaks into the rendered bullet.
    assert "ev_fmp" not in sc.body_bullets[0]


def test_references_unknown_ref_still_readable():
    blocks = [{"block_id": "b1", "evidence_refs": ["ev_fmp_sbc_pct_revenue"]}]
    sc = _build_references_slide(_ref_slide(), blocks, {})  # empty index
    assert sc.body_bullets[0] == "[1] SBC Pct Revenue"


def test_references_empty_blocks():
    sc = _build_references_slide(_ref_slide(), [], {})
    assert sc.body_bullets == ["Sources compiled from inputs to this deck."]


# ── title slide subtitle ─────────────────────────────────────────────────────


def test_title_slide_builds_subtitle():
    sc = _build_title_slide(
        OutlineSlide(slide_id="s1", layout="title", title="AAPL Case", block_refs=[]),
        {"audience": "ic", "title": "AAPL Case"},
    )
    assert sc.body_paragraphs
    assert sc.body_paragraphs[0].startswith("Investment Committee · ")


def test_title_slide_unknown_audience():
    sc = _build_title_slide(
        OutlineSlide(slide_id="s1", layout="title", title="X", block_refs=[]),
        {"audience": "analysts"},
    )
    # Unknown audience still produces a subtitle (title-cased + date).
    assert sc.body_paragraphs[0].startswith("Analysts · ")


# ── adapter collect_evidence hook ────────────────────────────────────────────


def test_collect_evidence_indexes_by_id():
    payload = {
        "ticker": "AAPL",
        "_evidence_items": [
            {"evidence_id": "ev_a", "kind": "structured_fundamental", "field": "x"},
            {"evidence_id": "ev_b", "kind": "web", "title": "t"},
            {"kind": "web"},  # no id → skipped
        ],
    }
    idx = DcfOutputAdapter().collect_evidence(DcfOutputSource(payload_inline=payload))
    assert set(idx) == {"ev_a", "ev_b"}


def test_collect_evidence_empty_when_no_items():
    idx = DcfOutputAdapter().collect_evidence(DcfOutputSource(payload_inline={"ticker": "X"}))
    assert idx == {}
