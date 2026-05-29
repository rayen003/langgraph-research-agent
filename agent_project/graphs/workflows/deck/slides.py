"""Per-slide content generation — one LLM call per slide with model routing.

For each ``OutlineSlide``:
  - Resolve ``block_refs`` to full ``NormalizedBlock`` payloads.
  - Pick the model from ``SLIDE_MODELS`` (gpt-4o-mini for layout/format,
    gpt-4o for thesis/narrative/executive_summary).
  - Build a layout-aware prompt that constrains the LLM to fields it should
    populate (bullets vs paragraphs vs tables).
  - Invoke with structured output (``SlideContent`` Pydantic model).
  - Fall back to deterministic stub when LLM fails — never block the deck.

Future: per-(layout, block_kinds, audience) template cache here (drop-in
short-circuit before the LLM call).  Not implemented in MVP.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from functools import lru_cache
from typing import Any

from langchain_openai import ChatOpenAI

from .activity import emit_step
from .state import (
    DeckState,
    OutlineSlide,
    SlideContent,
    SlideLayout,
    SLIDE_MODELS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model factory (one client per distinct model name, reused across slides)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _get_llm(model_name: str) -> ChatOpenAI:
    return ChatOpenAI(
        model=model_name,
        api_key=os.getenv("OPENAI_API_KEY"),
        timeout=90,
    )


def _model_for(layout: str) -> str:
    """Resolve which model handles a given layout, honoring env overrides.

    Env overrides:
      - DECK_MODEL_DEFAULT — fallback when layout not in SLIDE_MODELS.
      - DECK_MODEL_<LAYOUT_UPPER> — pin a specific layout (e.g. DECK_MODEL_THESIS).
    """
    env_key = f"DECK_MODEL_{layout.upper()}"
    pinned = os.getenv(env_key)
    if pinned:
        return pinned
    return SLIDE_MODELS.get(layout, os.getenv("DECK_MODEL_DEFAULT", "gpt-4o-mini"))


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """You are writing the final content for ONE slide of an analyst deck.

You receive the slide spec (layout, title, notes) and the content blocks
that feed it.  Produce a `SlideContent` JSON object with the fields that
match the layout:

LEGACY LAYOUTS:
- title / section_header: title only; body_bullets and body_paragraphs empty.
- bullets / risk_summary: 3-6 short bullets in `body_bullets`.
- metric_callout: 1-2 sentences in `body_paragraphs` framing the key metric.
- narrative / thesis / executive_summary: 1-3 short paragraphs in `body_paragraphs`.
- scenario_table: header row + data rows in `table_rows` (first row = header).
- chart_caption: 1 paragraph in `body_paragraphs` + `chart_caption`; set
  `chart_path` from the chart block's content.path.
- references: build `body_bullets` of citation strings; one per block.

