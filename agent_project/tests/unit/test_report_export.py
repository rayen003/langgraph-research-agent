"""Tests for DCF report export (markdown + PDF)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.report_export import (
    SENSITIVITY_CHART_MARKER,
    markdown_to_html,
    render_report_pdf,
)


def test_markdown_to_html_renders_table():
    md = "## Sensitivity Matrix\n\n| A | B |\n|---|---|\n| 1 | 2 |"
    html = markdown_to_html(md)
    assert "<table>" in html
    assert "Sensitivity Matrix" in html


def test_markdown_strips_chart_marker():
    md = f"Hello\n{SENSITIVITY_CHART_MARKER}\nWorld"
    html = markdown_to_html(md)
    assert SENSITIVITY_CHART_MARKER not in html
    assert "Hello" in html


def test_render_report_pdf_wide_assumptions_table():
    long_basis = (
        "Based on recent earnings reports, Apple reported a 17% revenue growth in Q2 FY2026 "
        "and management guidance suggests continued growth of approximately 14-17% in the near term."
    )
    md = (
        "# DCF Valuation: AAPL\n\n"
        "## Assumptions\n\n"
        "| Field | Value | Basis | Refs |\n"
        "|-------|-------|-------|------|\n"
        f"| revenue_growth | 17.50% | {long_basis} | [1][2] |\n"
        "| wacc | 10.15% | CAPM-derived WACC from env rates. | — |\n"
    )
    pdf = render_report_pdf(md)
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 2000
