"""Outline generation — one LLM call that picks slides and assigns blocks.

The LLM receives:
  - The ``DeckBrief`` (title, audience, slide_count_target, tone, must_cover).
  - A compact summary of every available block (id, kind, title, source_type).

It returns a ``DeckOutline`` — list of ``OutlineSlide`` specs, each with a
layout, title, the ``block_refs`` (IDs of blocks feeding that slide), and a
short ``notes`` hint for the per-slide content node.

The LLM does NOT write final body text here — that is the per-slide stage
where model routing kicks in.  This separation keeps the outline cheap
(one ``gpt-4o-mini`` call regardless of deck length) and isolates the
expensive narrative work to slides that actually need ``gpt-4o``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from langchain_openai import ChatOpenAI

from .activity import emit_step
from .state import DeckBrief, DeckOutline, DeckState, OutlineSlide
from .state import SlideLayout as _SlideLayout

# Valid layout names — used to guard free-form adapter hints in the fallback.
_VALID_LAYOUTS: frozenset[str] = frozenset(_SlideLayout.__args__)  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)


# Lazily initialized — avoids OpenAI key check at import time.
_OUTLINE_LLM: ChatOpenAI | None = None


def _get_outline_llm() -> ChatOpenAI:
    global _OUTLINE_LLM
    if _OUTLINE_LLM is None:
        _OUTLINE_LLM = ChatOpenAI(
            model=os.getenv("DECK_OUTLINE_MODEL", "gpt-4o-mini"),
            api_key=os.getenv("OPENAI_API_KEY"),
            timeout=90,
        )
    return _OUTLINE_LLM


_EXPECTATIONS_BLOCK_KINDS = {
    "expectations_table", "three_box", "debate",
    "capital_flow", "variable_impact", "decision",
}

_OUTLINE_SYSTEM_PROMPT = """You are a senior analyst designing a slide deck outline.

You receive a brief (audience, tone, target slide count, must-cover topics)
and an inventory of content blocks already prepared by the data team.  Your
job is to choose which slides exist, in what order, with what layout, and
which block(s) feed each one.

Rules:
1. Every slide MUST reference at least one block_id from the inventory.
   Do NOT invent block_ids — only use IDs that appear in the inventory list.
2. A block may feed multiple slides (e.g. a thesis narrative block can feed
   both the title slide tagline and a dedicated thesis slide).
3. Honor must_cover topics — each must appear as a slide if relevant blocks
   exist.  If no block supports a must_cover topic, omit it but flag in
   rationale.
4. Respect slide_count_target ±2.  When unset, aim for 6-10 slides.
5. Available layouts:
   Legacy: title, section_header, bullets, metric_callout, narrative,
       thesis, scenario_table, risk_summary, chart_caption,
       executive_summary, references.
   Expectations-deck: reconciliation_table, three_box, two_col_narrative,
       flow_diagram, variable_impact_table, decision_summary.
6. Block kind → preferred layout mapping (use these when the block kind
   appears in the inventory):
       expectations_table → reconciliation_table
       three_box          → three_box
       debate             → two_col_narrative
       capital_flow       → flow_diagram
       variable_impact    → variable_impact_table
       decision           → decision_summary
       metric             → metric_callout
       chart              → chart_caption
       narrative          → narrative or thesis
       list               → risk_summary or bullets
       table              → scenario_table
7. First slide should be 'title' layout.  If the brief implies a financial
   deck and a 'metric' block exists, place an 'executive_summary' or
   'metric_callout' slide second.
8. Use 'chart_caption' layout whenever a 'chart' block is included.
9. End with 'references' layout — its block_refs list ALL block_ids cited
   anywhere in the deck (the assembler renders the consolidated sources).
10. NEVER use 'section_header' layout for must_cover topics or for any slide
    that should carry analytical content (scenarios, sensitivity, thesis,
    risks, assumptions, valuation). A 'section_header' is a title-only slide
    and must reference at least one block — if no supporting block exists,
    OMIT the slide entirely and note the gap in `rationale`. Do not produce
    filler title-only slides for missing must_cover topics.

Output valid JSON matching the schema exactly.  No commentary."""


_EXPECTATIONS_TEMPLATE_HINT = """## Recommended deck shape (expectations-first)