EXPECTATIONS-DECK LAYOUTS (institutional shape — content is sharp, not promotional):
- reconciliation_table: ONE leader paragraph in `body_paragraphs` stating
  the headline tension (e.g. "Apple trades at expectations the cited
  assumptions do not yet justify"). Then 3-4 rows in `table_rows`. First
  row = header ["Metric", "Model", "Market-Implied", "Gap"]. Use the
  numbers from the `expectations_table` block's `signals` and `rows`.
- three_box: produce EXACTLY 3 entries in `columns`. Each entry =
  {"heading": str, "bullets": [str, ...]}. Heading must be one of:
  "Market Is Pricing", "We Assume", "What Must Be True". Bullets must be
  short noun-phrase claims (≤14 words each, no full sentences). Pull from
  the `three_box` block's `priced` / `assumed` / `required` content keys.
- two_col_narrative: EXACTLY 2 entries in `columns`. Each entry =
  {"heading": "Bull View" | "Bear View", "paragraphs": [str, ...]}. 1-3
  paragraphs each, ≤45 words. Sharper than sell-side language; quantify
  where possible; avoid "strong / robust / challenging" filler.
- flow_diagram: 3-5 entries in `flow_steps`. Each =
  {"label": str (short noun), "detail": str (one short clause)}. Sequence
  should derive `g_per_share` step-by-step from the `capital_flow` block.
  Optionally put a one-sentence framing in `body_paragraphs[0]`.
- variable_impact_table: header + rows in `table_rows`. Header =
  ["Variable", "Δ", "Impact"]. Each row = the variable name, the delta
  label (e.g. "−100 bps"), and the signed impact (e.g. "+42%"). Put the
  block's `caveat` into `chart_caption`.
- decision_summary: ONE strong framing paragraph in `body_paragraphs[0]`
  (the lead statement, ≤55 words). Then 3-5 required conditions in
  `body_bullets`, each phrased as a precondition (e.g. "Sustained sub-7%
  equity discount rates"). Do NOT use "we believe" / "in our view".

VOICE RULES (apply across all layouts):
- Quantify wherever possible. Replace vague qualifiers ("strong demand",
  "robust momentum") with explicit numbers from the blocks.
- Surface tensions and contradictions, not consensus narratives.
- Make one analytical point per slide. Multiple points → split.

BANNED PHRASES (do NOT use any of these — they are sell-side filler):
  "robust", "strong demand", "strong momentum", "well-positioned",
  "challenging macro", "challenging backdrop", "headwinds", "tailwinds",
  "execution risk", "we believe", "in our view", "going forward",
  "best-in-class", "category leader", "iconic", "ecosystem moat".
If a thesis/critique block uses these words, REWRITE the sentence with
quantified specifics drawn from the surrounding numbers. Example:
  BAD:  "Apple's robust revenue growth, driven by strong iPhone demand,
         positions it for continued momentum."
  GOOD: "Model assumes 19.5% near-term revenue growth (vs 28.9% implied)
         — gap of 9.4pp implies market is underwriting an upside scenario
         the cited assumptions do not justify."

PER-LAYOUT SCHEMA REMINDER (your JSON MUST populate these fields or the
slide will render blank):
  - three_box           → `columns` (3 entries, headings: "Market Is
                          Pricing", "We Assume", "What Must Be True")
  - two_col_narrative   → `columns` (2 entries, headings: "Bull View",
                          "Bear View")
  - flow_diagram        → `flow_steps` (3-5 entries, each with label+detail)
  - reconciliation_table → `table_rows` (header + 3-4 data rows)
  - variable_impact_table → `table_rows` + `chart_caption` (caveat)
  - decision_summary    → `body_paragraphs[0]` (lead) + `body_bullets` (conditions)
A slide is BLANK if the required field above is missing — your output
MUST include it for the layout to render.

OPTIONAL CUSTOM LAYOUT (`layout_spec`):
Leave `layout_spec` null to use the default layout — that is almost always the
right choice. Only emit one when the standard layout genuinely cannot fit the
content (e.g. a metric beside a chart). It does NOT replace the normal content
fields above — still populate those, since they are the fallback.
If you do emit one, it is `{"regions": [...]}`, each region a rectangle in
FRACTIONAL slide coordinates (0.0–1.0 of width / height, top-left origin):
  {"kind": "text"|"bullets"|"table"|"image"|"accent_bar",
   "x", "y", "w", "h": floats in [0,1] (x+w<=1, y+h<=1; keep y+h<=0.92 so the
                       footer stays clear; w>=0.03, h>=0.02),
   "text": str            (kind=text),
   "items": [str]         (kind=bullets),
   "rows": [[str], ...]   (kind=table, first row = header),
   "image_path": str      (kind=image; copy from a chart block's content.path),
   "role": "title"|"heading"|"body"|"caption"|"metric"|"muted",
   "align": "left"|"center"|"right",
   "fill": "panel"|"accent"|"none" (accent_bar/decoration)}
Put ALL content inline in the regions. Max 12 regions. A spec that overflows
the slide or has no real content is ignored — when unsure, omit it.

Rules:
1. ONLY use facts present in the provided blocks.  Do NOT invent numbers,
   tickers, dates, or quotes.
2. Cite block IDs in `citations` for every block you draw from.
3. Bullets: <=18 words each, no trailing periods unless full sentence.
4. Paragraphs: <=60 words each.
5. Always set slide_id, layout, title to the values from the spec — do not change them.
6. Output valid JSON matching the schema exactly.  No commentary."""


def _serialize_block_for_prompt(block: dict) -> dict:
    """Strip block dict to the fields the slide LLM should see."""
    return {
        "block_id": block["block_id"],
        "kind": block["kind"],
        "title": block.get("title"),
        "source_type": block.get("source_type"),
        "content": block.get("content"),
    }


def _build_slide_prompt(
    slide: OutlineSlide,
    resolved_blocks: list[dict],
    brief: dict[str, Any],
) -> str:
    import json as _json
    return (
        f"## Slide spec\n```json\n{_json.dumps(slide.model_dump(), ensure_ascii=False, indent=2)}\n```\n\n"
        f"## Deck brief context\n"
        f"audience={brief.get('audience')!r}  tone={brief.get('tone')!r}  title={brief.get('title')!r}\n\n"
        f"## Blocks feeding this slide ({len(resolved_blocks)})\n"
        f"```json\n{_json.dumps(resolved_blocks, ensure_ascii=False, indent=2)}\n```\n\n"
        "## Task\n"
        f"Produce the SlideContent for layout={slide.layout!r}.  "
        f"Use only the blocks above.  Title must be: {slide.title!r}."
    )


# ---------------------------------------------------------------------------
# LLM-output structural validation
# ---------------------------------------------------------------------------


# Layout → name of the SlideContent field that MUST be non-empty for that
# layout to render meaningfully in assemble.py. Used to detect when the LLM
# returned valid JSON but forgot the layout-specific payload (the most
# common failure mode for the new expectations layouts).
_REQUIRED_FIELD_BY_LAYOUT: dict[str, str] = {
    "three_box": "columns",
    "two_col_narrative": "columns",
    "flow_diagram": "flow_steps",
    "reconciliation_table": "table_rows",
    "variable_impact_table": "table_rows",
    "scenario_table": "table_rows",
    "decision_summary": "body_paragraphs",
    "metric_callout": "body_paragraphs",
    "narrative": "body_paragraphs",
    "thesis": "body_paragraphs",
    "executive_summary": "body_paragraphs",
    "chart_caption": "chart_path",
    "bullets": "body_bullets",
    "risk_summary": "body_bullets",
}


def _missing_required_field(content: SlideContent) -> str | None:
    """Return the missing required-field name for this layout, or None."""
    field = _REQUIRED_FIELD_BY_LAYOUT.get(content.layout)
    if field is None:
        return None
    value = getattr(content, field, None)
    if value is None:
        return field
    # Empty list / empty string both count as "missing".
    if isinstance(value, (list, str)) and not value:
        return field
    return None


def _slide_is_structurally_blank(content: SlideContent) -> bool:
    """True when LLM returned a slide that will render essentially empty."""
    return _missing_required_field(content) is not None


# ---------------------------------------------------------------------------
# Fallback content generator (deterministic, no LLM)
# ---------------------------------------------------------------------------


# Mapping from raw `content.{key}` names to human-readable column labels.
# Used by the three_box / decision fallbacks so we never expose snake_case
# identifiers (e.g. `revenue_growth_near`) to slide viewers.
_HUMAN_LABEL: dict[str, str] = {
    "implied_wacc": "Implied WACC",
    "implied_growth": "Implied growth",
    "implied_margin": "Implied margin",
    "wacc_plausibility": "WACC stance",
    "wacc_narrative": "WACC read",
    "lifecycle_stage": "Lifecycle",
    "margin_trajectory": "Margin trajectory",
    "capital_return_policy": "Capital return",
    "divergence_kinds": "Open divergences",
    "revenue_growth_near": "Revenue growth (near-term)",
    "revenue_growth_terminal": "Revenue growth (terminal)",
    "fcff_margin_near": "FCFF margin (near-term)",
    "fcff_margin_terminal": "FCFF margin (terminal)",
    "terminal_growth": "Terminal growth",
    "buyback_yield": "Buyback yield",
    "model_wacc": "Model WACC",
    "tax_rate": "Tax rate",
    "implied_share_price": "Implied share price",
    "current_price": "Spot price",
    "reconciliation_status": "Reconciliation status",
    "reconciliation_note": "Reconciliation note",
    "critique_summary": "Critique",
    "critique_concerns": "Critique concerns",
    "key_risks": "Key risk",
    "growth_drivers": "Growth driver",
    "macro_context": "Macro context",
    "competitive_position": "Competitive position",
}


def _humanize_key(key: str) -> str:
    """Return a human-readable label for a raw content key."""
    if key in _HUMAN_LABEL:
        return _HUMAN_LABEL[key]
    return key.replace("_", " ").capitalize()


def _format_scalar(value: Any) -> str:
    """Render a scalar value for slide display. None / empty → empty string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.4g}"
    return str(value).strip()


def _flatten_priced_assumed(content_block: dict[str, Any]) -> list[str]:
    """Turn priced/assumed dicts into a list of clean labelled bullets.

    Rules:
      - Skip None / empty values.
      - Skip nested dict / list values entirely (those should never be
        dumped raw — fallback's job is graceful, not faithful).
      - Use human labels via ``_HUMAN_LABEL``.
    """
    bullets: list[str] = []
    for raw_key, raw_value in content_block.items():
        if raw_value is None or raw_value == "":
            continue
        if isinstance(raw_value, (dict, list)):
            continue
        label = _humanize_key(str(raw_key))
        bullets.append(f"{label}: {_format_scalar(raw_value)}")
    return bullets[:6]


def _flatten_required_column(content_block: dict[str, Any]) -> list[str]:
    """Build the "What Must Be True" column bullets from the `required` dict.

    Pulls the most important narrative pieces (divergences, reconciliation
    note) and drops raw IDs / nested critique structures.
    """
    bullets: list[str] = []
    divergences = content_block.get("divergences") or []
    for d in divergences[:3]:
        summary = d.get("summary") if isinstance(d, dict) else None
        if summary:
            bullets.append(str(summary)[:120])
    status = content_block.get("reconciliation_status")
    if status:
        bullets.append(f"Reconciliation: {status}")
    note = content_block.get("reconciliation_note")
    if note and len(bullets) < 5:
        bullets.append(str(note)[:140])
    return bullets[:6]


def _deterministic_slide_content(
    slide: OutlineSlide,
    resolved_blocks: list[dict],
) -> SlideContent:
    """Build a usable slide from blocks without calling the LLM.

    Used when the LLM call fails so the deck still renders end-to-end.
    Quality is poor but format is correct.
    """
    bullets: list[str] = []
    paragraphs: list[str] = []
    table_rows: list[list[str]] = []
    chart_path: str | None = None
    chart_caption: str | None = None
    columns: list[dict[str, Any]] = []
    flow_steps: list[dict[str, str]] = []
    citations = [b["block_id"] for b in resolved_blocks]

    for b in resolved_blocks:
        kind = b.get("kind")
        content = b.get("content") or {}
        title = b.get("title", "")
        if kind == "chart" and not chart_path:
            chart_path = content.get("path")
            chart_caption = content.get("caption") or title
        elif kind == "metric":
            paragraphs.append(
                f"{title}: implied ${content.get('implied_price')} vs spot "
                f"${content.get('spot_price')} ({content.get('upside_pct')}% upside)."
            )
        elif kind == "narrative":
            for field in ("narrative", "bull", "bear"):
                v = content.get(field)
                if v:
                    paragraphs.append(str(v)[:300])
        elif kind == "list":
            for item in (content.get("items") or [])[:6]:
                bullets.append(str(item)[:140])
        elif kind == "table":
            rows = content.get("rows") or []
            if rows and isinstance(rows[0], dict):
                header = list(rows[0].keys())
                table_rows.append([str(h) for h in header])
                for r in rows[:8]:
                    table_rows.append([str(r.get(h, "")) for h in header])
            elif "assumptions" in content:
                table_rows.append(["Field", "Value"])
                for k, v in (content.get("assumptions") or {}).items():
                    table_rows.append([str(k), str(v)])
        # ── Expectations-deck block kinds ───────────────────────────────
        elif kind == "expectations_table":
            # Build table_rows from the block's `rows` list of dicts.
            rows = content.get("rows") or []
            if rows:
                header = ["Metric", "Model", "Market-Implied", "Gap"]
                table_rows.append(header)
                for r in rows[:6]:
                    metric = str(r.get("metric") or "")
                    model = str(r.get("model") or "")
                    implied = str(
                        r.get("market_implied")
                        or r.get("effective_with_buybacks")
                        or "",
                    )
                    gap_parts = []
                    if r.get("delta_bps") is not None:
                        gap_parts.append(f"{r['delta_bps']:.0f} bps")
                    if r.get("delta_pp") is not None:
                        gap_parts.append(f"{r['delta_pp']:.1f} pp")
                    table_rows.append([metric, model, implied, ", ".join(gap_parts)])
            summary = content.get("summary_hint") or ""
            if summary:
                paragraphs.append(str(summary)[:280])
        elif kind == "three_box":
            # priced / assumed are flat-ish dicts → humanize labels + skip nesting.
            # required carries divergences + reconciliation_note → use dedicated builder.
            priced = content.get("priced") or {}
            assumed = content.get("assumed") or {}
            required = content.get("required") or {}
            columns.append({
                "heading": "Market Is Pricing",
                "bullets": _flatten_priced_assumed(priced),
            })
            columns.append({
                "heading": "We Assume",
                "bullets": _flatten_priced_assumed(assumed),
            })
            columns.append({
                "heading": "What Must Be True",
                "bullets": _flatten_required_column(required),
            })
        elif kind == "debate":
            bull = content.get("bull") or ""
            bear = content.get("bear") or ""
            if bull:
                columns.append({"heading": "Bull View", "paragraphs": [str(bull)[:280]]})
            if bear:
                columns.append({"heading": "Bear View", "paragraphs": [str(bear)[:280]]})
        elif kind == "capital_flow":
            display = content.get("display") or {}
            flow_steps = [
                {"label": "Business growth (terminal)", "detail": str(display.get("business_growth") or "")},
                {"label": "Buyback yield", "detail": str(display.get("buyback_yield") or "")},
                {"label": "Share count shrinkage", "detail": str(display.get("shares_shrinkage") or "")},
                {"label": "Effective per-share growth", "detail": str(display.get("effective_per_share") or "")},
            ]
            hint = content.get("derivation_hint")
            if hint:
                paragraphs.append(str(hint)[:200])
        elif kind == "variable_impact":
            rows = content.get("rows") or []
            if rows:
                table_rows.append(["Variable", "Δ", "Impact"])
                for r in rows[:6]:
                    impact = r.get("impact_pct")
                    impact_str = f"{impact:+.1f}%" if isinstance(impact, (int, float)) else "—"
                    table_rows.append([
                        str(r.get("variable") or ""),
                        str(r.get("delta_label") or ""),
                        impact_str,
                    ])
            caveat = content.get("caveat")
            if caveat:
                chart_caption = str(caveat)[:220]
        elif kind == "decision":
            # Lead paragraph: reconciliation note (the "why upside is uncertain" line).
            note = content.get("reconciliation_note") or ""
            if note:
                paragraphs.append(str(note)[:300])
            # Required-condition bullets: derive from divergences (these are
            # genuine if/then preconditions). Skip framing_context entirely —
            # it's LLM context, not slide content. Dumping `key_risks: …`
            # and `growth_drivers: …` raw produced the broken slide 8.
            divergences = content.get("divergences") or []
            for d in divergences[:5]:
                if not isinstance(d, dict):
                    continue
                summary = d.get("summary")
                if summary:
                    bullets.append(str(summary)[:140])

    return SlideContent(
        slide_id=slide.slide_id,
        layout=slide.layout,
        title=slide.title,
        body_bullets=bullets[:6],
        body_paragraphs=paragraphs[:3],
        table_rows=table_rows,
        chart_path=chart_path,
        chart_caption=chart_caption,
        columns=columns,
        flow_steps=flow_steps,
        citations=citations,
    )


# ---------------------------------------------------------------------------
# Main node — loop over outline slides
# ---------------------------------------------------------------------------


def per_slide_generate_node(state: DeckState) -> dict:
    """Generate final content for each outline slide via per-slide LLM calls."""
    parent_step_id = state.get("parent_step_id") or "workflow_deck"
    emit_step("per_slide_generate", "start", parent_step_id)

    outline_dict = state.get("outline") or {}
    slides_raw = outline_dict.get("slides") or []
    if not slides_raw:
        emit_step("per_slide_generate", "error", parent_step_id, {
            "summary_line": "No slides in outline — nothing to generate.",
        })
        raise ValueError("per_slide_generate_node received empty outline.slides.")

    blocks_by_id = state.get("blocks_by_id") or {}
    brief = state.get("brief") or {}
    evidence_index = state.get("evidence_index") or {}

    rendered: list[SlideContent] = []
    fallback_count = 0
    model_usage: dict[str, int] = {}

    for slide_raw in slides_raw:
        slide = OutlineSlide.model_validate(slide_raw)
        resolved = [
            blocks_by_id[ref] for ref in slide.block_refs if ref in blocks_by_id
        ]
        if not resolved and slide.layout not in {"title", "section_header", "references"}:
            # Skip silently — outline_review repair should have caught this, but defensive.
            logger.warning("Slide %s has no resolvable blocks; skipping.", slide.slide_id)
            continue

        # Special-case references slide: build deterministically from all blocks
        # (LLM would just format strings).
        if slide.layout == "references":
            rendered.append(
                _build_references_slide(slide, list(blocks_by_id.values()), evidence_index)
            )
            model_usage["deterministic"] = model_usage.get("deterministic", 0) + 1
            continue

        # Special-case title slide: build deterministically so the cover always
        # carries a subtitle (the LLM often returns a bare title).
        if slide.layout == "title":
            rendered.append(_build_title_slide(slide, brief))
            model_usage["deterministic"] = model_usage.get("deterministic", 0) + 1
            continue

        model_name = _model_for(slide.layout)
        model_usage[model_name] = model_usage.get(model_name, 0) + 1

        prompt = _build_slide_prompt(slide, [_serialize_block_for_prompt(b) for b in resolved], brief)
        content: SlideContent | None = None
        try:
            llm = _get_llm(model_name)
            # method="function_calling" is REQUIRED — do not revert to the default.
            # SlideContent has bare-dict fields (`columns`, `flow_steps`) which emit
            # `additionalProperties: true`. The default strict json_schema mode rejects
            # that with a 400 ("'additionalProperties' is required ... to be false"),
            # which silently forced EVERY slide to deterministic fallback content.
            structured = llm.with_structured_output(SlideContent, method="function_calling")
            result = structured.invoke([
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ])
            content = result if isinstance(result, SlideContent) else SlideContent.model_validate(result)
            # Enforce LLM did not change identity fields.
            content = content.model_copy(update={
                "slide_id": slide.slide_id,
                "layout": slide.layout,
                "title": slide.title,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slide %s LLM call failed (%s) — will fallback.", slide.slide_id, exc)

        # Per-layout schema validation. The LLM frequently returns valid
        # SlideContent JSON but forgets the layout-specific fields (e.g.
        # `columns` for three_box) — that produces a title-only slide.
        # Retry ONCE with an explicit "you forgot the required field" nudge
        # before giving up to the deterministic fallback.
        if content is not None and _slide_is_structurally_blank(content):
            missing = _missing_required_field(content)
            logger.info(
                "Slide %s layout=%s missing required field=%s — retrying LLM with explicit instruction.",
                slide.slide_id, slide.layout, missing,
            )
            try:
                retry_prompt = (
                    prompt
                    + "\n\n## CRITICAL\n"
                    + f"Your previous output had an EMPTY `{missing}` field, which is REQUIRED "
                    + f"for layout={slide.layout!r}. Re-emit the SlideContent JSON with the "
                    + f"`{missing}` field populated. See the PER-LAYOUT SCHEMA REMINDER section."
                )
                llm = _get_llm(model_name)
                # method="function_calling" REQUIRED here too — see note at the
                # primary call site above (bare-dict fields break strict json_schema).
                structured = llm.with_structured_output(SlideContent, method="function_calling")
                result = structured.invoke([
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": retry_prompt},
                ])
                retried = result if isinstance(result, SlideContent) else SlideContent.model_validate(result)
                retried = retried.model_copy(update={
                    "slide_id": slide.slide_id,
                    "layout": slide.layout,
                    "title": slide.title,
                })
                if not _slide_is_structurally_blank(retried):
                    content = retried
                else:
                    logger.warning(
                        "Slide %s retry still missing %s — falling back deterministically.",
                        slide.slide_id, missing,
                    )
                    content = None
            except Exception as retry_exc:  # noqa: BLE001
                logger.warning("Slide %s LLM retry failed (%s).", slide.slide_id, retry_exc)
                content = None

        if content is None:
            content = _deterministic_slide_content(slide, resolved)
            fallback_count += 1

        # Phase B2: drop a non-renderable layout_spec so state stays clean and
        # assemble.py never has to guard garbage geometry. The canonical content
        # fields (validated above) carry the slide either way.
        if content.layout_spec is not None and not content.layout_spec.is_renderable():
            logger.debug(
                "Slide %s emitted non-renderable layout_spec — dropping it.",
                slide.slide_id,
            )
            content = content.model_copy(update={"layout_spec": None})

        rendered.append(content)

    emit_step("per_slide_generate", "complete", parent_step_id, {
        "summary_line": (
            f"{len(rendered)} slide(s) generated"
            + (f" ({fallback_count} fallback)" if fallback_count else "")
        ),
        "slide_count": len(rendered),
        "fallback_count": fallback_count,
        "model_usage": model_usage,
    })

    return {"slides": [s.model_dump() for s in rendered]}


# Acronyms to restore after .title()-casing humanized field/source names.
_CITATION_ACRONYMS = {
    "Fcff": "FCFF", "Wacc": "WACC", "Sbc": "SBC", "Capm": "CAPM",
    "Tgr": "TGR", "Eps": "EPS", "Roic": "ROIC", "Fmp": "FMP", "Sec": "SEC",
    "Ev": "EV", "Usd": "USD",
}


def _humanize(token: str) -> str:
    """``fcff_margin`` → ``FCFF Margin``; graceful for arbitrary IDs."""
    out = token.replace("_", " ").strip().title()
    for k, v in _CITATION_ACRONYMS.items():
        out = re.sub(rf"\b{k}\b", v, out)
    return out


def _format_citation(evidence_id: str, item: dict | None) -> str:
    """Render one evidence item as a human-readable citation line.

    Falls back to a humanized form of the raw ID when the item is unknown so
    the slide never leaks bare KG IDs like ``ev_fmp_fcff_margin``.
    """
    if not item:
        stripped = evidence_id
        for pre in ("ev_web_", "ev_fmp_", "ev_sec_", "ev_"):
            if stripped.startswith(pre):
                stripped = stripped[len(pre):]
                break
        return _humanize(stripped) or evidence_id

    source = str(item.get("source") or "")
    as_of = str(item.get("as_of") or "")
    # Trim ISO datetimes (``2026-05-01T14:53:28.000Z``) to the date.
    if "T" in as_of:
        as_of = as_of.split("T", 1)[0]
    kind = str(item.get("kind") or "")

    if kind == "structured_fundamental":
        src = "FMP" if source == "fmp" else _humanize(source)
        parts = [src, _humanize(str(item.get("field") or evidence_id))]
    elif kind == "filing_excerpt":
        src = "SEC EDGAR" if source == "sec_edgar" else _humanize(source)
        head = f"{src} {item.get('filing_type') or ''}".strip()
        parts = [head]
        if item.get("section"):
            parts.append(str(item["section"]))
    else:  # web / news / generic
        src = _humanize(source) if source else "Source"
        title = str(item.get("title") or item.get("text") or "").strip()
        parts = [src]
        if title:
            parts.append(title[:80])
    if as_of:
        parts.append(as_of)
    return " · ".join(p for p in parts if p)


def _build_references_slide(
    slide: OutlineSlide,
    all_blocks: list[dict],
    evidence_index: dict[str, dict] | None = None,
) -> SlideContent:
    """Deterministic reference slide builder — no LLM needed.

    Resolves each block's ``evidence_refs`` against ``evidence_index`` to render
    human-readable citations; unknown IDs degrade to a humanized label.
    """
    evidence_index = evidence_index or {}
    bullets: list[str] = []
    citations: list[str] = []
    seen: set[str] = set()
    for b in all_blocks:
        for ref in (b.get("evidence_refs") or []):
            if ref in seen:
                continue
            seen.add(ref)
            label = _format_citation(ref, evidence_index.get(ref))
            bullets.append(f"[{len(bullets)+1}] {label}")
            citations.append(ref)
        # Always cite the block itself.
        if b["block_id"] not in seen:
            seen.add(b["block_id"])
            citations.append(b["block_id"])
    if not bullets:
        bullets.append("Sources compiled from inputs to this deck.")
    return SlideContent(
        slide_id=slide.slide_id,
        layout="references",
        title=slide.title or "Sources",
        body_bullets=bullets[:20],
        citations=citations,
    )


# Audience code → presentable label for the title-slide subtitle.
_AUDIENCE_LABELS = {
    "ic": "Investment Committee",
    "retail": "Retail Investors",
    "internal": "Internal Review",
    "client": "Client Briefing",
    "board": "Board of Directors",
}


def _build_title_slide(slide: OutlineSlide, brief: dict) -> SlideContent:
    """Deterministic title slide — guarantees a subtitle (audience · date).

    The LLM frequently emits a bare title with no ``body_paragraphs``, leaving a
    blank cover. We compose the subtitle from brief context instead.
    """
    audience = str(brief.get("audience") or "").lower()
    aud_label = _AUDIENCE_LABELS.get(audience, audience.title() if audience else "")
    date_label = datetime.now().strftime("%B %Y")
    subtitle = " · ".join(p for p in (aud_label, date_label) if p)
    return SlideContent(
        slide_id=slide.slide_id,
        layout="title",
        title=slide.title or str(brief.get("title") or "Investment Case"),
        body_paragraphs=[subtitle] if subtitle else [],
    )


__all__ = ["per_slide_generate_node"]
