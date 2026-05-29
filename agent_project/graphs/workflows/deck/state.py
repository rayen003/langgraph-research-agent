"""Deck workflow state, source schemas, and shared Pydantic contracts.

The deck workflow is standalone — it accepts a polymorphic list of typed
``DeckSource`` inputs and produces a PPTX (and later PDF/HTML).  Each input
type is normalized into ``NormalizedBlock`` items by a dedicated adapter
(see ``adapters/``).  Downstream nodes (outline → per-slide LLM → assemble)
are source-agnostic.

DCF integration: ``DcfOutputSource`` is one input type among many; the deck
workflow has no DCF dependency outside ``adapters/dcf_output.py``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict, Union

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Input contract — discriminated union of supported source types
# ---------------------------------------------------------------------------


class _SourceBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DcfOutputSource(_SourceBase):
    """Reference to a completed DCF run as a deck source."""
    type: Literal["dcf_output"] = "dcf_output"
    run_id: str | None = Field(default=None, description="KG dcf_run node ID, e.g. META::dcf_run::workflow_dcf::meta")
    payload_path: str | None = Field(default=None, description="Disk path to dcf_output.json")
    payload_inline: dict | None = Field(default=None, description="Full dcf_output.json dict passed directly")


class DocumentSource(_SourceBase):
    """One or more uploaded documents indexed in ChromaDB (documents.py)."""
    type: Literal["document"] = "document"
    doc_ids: list[str] = Field(default_factory=list)
    query_hints: list[str] = Field(default_factory=list, description="Optional retrieval bias terms for RAG")


class WebSource(_SourceBase):
    """Explicit URLs to fetch + summarize."""
    type: Literal["web"] = "web"
    urls: list[str] = Field(default_factory=list)


class ManualTextSource(_SourceBase):
    """Raw analyst-provided text (paste / notes)."""
    type: Literal["manual_text"] = "manual_text"
    title: str
    body: str


class KgSubgraphSource(_SourceBase):
    """Pull a KG subgraph rooted at an anchor (company, theme, run, etc.)."""
    type: Literal["kg_subgraph"] = "kg_subgraph"
    anchor_id: str
    depth: int = Field(default=2, ge=1, le=4)


class ChartArtifactSource(_SourceBase):
    """Existing image artifact (PNG path) to embed directly."""
    type: Literal["chart_artifact"] = "chart_artifact"
    path: str
    caption: str | None = None


DeckSource = Annotated[
    Union[
        DcfOutputSource,
        DocumentSource,
        WebSource,
        ManualTextSource,
        KgSubgraphSource,
        ChartArtifactSource,
    ],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Deck brief — user-level intent
# ---------------------------------------------------------------------------


HitlMode = Literal["disabled", "partial", "full"]
# - "disabled": no HITL, auto-approve outline and slides
# - "partial":  HITL on outline only (default — matches DCF approval pattern)
# - "full":     HITL on outline AND per-slide review before assembly


class DeckBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="Deck title, also used for output filename")
    audience: Literal["board", "ic", "internal", "client", "generic"] = "generic"
    slide_count_target: int | None = Field(default=None, ge=1, le=40)
    tone: str | None = Field(default=None, description="e.g. 'concise', 'narrative', 'data-dense'")
    must_cover: list[str] = Field(default_factory=list, description="Topics that MUST appear as slides")
    hitl_mode: HitlMode = "partial"
    # ── Phase B theme tokens (optional; audience supplies defaults) ──────────
    # When set, these override the audience-derived theme in assemble.py.
    density: Literal["compact", "standard", "spacious"] | None = Field(
        default=None,
        description="Vertical spacing / font baseline. Overrides audience default.",
    )
    accent: str | None = Field(
        default=None,
        description="Accent color as hex (e.g. '#2dd4bf' or '2dd4bf'). Invalid values fall back to the audience default.",
    )
    font_scale: float | None = Field(
        default=None,
        ge=0.8,
        le=1.4,
        description="Multiplier applied to all font sizes. Clamped to [0.8, 1.4].",
    )


# ---------------------------------------------------------------------------
# Normalized block — atomic unit any adapter produces
# ---------------------------------------------------------------------------


# Block kinds.
#
# Legacy descriptive kinds (`metric`, `table`, `chart`, `list`, `narrative`,
# `quote`) are kept for non-DCF source adapters (manual_text, document, web,
# kg_subgraph, chart_artifact) and the legacy DCF slides.
#
# Expectations-first kinds drive the new institutional deck shape — each one
# emits structured raw data, and the per-slide LLM writes the framing prose
# on top so the deck can adapt to the company archetype:
#   - expectations_table : model vs market-implied reconciliation table
#   - three_box          : "priced / assumed / required" exec summary
#   - debate             : bull vs bear two-column structured narrative
#   - capital_flow       : per-share growth derivation chain
#   - variable_impact    : multi-variable Δ-sensitivity table (linearized)
#   - decision           : "what must happen for upside" framing block
BlockKind = Literal[
    "narrative", "table", "metric", "chart", "quote", "list",
    "expectations_table", "three_box", "debate",
    "capital_flow", "variable_impact", "decision",
]


class NormalizedBlock(BaseModel):
    """Adapter output: a single content unit, source-typed and citable.

    Block IDs are deterministic — same source + content → same block_id across
    re-runs.  This is required for KG ``deck_run`` snapshot stability.
    """
    model_config = ConfigDict(extra="forbid")
    block_id: str = Field(description="Stable deterministic ID: {source_type}_{source_hash}_{idx}")
    kind: BlockKind
    title: str
    content: dict[str, Any] = Field(description="Kind-specific payload — see adapter docs")
    source_type: str = Field(description="DeckSource.type that produced this block")
    source_ref: str = Field(description="Original source identifier (run_id, doc_id, url, etc.)")
    evidence_refs: list[str] = Field(default_factory=list, description="Evidence IDs for citation drawer")
    suggested_slide_layouts: list[str] = Field(
        default_factory=list,
        description="Adapter hint, not binding — outline gen may override",
    )


# ---------------------------------------------------------------------------
# Outline + slide content schemas
# ---------------------------------------------------------------------------


SlideLayout = Literal[
    "title",
    "section_header",
    "bullets",
    "metric_callout",
    "narrative",
    "thesis",
    "scenario_table",
    "risk_summary",
    "chart_caption",
    "executive_summary",
    "references",
    # Expectations-deck layouts (added alongside legacy layouts so the new
    # institutional deck shape can render without breaking back-compat).
    "reconciliation_table",   # left col = model, right col = market-implied, delta col
    "three_box",              # three vertical boxes: priced / assumed / required
    "two_col_narrative",      # bull on left, bear on right
    "flow_diagram",           # vertical steps: FCFF → buybacks → per-share growth
    "variable_impact_table",  # variable | Δ | impact column layout
    "decision_summary",       # "what must happen for upside" closing slide
]


class OutlineSlide(BaseModel):
    """One slide spec produced by the outline node — content not yet rendered."""
    model_config = ConfigDict(extra="forbid")
    slide_id: str
    layout: SlideLayout
    title: str
    block_refs: list[str] = Field(default_factory=list, description="block_ids feeding this slide")
    notes: str = Field(default="", description="Outline-gen hint to per-slide LLM")


class DeckOutline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slides: list[OutlineSlide]
    rationale: str = Field(default="", description="Why this structure")


# ---------------------------------------------------------------------------
# Phase B2 — optional LLM-emitted declarative layout spec
# ---------------------------------------------------------------------------
#
# A ``LayoutSpec`` lets the per-slide LLM override the default per-layout
# geometry when the canned layout does not fit the content. It is a list of
# rectangular ``regions`` in *fractional* slide coordinates (0.0–1.0 of width /
# height, top-left origin), each carrying its content inline. It is purely an
# overlay: the LLM still populates the canonical SlideContent fields (so the
# deterministic fallback always has content), and ``assemble.py`` only renders
# the spec when ``is_renderable()`` passes — otherwise it falls back to the
# standard per-layout renderer. LLM-emitted geometry is unreliable, so the
# validation here is deliberately strict and coordinates are NOT enforced by
# pydantic Field constraints (an out-of-range value drops the spec gracefully
# instead of failing the whole SlideContent parse).


RegionKind = Literal["text", "bullets", "table", "image", "accent_bar"]
RegionRole = Literal["title", "heading", "body", "caption", "metric", "muted"]

# Bottom of the usable region (fraction of slide height). assemble.py paints a
# footer (slide number + citations) below this, so spec regions must stop above
# it or they overlap the footer text.
_FOOTER_TOP = 0.95


class LayoutRegion(BaseModel):
    """One rectangle in a ``LayoutSpec``, with inline content."""
    model_config = ConfigDict(extra="forbid")
    kind: RegionKind
    # Fractional rect (0.0–1.0 of slide w/h). Defaults make a missing/partial
    # region degenerate so ``in_bounds`` rejects it rather than crashing parse.
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    # Kind-specific inline content (all optional; validated by ``has_content``).
    text: str | None = None
    items: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    image_path: str | None = None
    # Styling hints.
    role: RegionRole | None = None
    align: Literal["left", "center", "right"] | None = None
    fill: Literal["panel", "accent", "none"] | None = None

    def in_bounds(self) -> bool:
        """True when the rect is non-degenerate and fits in the usable area.

        The bottom is capped at ``_FOOTER_TOP`` (not 1.0) so regions never
        overlap the footer strip assemble.py renders on every slide.
        """
        return (
            0.0 <= self.x <= 1.0
            and 0.0 <= self.y <= 1.0
            and self.w >= 0.03
            and self.h >= 0.02
            and self.x + self.w <= 1.001
            and self.y + self.h <= _FOOTER_TOP + 0.001
        )

    def has_content(self) -> bool:
        """True when the region actually carries something to render."""
        if self.kind == "accent_bar":
            return True
        if self.kind == "text":
            return bool(self.text and self.text.strip())
        if self.kind == "bullets":
            return any(str(i).strip() for i in self.items)
        if self.kind == "table":
            return any(self.rows)
        if self.kind == "image":
            return bool(self.image_path)
        return False


class LayoutSpec(BaseModel):
    """Declarative geometry overlay for one slide (optional)."""
    model_config = ConfigDict(extra="forbid")
    regions: list[LayoutRegion] = Field(default_factory=list)

    def is_renderable(self) -> bool:
        """Strict gate: every region in-bounds, sane count, real content present.

        When this returns False, ``assemble.py`` ignores the spec and uses the
        standard per-layout renderer instead.
        """
        if not self.regions or len(self.regions) > 12:
            return False
        if not all(r.in_bounds() for r in self.regions):
            return False
        # At least one non-decorative region must carry content.
        return any(r.kind != "accent_bar" and r.has_content() for r in self.regions)


class SlideContent(BaseModel):
    """Final per-slide content — rendered to PPTX by assemble.py."""
    model_config = ConfigDict(extra="forbid")
    slide_id: str
    layout: SlideLayout
    title: str
    body_bullets: list[str] = Field(default_factory=list)
    body_paragraphs: list[str] = Field(default_factory=list)
    table_rows: list[list[str]] = Field(default_factory=list, description="2D table, first row is header")
    chart_path: str | None = None
    chart_caption: str | None = None
    citations: list[str] = Field(default_factory=list, description="Evidence IDs cited on this slide")
    # ── Expectations-deck extension fields (optional, used by new layouts) ──
    # ``columns``: ordered multi-column blocks. Each entry: {"heading": str,
    # "bullets": list[str], "paragraphs": list[str]}. Used by `three_box`
    # (priced / assumed / required) and `two_col_narrative` (bull / bear).
    columns: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Ordered columns for multi-column layouts (three_box, two_col_narrative)",
    )
    # ``flow_steps``: ordered nodes in a flow_diagram, each {"label": str,
    # "detail": str}. Used by `flow_diagram` (FCFF → buybacks → per-share growth).
    flow_steps: list[dict[str, str]] = Field(
        default_factory=list,
        description="Ordered steps for flow_diagram layout",
    )
    # ── Phase B2: optional declarative geometry overlay ──────────────────────
    # When present AND ``is_renderable()``, assemble.py renders these regions
    # instead of the per-layout renderer. Null = use the standard layout.
    layout_spec: LayoutSpec | None = Field(
        default=None,
        description="Optional custom region geometry; overrides the default layout when valid",
    )


# ---------------------------------------------------------------------------
# Per-slide LLM routing — cheap models for formatting, capable for judgment
# ---------------------------------------------------------------------------


SLIDE_MODELS: dict[str, str] = {
    "title":             "gpt-4o-mini",
    "section_header":    "gpt-4o-mini",
    "bullets":           "gpt-4o-mini",
    "metric_callout":    "gpt-4o-mini",
    "narrative":         "gpt-4o",
    "thesis":            "gpt-4o",
    "scenario_table":    "gpt-4o-mini",
    "risk_summary":      "gpt-4o",
    "chart_caption":     "gpt-4o-mini",
    "executive_summary": "gpt-4o",
    "references":        "gpt-4o-mini",
    # Expectations-deck layouts — all narrative/judgment heavy, route to gpt-4o
    # so the LLM can produce the sharp institutional framing. The table-like
    # ones still benefit from gpt-4o because the column headings and impact
    # phrasing need to be tight, not formulaic.
    "reconciliation_table":  "gpt-4o",
    "three_box":             "gpt-4o",
    "two_col_narrative":     "gpt-4o",
    "flow_diagram":          "gpt-4o",
    "variable_impact_table": "gpt-4o-mini",
    "decision_summary":      "gpt-4o",
}


# ---------------------------------------------------------------------------
# DeckState — LangGraph TypedDict
# ---------------------------------------------------------------------------


class DeckState(TypedDict, total=False):
    # Input
    sources: list[dict]              # raw discriminated-union dicts (validated upstream)
    brief: dict                      # DeckBrief as dict
    # Normalize stage
    blocks: list[dict]               # NormalizedBlock list
    blocks_by_id: dict[str, dict]    # O(1) lookup
    evidence_index: dict[str, dict]  # evidence_id → evidence item (for readable citations)
    # Outline + HITL
    outline: dict                    # DeckOutline as dict
    outline_approved: bool
    outline_feedback: str | None
    # Per-slide content
    slides: list[dict]               # SlideContent list
    # Rendering
    artifacts: list[dict]            # [{kind, path, slide_id}]
    pptx_path: str | None
    pdf_path: str | None
    html_path: str | None
    # KG snapshot
    deck_run_id: str | None
    deck_output_path: str | None
    # Plumbing
    session_id: str
    parent_step_id: str
    hitl_mode: str                   # mirrors brief.hitl_mode for quick read


__all__ = [
    "DcfOutputSource",
    "DocumentSource",
    "WebSource",
    "ManualTextSource",
    "KgSubgraphSource",
    "ChartArtifactSource",
    "DeckSource",
    "DeckBrief",
    "HitlMode",
    "NormalizedBlock",
    "BlockKind",
    "OutlineSlide",
    "DeckOutline",
    "SlideContent",
    "SlideLayout",
    "LayoutRegion",
    "LayoutSpec",
    "SLIDE_MODELS",
    "DeckState",
]
