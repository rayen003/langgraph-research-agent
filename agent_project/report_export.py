"""Report download helpers — markdown + PDF export for DCF runs."""

from __future__ import annotations

import json
import re
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

SENSITIVITY_CHART_MARKER = "[SENSITIVITY_CHART]"

colors_hex = {
    "title": "#2f2419",
    "heading": "#3d3428",
    "body": "#1f1a14",
    "muted": "#5c5040",
    "border": "#d4c9b8",
    "table_head": "#f0e6cc",
    "table_alt": "#faf8f4",
}


def resolve_sensitivity_png(run_dir: Path, payload: dict | None = None) -> Path | None:
    """Locate the sensitivity heatmap PNG for a run."""
    if payload:
        chart = payload.get("sensitivity_chart")
        if isinstance(chart, str) and chart.strip():
            candidate = Path(chart)
            if not candidate.is_absolute():
                candidate = run_dir / chart
            if candidate.exists():
                return candidate

    artifacts = run_dir / "artifacts"
    if not artifacts.is_dir():
        return None
    globs = sorted(
        artifacts.glob("*sensitivity*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return globs[0] if globs else None


def load_dcf_report_markdown(run_dir: Path) -> tuple[str, str, Path | None]:
    """Return (markdown, download_basename, sensitivity_png) from dcf_output.json."""
    from graphs.workflows.dcf.payload import summarize_dcf_payload  # noqa: PLC0415

    dcf_path = run_dir / "dcf_output.json"
    if not dcf_path.exists():
        raise FileNotFoundError(f"No DCF output in {run_dir}")
    payload = json.loads(dcf_path.read_text(encoding="utf-8"))
    markdown = summarize_dcf_payload(payload)
    ticker = str(payload.get("ticker") or "report").lower()
    png_path = resolve_sensitivity_png(run_dir, payload)
    return markdown, f"dcf_{ticker}", png_path


def markdown_to_html(markdown_text: str, *, sensitivity_png: Path | None = None) -> str:
    """Convert report markdown to styled HTML (previews/tests)."""
    import markdown as md_lib

    body_md = markdown_text.replace(SENSITIVITY_CHART_MARKER, "").strip()
    return md_lib.markdown(
        body_md,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
    )


def _pdf_safe(text: str) -> str:
    text = text.replace("✓", "[ok]").replace("⚠", "[!]")
    return text.replace("–", "-").replace("—", "-").replace("◆", "-")


def _md_inline_to_reportlab(text: str) -> str:
    text = escape(_pdf_safe(text))
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" color="#1a56db">\1</a>',
        text,
    )
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"_(.+?)_", r"<i>\1</i>", text)
    return text


def _is_table_separator(line: str) -> bool:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells)


def _parse_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _build_styles():
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            textColor=colors_hex["title"],
            spaceAfter=10,
            alignment=TA_LEFT,
        ),
        "h2": ParagraphStyle(
            "ReportH2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            textColor=colors_hex["heading"],
            spaceBefore=14,
            spaceAfter=6,
            alignment=TA_LEFT,
        ),
        "body": ParagraphStyle(
            "ReportBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            textColor=colors_hex["body"],
            spaceAfter=4,
            alignment=TA_LEFT,
        ),
        "bullet": ParagraphStyle(
            "ReportBullet",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            leftIndent=14,
            bulletIndent=4,
            textColor=colors_hex["body"],
            spaceAfter=3,
            alignment=TA_LEFT,
        ),
        "quote": ParagraphStyle(
            "ReportQuote",
            parent=base["BodyText"],
            fontName="Helvetica-Oblique",
            fontSize=9.5,
            leading=13,
            textColor=colors_hex["muted"],
            leftIndent=12,
            spaceAfter=6,
            alignment=TA_LEFT,
        ),
    }


def _table_col_widths(col_count: int, headers: list[str], avail_width: float) -> list[float]:
    """Allocate column widths that fit within the printable area."""
    if col_count <= 0:
        return []
    headers_lower = [h.lower().strip() for h in headers]

    if col_count == 4 and any("basis" in h for h in headers_lower):
        refs_w, field_w, value_w = 34, 60, 46
        basis_w = avail_width - refs_w - field_w - value_w
        return [field_w, value_w, max(basis_w, 100), refs_w]

    if col_count == 4 and any("wacc" in h for h in headers_lower):
        first = 52
        rest = (avail_width - first) / max(col_count - 1, 1)
        return [first] + [rest] * (col_count - 1)

    if col_count == 5 and any("signal" in h for h in headers_lower):
        raw = [50, 46, 54, 54, 34]
        scale = avail_width / sum(raw)
        return [w * scale for w in raw]

    if col_count == 3 and any("scenario" in h for h in headers_lower):
        return [avail_width * 0.34, avail_width * 0.22, avail_width * 0.44]

    return [avail_width / col_count] * col_count