The inventory contains expectations-first blocks. This signals an
institutional DCF deck — frame it as **market expectations vs fundamental
reality**, not as "here is my spreadsheet output".

Strongly preferred slide order (adapt titles to the ticker / brief):

  1. title              — "{ticker} — Market Expectations vs Fundamental Reality"
                          (or similar reconciliation framing)
  2. three_box          — exec summary: priced / assumed / required
                          (uses the `three_box` block)
  3. reconciliation_table — model vs market-implied (WACC, growth, terminal)
                          (uses the `expectations_table` block)
  4. variable_impact_table — value drivers that actually matter
                          (uses the `variable_impact` block)
  5. flow_diagram       — capital allocation: enterprise → per-share growth
                          (uses the `capital_flow` block; OMIT if no buyback)
  6. two_col_narrative  — core debate: bull vs bear
                          (uses the `debate` block)
  7. chart_caption      — sensitivity heatmap (if `chart` block exists)
  8. decision_summary   — "what must happen for upside from here"
                          (uses the `decision` block)
  9. references

Drop any slide whose source block is missing from the inventory. Do not
substitute legacy blocks for expectations blocks when both are present.
The legacy `scenarios` / `metric` / `valuation_table` blocks are FALLBACK
content — only add them if the brief's must_cover demands them AND the
expectations counterpart isn't available."""


def _format_block_inventory(blocks: list[dict]) -> str:
    """Compact block listing for the LLM prompt — IDs + minimal metadata."""
    lines: list[str] = []
    for b in blocks:
        # Trim title; LLM cares about ID, kind, layout hints, source.
        title = str(b.get("title") or "")[:80]
        hints = ", ".join(b.get("suggested_slide_layouts") or []) or "—"
        lines.append(
            f"- {b['block_id']} | kind={b['kind']} | source={b['source_type']} "
            f"| title={title!r} | hints=[{hints}]"
        )
    return "\n".join(lines)


def _has_expectations_blocks(blocks: list[dict]) -> bool:
    return any(b.get("kind") in _EXPECTATIONS_BLOCK_KINDS for b in blocks)


def _extract_ticker_hint(brief: DeckBrief, blocks: list[dict]) -> str:
    """Best-effort ticker pull for the template title example."""
    title = (brief.title or "").strip()
    # Common deck titles start with a ticker token (e.g. "AAPL — DCF Case").
    if title:
        first_token = title.split()[0].strip(" —-:|")
        if first_token.isupper() and 1 <= len(first_token) <= 6:
            return first_token
    # Fallback: scan block titles for an uppercase short token.
    for b in blocks:
        t = str(b.get("title") or "")
        for tok in t.split():
            tok = tok.strip(" —-:|")
            if tok.isupper() and 1 <= len(tok) <= 6:
                return tok
    return "TICKER"


def _build_prompt(brief: DeckBrief, blocks: list[dict]) -> str:
    inventory = _format_block_inventory(blocks)
    brief_json = json.dumps(brief.model_dump(), ensure_ascii=False, indent=2)

    template_section = ""
    if _has_expectations_blocks(blocks):
        ticker = _extract_ticker_hint(brief, blocks)
        template_section = (
            "\n\n"
            + _EXPECTATIONS_TEMPLATE_HINT.replace("{ticker}", ticker)
            + "\n"
        )

    return (
        f"## Deck brief\n```json\n{brief_json}\n```\n\n"
        f"## Available content blocks ({len(blocks)} total)\n{inventory}"
        f"{template_section}\n\n"
        "## Task\n"
        "Produce a `DeckOutline` JSON object with `slides` (list of "
        "`OutlineSlide`) and a short `rationale` explaining the structure.\n"
    )