def _table_cell_styles():
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle

    body = ParagraphStyle(
        "TableCell",
        fontName="Helvetica",
        fontSize=7.5,
        leading=9.5,
        textColor=colors_hex["body"],
        alignment=TA_LEFT,
        wordWrap="LTR",
    )
    header = ParagraphStyle(
        "TableHeaderCell",
        parent=body,
        fontName="Helvetica-Bold",
        fontSize=7.5,
        textColor=colors_hex["title"],
    )
    return header, body


def _table_flowable(rows: list[list[str]], *, avail_width: float = 495):
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    if not rows:
        return None

    col_count = max(len(r) for r in rows)
    headers = rows[0]
    col_widths = _table_col_widths(col_count, headers, avail_width)
    header_style, cell_style = _table_cell_styles()

    data: list[list] = []
    for row_idx, row in enumerate(rows):
        style = header_style if row_idx == 0 else cell_style
        normalized = row + [""] * (col_count - len(row))
        data.append([
            Paragraph(_md_inline_to_reportlab(c) if c else " ", style)
            for c in normalized
        ])

    table = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(colors_hex["table_head"])),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor(colors_hex["title"])),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor(colors_hex["border"])),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor(colors_hex["table_alt"])],
                ),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _markdown_to_flowables(
    markdown_text: str,
    *,
    sensitivity_png: Path | None = None,
    avail_width: float = 495,
) -> list:
    from reportlab.platypus import Image, Paragraph, Spacer

    styles = _build_styles()
    flowables: list = []
    lines = markdown_text.replace(SENSITIVITY_CHART_MARKER, "").splitlines()
    i = 0
    pending_chart = False

    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if not stripped:
            flowables.append(Spacer(1, 6))
            i += 1
            continue

        if stripped.startswith("# "):
            flowables.append(Paragraph(_md_inline_to_reportlab(stripped[2:]), styles["title"]))
            i += 1
            continue

        if stripped.startswith("## "):
            heading = stripped[3:]
            flowables.append(Paragraph(_md_inline_to_reportlab(heading), styles["h2"]))
            pending_chart = heading.lower() == "sensitivity matrix"
            i += 1
            continue

        if stripped.startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not _is_table_separator(lines[i]):
                    table_lines.append(_parse_table_row(lines[i]))
                i += 1
            table = _table_flowable(table_lines, avail_width=avail_width)
            if table:
                flowables.append(Spacer(1, 4))
                flowables.append(table)
                flowables.append(Spacer(1, 8))
            if pending_chart and sensitivity_png and sensitivity_png.exists():
                img_w = min(avail_width, 460)
                img_h = img_w * (295 / 460)
                img = Image(str(sensitivity_png), width=img_w, height=img_h)
                img.hAlign = "CENTER"
                flowables.append(Spacer(1, 6))
                flowables.append(img)
                flowables.append(Spacer(1, 10))
                pending_chart = False
            continue

        if stripped.startswith("> "):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                quote_lines.append(lines[i].strip()[2:])
                i += 1
            flowables.append(Paragraph(_md_inline_to_reportlab(" ".join(quote_lines)), styles["quote"]))
            continue

        if stripped.startswith("- "):
            flowables.append(
                Paragraph(f"• {_md_inline_to_reportlab(stripped[2:])}", styles["bullet"])
            )
            i += 1
            continue

        para_lines = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "-", ">")):
            para_lines.append(lines[i].strip())
            i += 1
        flowables.append(Paragraph(_md_inline_to_reportlab(" ".join(para_lines)), styles["body"]))

    return flowables


def render_report_pdf(markdown_text: str, *, sensitivity_png: Path | None = None) -> bytes:
    """Render markdown report bytes as a formatted PDF (ReportLab)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=50,
        rightMargin=50,
        topMargin=48,
        bottomMargin=48,
        title="DCF Report",
    )
    avail_width = A4[0] - doc.leftMargin - doc.rightMargin
    story = _markdown_to_flowables(
        markdown_text,
        sensitivity_png=sensitivity_png,
        avail_width=avail_width,
    )
    doc.build(story)
    pdf = buf.getvalue()
    if not pdf.startswith(b"%PDF"):
        raise RuntimeError("PDF generation produced invalid output")
    return pdf