def generate_outline_node(state: DeckState) -> dict:
    """Produce the deck outline from brief + normalized blocks."""
    parent_step_id = state.get("parent_step_id") or "workflow_deck"
    emit_step("generate_outline", "start", parent_step_id)

    blocks = state.get("blocks") or []
    brief_dict = state.get("brief") or {}
    try:
        brief = DeckBrief.model_validate(brief_dict)
    except Exception as exc:  # noqa: BLE001
        emit_step("generate_outline", "error", parent_step_id, {
            "summary_line": f"Invalid brief: {exc}",
        })
        raise

    if not blocks:
        emit_step("generate_outline", "error", parent_step_id, {
            "summary_line": "No blocks available — normalize stage produced nothing.",
        })
        raise ValueError("generate_outline_node received empty blocks list.")

    prompt = _build_prompt(brief, blocks)
    block_ids = {b["block_id"] for b in blocks}

    try:
        structured = _get_outline_llm().with_structured_output(DeckOutline)
        result = structured.invoke([
            {"role": "system", "content": _OUTLINE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ])
        outline = result if isinstance(result, DeckOutline) else DeckOutline.model_validate(result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Outline LLM call failed")
        emit_step("generate_outline", "error", parent_step_id, {
            "summary_line": f"Outline LLM failed: {exc}",
            "fallback": "deterministic",
        })
        # Deterministic fallback: one slide per block plus title + refs.
        outline = _deterministic_fallback_outline(blocks)

    # Repair pass: drop block_refs that don't exist; warn rather than abort.
    repaired_slides: list[OutlineSlide] = []
    dropped_refs: list[str] = []
    for slide in outline.slides:
        valid_refs = [r for r in slide.block_refs if r in block_ids]
        if len(valid_refs) != len(slide.block_refs):
            dropped_refs.extend([r for r in slide.block_refs if r not in block_ids])
        repaired_slides.append(slide.model_copy(update={"block_refs": valid_refs}))

    # Drop slides that ended up with zero valid refs (unrenderable).
    # 'section_header' previously got a free pass, but those produced empty
    # title-only filler slides for must_cover topics with no supporting
    # blocks. Require a real block ref now — only 'title' and 'references'
    # are allowed to have empty block_refs (they render off deck-level data).
    rendered_slides = [
        s for s in repaired_slides
        if s.block_refs or s.layout in {"title", "references"}
    ]
    dropped_empty_section_headers = [
        s.slide_id for s in repaired_slides
        if not s.block_refs and s.layout == "section_header"
    ]
    if dropped_empty_section_headers:
        logger.info(
            "Outline: dropped %d empty section_header slide(s): %s",
            len(dropped_empty_section_headers), dropped_empty_section_headers,
        )
    outline = outline.model_copy(update={"slides": rendered_slides})

    emit_step("generate_outline", "complete", parent_step_id, {
        "summary_line": f"{len(outline.slides)} slide(s): " + " → ".join(
            f"{s.layout}" for s in outline.slides[:8]
        ) + ("…" if len(outline.slides) > 8 else ""),
        "slide_count": len(outline.slides),
        "rationale": outline.rationale[:200],
        "dropped_block_refs": dropped_refs[:10],
        "outline": outline.model_dump(),
    })

    return {
        "outline": outline.model_dump(),
        "hitl_mode": brief.hitl_mode,
    }


def _deterministic_fallback_outline(blocks: list[dict]) -> DeckOutline:
    """Build a minimal outline when the LLM call fails — one slide per block."""
    slides: list[OutlineSlide] = [
        OutlineSlide(
            slide_id="s0_title",
            layout="title",
            title="Deck",
            block_refs=[blocks[0]["block_id"]] if blocks else [],
            notes="Auto-generated title slide (LLM fallback).",
        )
    ]
    for i, b in enumerate(blocks, start=1):
        # Pick first suggested layout that is a real SlideLayout, else 'bullets'.
        # `suggested_slide_layouts` is a free-form list[str] adapter hint — an
        # invalid value here would make OutlineSlide() raise, defeating the
        # whole point of this no-LLM fallback.
        hints = b.get("suggested_slide_layouts") or []
        layout_hint = next((h for h in hints if h in _VALID_LAYOUTS), "bullets")
        slides.append(OutlineSlide(
            slide_id=f"s{i}_{b['block_id'][:10]}",
            layout=layout_hint,  # type: ignore[arg-type]
            title=str(b.get("title") or "Slide"),
            block_refs=[b["block_id"]],
            notes="Fallback: one block per slide.",
        ))
    slides.append(OutlineSlide(
        slide_id="s_last_refs",
        layout="references",
        title="Sources",
        block_refs=[b["block_id"] for b in blocks],
        notes="Consolidated references.",
    ))
    return DeckOutline(slides=slides, rationale="Deterministic fallback — LLM outline call failed.")


__all__ = ["generate_outline_node"]
