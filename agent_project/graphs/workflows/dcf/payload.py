"""LLM-facing payload summary + consistency checks + ref humanization."""

from __future__ import annotations

import json
from typing import Any

from .sources import (
    SourceRegistry,
    company_profile_section_lines,
    company_state_ref_ids,
    field_basis,
    humanize_evidence_refs,
    infer_evidence_item,
    inline_cite_text,
    market_reconciliation_section_lines,
    payload_evidence_items,
    recent_developments_section_lines,
    resolve_evidence_item,
    wacc_input_ref_ids,
)
from .state import _TIER_A_FIELDS
from .priors import forecast_confidence


def _humanize_evidence_refs(
    refs: list[str],
    evidence_items: list[dict[str, Any]],
) -> list[str]:
    """Map opaque evidence_ids to human-readable labels."""
    return humanize_evidence_refs(refs, evidence_items)


def _memo_proposals_by_field(memo: dict[str, Any]) -> dict[str, dict[str, Any]]:
    proposals = memo.get("proposals") if isinstance(memo, dict) else None
    if not proposals:
        return {}
    return {
        p["field"]: p
        for p in proposals
        if isinstance(p, dict) and p.get("field")
    }


def _provenance_ref_ids(prov: dict[str, Any]) -> list[str]:
    refs = list(prov.get("evidence_refs") or [])
    if refs:
        return refs
    reference = prov.get("reference")
    if reference:
        return [part.strip() for part in str(reference).split(",") if part.strip()]
    return []


def _confidence_label(score: float) -> str:
    return "high" if score >= 0.70 else "medium" if score >= 0.50 else "low"


def _shareholder_mechanics_section_lines(
    valuation: dict[str, Any],
    assumptions: dict[str, Any],
    horizon_years: int,
) -> list[str]:
    """Surface buyback/SBC/per-share mechanics from valuation + assumptions."""
    shares_initial = valuation.get("shares_initial")
    if shares_initial is None:
        shares_initial = assumptions.get("shares_outstanding")
    if not shares_initial:
        return []

    buyback_yield = float(
        valuation.get("buyback_yield")
        if valuation.get("buyback_yield") is not None
        else assumptions.get("buyback_yield", 0.0) or 0.0
    )
    horizon = max(int(horizon_years or 5), 1)
    shares_end = valuation.get("shares_end")
    if shares_end is None:
        shares_end = float(shares_initial) * ((1.0 - buyback_yield) ** horizon)
    else:
        shares_end = float(shares_end)
    shares_initial = float(shares_initial)

    sbc_pct = float(assumptions.get("sbc_pct_revenue", 0.0) or 0.0)
    fcff_margin = assumptions.get("fcff_margin")
    perpetual = valuation.get("perpetual_buyback_yield")
    effective_g = valuation.get("effective_terminal_growth")
    terminal_g = assumptions.get("terminal_growth")
    cap_source = str(valuation.get("perpetual_buyback_cap_source") or "")

    lines = ["## Shareholder Mechanics", ""]
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Initial shares outstanding | {shares_initial:,.0f}M |")
    lines.append(f"| Shares after {horizon}-year buybacks | {shares_end:,.0f}M |")
    if shares_initial > 0:
        reduction = (1.0 - shares_end / shares_initial) * 100.0
        lines.append(f"| Share count reduction (explicit horizon) | {reduction:.1f}% |")
    lines.append(f"| Annual buyback yield (assumption) | {buyback_yield:.2%} |")

    if sbc_pct > 0:
        lines.append(f"| SBC drag on FCFF margin | −{sbc_pct:.2%} of revenue |")
        if fcff_margin is not None:
            economic = float(fcff_margin) - sbc_pct
            lines.append(
                f"| Reported → economic FCFF margin | "
                f"{float(fcff_margin):.2%} → {economic:.2%} |"
            )

    if perpetual is not None:
        lines.append(f"| Perpetual buyback yield (terminal) | {float(perpetual):.2%} |")

    if effective_g is not None:
        if terminal_g is not None and perpetual is not None:
            lines.append(
                f"| Effective terminal growth (g + buyback) | "
                f"{float(terminal_g):.2%} + {float(perpetual):.2%} = {float(effective_g):.2%} |"
            )
        else:
            lines.append(f"| Effective terminal growth | {float(effective_g):.2%} |")

    cap_notes = {
        "fcff_yield_cap": "Perpetual buyback capped by terminal FCF yield.",
        "hard_cap_4pct": "Perpetual buyback capped at 4%.",
        "input": "Perpetual buyback equals the input buyback yield.",
        "no_buyback": "No buyback assumed — terminal uses revenue growth only.",
    }
    if cap_source in cap_notes:
        lines.append("")
        lines.append(f"> {cap_notes[cap_source]}")

    lines.append("")
    lines.append(
        "> Implied per-share price = equity value ÷ shares after explicit-horizon buybacks. "
        "Terminal value uses perpetual buyback compounding at the rate above."
    )
    lines.append("")
    return lines


def _run_consistency_checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Cross-validate the DCF output for internal consistency."""
    checks: list[dict[str, Any]] = []
    val = payload.get("valuation") or {}
    features = payload.get("features") or {}
    wacc_comp = payload.get("wacc_components") or {}
    memo = payload.get("assumption_memo") or {}

    pv = val.get("pv_cash_flows", 0) or 0
    tv_pv = val.get("terminal_pv", 0) or 0
    ev = val.get("enterprise_value", 0) or 0
    if abs((pv + tv_pv) - ev) > 0.01 * max(ev, 1):
        checks.append({"ok": False, "label": "EV reconciliation",
                        "detail": f"PV (${pv:,.0f}M) + TV_discounted (${tv_pv:,.0f}M) ≠ EV (${ev:,.0f}M)"})
    else:
        checks.append({"ok": True, "label": "EV reconciliation",
                        "detail": f"${pv:,.0f}M + ${tv_pv:,.0f}M = ${ev:,.0f}M"})

    memo_wacc = (payload.get("assumptions") or {}).get("wacc")
    capm_beta = features.get("beta")
    capm_erp = wacc_comp.get("equity_risk_premium", 0.055)
    capm_rf = wacc_comp.get("risk_free_rate", 0.045)
    if memo_wacc and capm_beta:
        implied_capm_wacc = capm_rf + float(capm_beta) * capm_erp
        wacc_method = wacc_comp.get("method", "?")
        if wacc_method == "capm" and abs(memo_wacc - implied_capm_wacc) > 0.02:
            checks.append({"ok": False, "label": "WACC vs CAPM",
                           "detail": f"WACC={memo_wacc:.2%} vs CAPM-implied Rf+β·ERP={implied_capm_wacc:.2%} (gap > 2%)"})
        else:
            checks.append({"ok": True, "label": "WACC vs CAPM",
                           "detail": f"WACC={memo_wacc:.2%} via {wacc_method}, β={capm_beta}"})

    tg = (payload.get("assumptions") or {}).get("terminal_growth")
    rf = wacc_comp.get("risk_free_rate", 0.045)
    if tg is not None and rf:
        if tg > rf + 0.005:
            checks.append({"ok": False, "label": "Terminal growth vs Rf",
                           "detail": f"TGR={tg:.2%} exceeds Rf={rf:.2%} by >50bps — implies company outgrows economy forever"})
        else:
            checks.append({"ok": True, "label": "Terminal growth vs Rf",
                           "detail": f"TGR={tg:.2%} ≤ Rf={rf:.2%}+buffer ✓"})

    proposals = memo.get("proposals") if isinstance(memo, dict) else None
    if proposals:
        total_refs = sum(len(p.get("evidence_refs", [])) for p in proposals)
        filing_refs = 0
        evidence_items = payload_evidence_items(payload)
        by_id = {item.get("evidence_id", ""): item for item in evidence_items}
        for p in proposals:
            for ref in p.get("evidence_refs", []):
                if by_id.get(ref, {}).get("source_tier") == "filing":
                    filing_refs += 1
        if total_refs == 0:
            checks.append({"ok": False, "label": "Evidence coverage",
                           "detail": "No evidence refs cited in memo proposals"})
        elif filing_refs == 0:
            checks.append({"ok": False, "label": "Evidence coverage",
                           "detail": f"{total_refs} refs but 0 from SEC filings — assumptions may lack grounding"})
        else:
            checks.append({"ok": True, "label": "Evidence coverage",
                           "detail": f"{total_refs} refs, {filing_refs} from SEC filings ✓"})

    return checks


SENSITIVITY_CHART_MARKER = "[SENSITIVITY_CHART]"


def _sensitivity_matrix_section_lines(payload: dict[str, Any]) -> list[str]:
    """WACC × terminal-growth matrix + inline chart marker (before assumptions)."""
    sensitivity = payload.get("sensitivity_table") or []
    if not sensitivity:
        return []
    prices = [
        float(r.get("implied_share_price", 0))
        for r in sensitivity
        if r.get("implied_share_price")
    ]
    if not prices:
        return []

    waccs = sorted({round(float(r["wacc"]), 6) for r in sensitivity})
    tgrs = sorted({round(float(r["terminal_growth"]), 6) for r in sensitivity})
    lookup = {
        (round(float(r["wacc"]), 6), round(float(r["terminal_growth"]), 6)): float(
            r["implied_share_price"],
        )
        for r in sensitivity
    }

    lines = ["## Sensitivity Matrix"]
    header = "| WACC \\ TGR | " + " | ".join(f"{t:.2%}" for t in tgrs) + " |"
    sep = "|" + "---|" * (len(tgrs) + 1)
    lines.append(header)
    lines.append(sep)
    for w in waccs:
        row = [f"{w:.2%}"]
        for t in tgrs:
            val = lookup.get((w, t))
            row.append(f"${val:.2f}" if val is not None else "—")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append(
        f"Range across WACC ±1% and terminal-growth ±0.5%: "
        f"${min(prices):.2f} – ${max(prices):.2f}."
    )
    lines.append("")
    lines.append(SENSITIVITY_CHART_MARKER)
    lines.append("")
    return lines


def _confidence_drivers_lines(payload: dict[str, Any]) -> list[str]:
    """Render decomposition of interpretive confidence so it isn't opaque."""
    breakdown = payload.get("confidence_breakdown") or {}
    components = breakdown.get("components") or {}

    component_labels = {
        "data_quality": ("Data quality", 20),
        "revenue_growth": ("Revenue growth grounding", 20),
        "margin_stability": ("Margin stability", 20),
        "wacc_reliability": ("WACC reliability", 25),
        "terminal_assumptions": ("Terminal assumptions", 15),
        "validity_penalty": ("Validity / divergence penalty", None),
    }
    rows: list[str] = []
    for key, (label, weight) in component_labels.items():
        comp = components.get(key)
        if not isinstance(comp, dict):
            continue
        score = comp.get("score")
        reason = str(comp.get("reason") or "").strip()
        if not isinstance(score, (int, float)):
            continue
        weight_str = f" (weight {weight}%)" if weight else ""
        score_pct = float(score) * 100
        sign = "+" if score >= 0.50 else "−"
        rows.append(f"  - {sign} **{label}**{weight_str}: {score_pct:.0f}% — {reason}")

    assessment = payload.get("confidence_assessment") or {}
    grounding = assessment.get("evidence_grounding") or {}
    if grounding:
        mult = grounding.get("multiplier", 1.0)
        label = str(grounding.get("label") or "").replace("_", " ")
        reason = grounding.get("reason") or ""
        sign = "−" if mult < 1.0 else "+"
        rows.append(
            f"  - {sign} **Evidence grounding**: {label} (×{mult:.2f}) — {reason}"
        )
        verdicts = assessment.get("verdict_counts") or {}
        if verdicts:
            verdict_str = ", ".join(f"{k}: {v}" for k, v in verdicts.items() if v)
            if verdict_str:
                rows.append(f"  - Reconciliation verdicts: {verdict_str}")

    if not rows:
        return []
    return ["**Interpretive confidence drivers:**", *rows, ""]


def _executive_summary_lines(payload: dict[str, Any]) -> list[str]:
    ticker = str(payload.get("ticker") or "?").upper()
    valuation = payload.get("valuation") or {}
    implied = valuation.get("implied_share_price")
    spot = valuation.get("current_price") or (payload.get("profile_meta") or {}).get("spot_price")
    confidence = str(payload.get("confidence_label") or "medium")
    confidence_assessment = payload.get("confidence_assessment") or {}
    model_validity = str(payload.get("model_validity") or "valid")
    reconciliation = str(payload.get("reconciliation_status") or "aligned")

    lines = ["## Executive Summary", ""]
    lines.append(f"- **Model validity:** {model_validity.upper()}")
    if reconciliation != "aligned":
        label = reconciliation.replace("_", " ")
        lines.append(f"- **Market reconciliation:** {label}")
        note = str(payload.get("reconciliation_note") or "").strip()
        if note:
            lines.append(f"- **Reconciliation note:** {note}")
    if confidence_assessment:
        proc = confidence_assessment.get("procedural_confidence")
        interp = confidence_assessment.get("interpretive_confidence")
        if isinstance(proc, (int, float)):
            lines.append(f"- **Procedural confidence:** {_confidence_label(float(proc)).upper()} ({float(proc):.0%})")
        if isinstance(interp, (int, float)):
            lines.append(f"- **Interpretive confidence:** {_confidence_label(float(interp)).upper()} ({float(interp):.0%})")
    else:
        lines.append(f"- **Confidence:** {confidence.upper()}")

    if model_validity != "invalid" and isinstance(implied, (int, float)) and implied:
        price_line = f"- **Model-implied share price:** ${implied:.2f}"
        if isinstance(spot, (int, float)) and spot > 0:
            pct = ((implied / spot) - 1) * 100
            price_line += (
                f" vs spot ${float(spot):.2f} "
                f"({pct:+.1f}% — informational, not a verdict)"
            )
        lines.append(price_line)

    scenario_results = payload.get("scenario_results") or []
    prices = [
        r["valuation"].get("implied_share_price", 0)
        for r in scenario_results
        if r.get("valuation", {}).get("implied_share_price")
    ]
    if len(prices) >= 2:
        lines.append(
            f"- **Scenario range:** ${min(prices):.2f} – ${max(prices):.2f} "
            f"(bear / base / bull)"
        )
    lines.append("")
    drivers = _confidence_drivers_lines(payload)
    if drivers:
        lines.extend(drivers)
    return lines


def _assistant_instruction_lines(payload: dict[str, Any]) -> list[str]:
    model_validity = str(payload.get("model_validity") or "valid")
    reconciliation = str(payload.get("reconciliation_status") or "aligned")

    if model_validity == "invalid":
        return [
            "> ⚠ **MODEL MARKED INVALID — DEGRADED OUTPUT**",
            f"> Reason: {payload.get('invalidation_reason') or 'solver or critical input failure'}",
            "> **Instructions to assistant (MANDATORY):** "
            "(1) Do NOT cite a specific implied price as a target. "
            "(2) Do NOT compute gap-vs-spot as a buy/sell verdict. "
            "(3) Do NOT say overvalued, undervalued, fairly priced, or any synonym. "
            "(4) Lead with WHAT broke (solver failure, missing inputs) — not market gaps. "
            "(5) Use only numbered [n] citations from ## References — never invent sources. "
            "(6) Recommend what to fix before re-running.",
            "",
        ]

    if reconciliation == "structural_gap":
        return [
            "> **How to present this report (MANDATORY):** "
            "(1) The DCF model is **VALID** — do NOT call it invalid, unreliable, or lacking foundation. "
            "(2) Market reconciliation gaps show what the **price implies**, not proof the model is wrong. "
            "(3) Do NOT say overvalued, undervalued, fairly priced, overvaluation, or undervaluation. "
            "(4) Present the scenario range and reconciliation table using [n] refs from this report. "
            "(5) Never cite vague sources like 'internal financial analysis' — only [n] from ## References. "
            "(6) When implied growth/margin far exceeds the model, explain that spot embeds higher expectations — expected when model price ≠ spot.",
            "",
        ]

    return [
        "> **Instructions to assistant:** Present assumptions with [n] citations. "
        "Do not invent sources. Avoid buy/sell verdicts from gap-vs-spot alone.",
        "",
    ]


def extract_dcf_report_from_tool_pointer(content: str) -> str | None:
    """Return the full DCF markdown report from a run_dcf_workflow tool pointer."""
    try:
        pointer = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(pointer, dict):
        return None
    if pointer.get("tool_name") != "run_dcf_workflow":
        return None
    if pointer.get("dcf_hitl"):
        return None

    summary = str(pointer.get("summary") or "")
    if summary.startswith("# DCF Valuation:"):
        return summary

    tool_result_id = pointer.get("tool_result_id")
    if not tool_result_id:
        return None
    from utils import get_run_dir  # noqa: PLC0415

    file_path = get_run_dir() / "tool_results" / f"{tool_result_id}.json"
    if not file_path.exists():
        return None
    try:
        stored = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    report = str(stored.get("summary") or "")
    if report.startswith("# DCF Valuation:"):
        return report
    return None


def extract_dcf_payload_from_tool_pointer(content: str) -> dict[str, Any] | None:
    """Return the full dcf_output JSON dict from a run_dcf_workflow tool pointer."""
    try:
        pointer = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(pointer, dict):
        return None
    if pointer.get("tool_name") != "run_dcf_workflow" or pointer.get("dcf_hitl"):
        return None

    tool_result_id = pointer.get("tool_result_id")
    if not tool_result_id:
        return _load_persisted_dcf_payload()

    from utils import get_run_dir  # noqa: PLC0415

    file_path = get_run_dir() / "tool_results" / f"{tool_result_id}.json"
    if not file_path.exists():
        return _load_persisted_dcf_payload()
    try:
        stored = json.loads(file_path.read_text(encoding="utf-8"))
        payload = json.loads(stored.get("result") or "{}")
    except (json.JSONDecodeError, OSError, TypeError):
        return _load_persisted_dcf_payload()
    return payload if isinstance(payload, dict) and payload.get("ticker") else _load_persisted_dcf_payload()


def dcf_source_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Evidence payload needed by the report citation drawer."""
    registry = SourceRegistry.from_payload(payload)
    citation_map = registry.citation_map()
    evidence_items = payload_evidence_items(payload)
    by_id = {str(item.get("evidence_id") or ""): item for item in evidence_items}
    ticker = str(payload.get("ticker") or "")

    resolved: list[dict[str, Any]] = []
    resolved_ids: set[str] = set()
    for evidence_id in citation_map.values():
        if not evidence_id or evidence_id in resolved_ids:
            continue
        item = resolve_evidence_item(evidence_id, by_id, all_items=evidence_items)
        if item is None:
            item = infer_evidence_item(evidence_id, ticker=ticker)
        resolved.append(item)
        resolved_ids.add(evidence_id)

    return {
        "citation_map": citation_map,
        "evidence_items": resolved,
    }


def _load_persisted_dcf_payload() -> dict[str, Any] | None:
    """Read the finalized DCF JSON artifact when tool-result metadata is sparse."""
    from utils import get_run_dir  # noqa: PLC0415

    path = get_run_dir() / "dcf_output.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def extract_dcf_source_metadata_from_tool_pointer(content: str) -> dict[str, Any] | None:
    """Return citation metadata from a run_dcf_workflow tool pointer."""
    try:
        pointer = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(pointer, dict):
        return None
    if pointer.get("tool_name") != "run_dcf_workflow" or pointer.get("dcf_hitl"):
        return None

    tool_result_id = pointer.get("tool_result_id")
    if not tool_result_id:
        return None
    from utils import get_run_dir  # noqa: PLC0415

    file_path = get_run_dir() / "tool_results" / f"{tool_result_id}.json"
    if not file_path.exists():
        return None
    try:
        stored = json.loads(file_path.read_text(encoding="utf-8"))
        payload = json.loads(stored.get("result") or "{}")
    except (json.JSONDecodeError, OSError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    if not payload_evidence_items(payload):
        persisted = _load_persisted_dcf_payload()
        if persisted:
            payload = dict(payload)
            if persisted.get("_evidence_items"):
                payload["_evidence_items"] = persisted["_evidence_items"]
            if persisted.get("evidence_pack"):
                payload["evidence_pack"] = persisted["evidence_pack"]

    return dcf_source_metadata(payload)


def summarize_dcf_payload(payload: dict[str, Any], *, for_display: bool = True) -> str:
    """Rich DCF report markdown.

    When ``for_display=True`` (default), omits LLM-only instruction blocks.
    When ``for_display=False``, includes assistant guidance for tool pointers.
    """
    if not isinstance(payload, dict):
        return "DCF workflow finished without payload."

    lines: list[str] = []

    ticker = str(payload.get("ticker") or "?").upper()
    valuation = payload.get("valuation") or {}
    implied = valuation.get("implied_share_price")
    spot = valuation.get("current_price") or 0.0
    confidence = str(payload.get("confidence_label") or "medium")
    horizon = payload.get("horizon_years", 5)

    lines.append(f"# DCF Valuation: {ticker}")
    lines.append(f"Horizon: {horizon} years")

    evidence_items = payload_evidence_items(payload)
    source_registry = SourceRegistry.from_payload(payload)

    profile_lines = company_profile_section_lines(payload, source_registry)
    if profile_lines:
        lines.extend(profile_lines)

    news_lines = recent_developments_section_lines(payload, source_registry)
    if news_lines:
        lines.extend(news_lines)

    lines.extend(_executive_summary_lines(payload))
    if not for_display:
        lines.extend(_assistant_instruction_lines(payload))

    model_validity = str(payload.get("model_validity") or "valid")
    is_invalid = model_validity == "invalid"
    has_executive_summary = True
    scenario_results = payload.get("scenario_results") or []
    if scenario_results:
        prices = [r["valuation"].get("implied_share_price", 0) for r in scenario_results if r.get("valuation", {}).get("implied_share_price")]
        if is_invalid:
            lines.append("## Scenario Range (illustrative only — model invalid)")
            if prices:
                lines.append(
                    f"Spread across scenarios: ${min(prices):.2f} – ${max(prices):.2f} "
                    f"(width ${max(prices) - min(prices):.2f}). "
                    f"Per-scenario point estimates withheld because the underlying model failed validation."
                )
            lines.append("")
        else:
            lines.append("## Scenario Valuation")
            lines.append("| Scenario | Probability | Implied Price |")
            lines.append("|----------|-------------|---------------|")
            for r in scenario_results:
                name = r.get("name", "?").title()
                prob = r.get("probability", 0)
                price = r.get("valuation", {}).get("implied_share_price", 0)
                lines.append(f"| {name} | {prob:.0%} | ${price:.2f} |")
            if prices:
                expected = sum(r.get("probability", 0) * r.get("valuation", {}).get("implied_share_price", 0) for r in scenario_results)
                lines.append(f"\n**Expected value:** ${expected:.2f}")
                lines.append(f"**Range:** ${min(prices):.2f} – ${max(prices):.2f}")
            lines.append("")
    elif not has_executive_summary or not for_display:
        if is_invalid:
            lines.append("Point estimate withheld — model invalid.")
            lines.append(f"Confidence: **{confidence.upper()}** (model invalid)")
            lines.append("")
        elif not for_display:
            if isinstance(implied, (int, float)) and implied:
                gap = ""
                if isinstance(spot, (int, float)) and spot > 0:
                    pct = ((implied / spot) - 1) * 100
                    gap = f" (vs spot ${spot:.2f}, {pct:+.1f}%)"
                lines.append(f"Implied share price: ${implied:.2f}{gap}")
            lines.append(f"Confidence: **{confidence.upper()}**")
            lines.append("")

    sens_lines = _sensitivity_matrix_section_lines(payload)
    if sens_lines:
        lines.extend(sens_lines)

    thesis = payload.get("thesis") or {}
    if thesis:
        lines.append("## Investment Thesis")
        bull = thesis.get("bull_thesis", "")
        bear = thesis.get("bear_thesis", "")
        narrative = thesis.get("narrative", "")
        drivers = thesis.get("key_drivers") or []
        if bull:
            lines.append(f"**Bull case:** {bull}")
        if bear:
            lines.append(f"**Bear case:** {bear}")
        if drivers:
            lines.append("**Key drivers:**")
            for d in drivers:
                direction = d.get("direction", "?")
                conviction = d.get("conviction", "medium")
                lines.append(f"  - {d.get('driver', '?')} ({direction}, {conviction} conviction)")
        if narrative:
            lines.append(f"**Narrative:** {narrative}")
        lines.append("")

    critique = payload.get("critique") or {}
    divergences = payload.get("divergences") or []
    analysis_positions = payload.get("analysis_positions") or []

    if critique or divergences or analysis_positions:
        flags = critique.get("flags") or []
        iteration = critique.get("iteration", 0)
        stop_reason = str(critique.get("stop_reason") or "").strip()
        changes = critique.get("changes") or []
        findings = critique.get("findings") or []
        should_refine = critique.get("should_refine", False)
        review_summary = str(critique.get("review_summary") or "").strip()

        lines.append("## Analysis Journey")
        if flags:
            lines.append(f"Quality signals after valuation (iteration {iteration}):")
            for f in flags:
                sev = f.get("severity", "?").upper()
                signal = f.get("signal", "?").replace("_", " ")
                val = f.get("value", f.get("value_bps", f.get("value_pct", "?")))
                unit = "bps" if "value_bps" in f else ("%" if "value_pct" in f else "")
                lines.append(f"  [{sev}] {signal}: {val}{unit}")
        if findings:
            high = sum(1 for f in findings if str(f.get("severity", "")).lower() == "high")
            lines.append(f"Adversarial review: {len(findings)} finding(s) ({high} high-severity).")
        if changes:
            lines.append("**Assumption changes from review:**")
            for change in changes[:12]:
                lines.append(f"  - {change}")
        if analysis_positions:
            explained = sum(1 for p in analysis_positions if p.get("position") == "EXPLAINED")
            unresolved = len(analysis_positions) - explained
            lines.append(
                f"Market reconciliation: {explained} explained, {unresolved} unresolved divergence(s)."
            )
        elif divergences:
            lines.append(f"Market reconciliation: {len(divergences)} divergence(s) detected pre-analysis.")
        interpretation = critique.get("interpretation", "")
        if interpretation:
            lines.append(f"**Interpretation:** {interpretation}")
        if review_summary and review_summary != stop_reason:
            lines.append(f"**Review note:** {review_summary[:300]}")
        if stop_reason:
            lines.append(f"**Final:** {stop_reason}")
        elif should_refine and changes:
            lines.append("**Status:** Assumptions updated and valuation re-run with revised inputs.")
        elif not flags and not changes and not findings and not divergences and not analysis_positions:
            lines.append("No issues found — valuation accepted as-is.")
        lines.append("")

    assumptions = payload.get("assumptions") or {}
    provenance = payload.get("assumption_provenance") or {}
    memo = payload.get("assumption_memo") or {}
    memo_by_field = _memo_proposals_by_field(memo if isinstance(memo, dict) else {})

    if assumptions:
        lines.append("## Assumptions")
        lines.append("| Field | Value | Basis | Refs |")
        lines.append("|-------|-------|-------|------|")
        for field in [
            "base_revenue", "revenue_growth", "fcff_margin",
            "revenue_growth_terminal", "fcff_margin_terminal",
            "terminal_growth", "tax_rate", "buyback_yield",
            "sbc_pct_revenue", "wacc",
            "shares_outstanding", "net_debt",
        ]:
            val = assumptions.get(field)
            if val is None:
                continue
            prov = provenance.get(field, {}) if isinstance(provenance, dict) else {}
            if not isinstance(prov, dict):
                prov = {}
            basis = field_basis(field, prov, memo_by_field.get(field))
            ref_ids = _provenance_ref_ids(prov)
            refs = source_registry.format_refs(ref_ids)
            if field == "shares_outstanding":
                val_str = f"{val:,.0f}M"
            elif field in _TIER_A_FIELDS:
                val_str = f"${val:,.0f}M"
            else:
                val_str = f"{val:.2%}"
            lines.append(f"| {field} | {val_str} | {basis} | {refs} |")
        lines.append("")

    wacc_comp = payload.get("wacc_components") or {}
    features = payload.get("features") or {}
    if wacc_comp:
        wacc_refs = source_registry.format_refs(
            wacc_input_ref_ids(wacc_comp, features, evidence_items),
        )
        lines.append("## WACC Decomposition")
        lines.append(f"Method: {wacc_comp.get('method', 'unknown')} {wacc_refs}".rstrip())
        for key in (
            "risk_free_rate", "equity_risk_premium", "beta",
            "cost_of_equity", "pre_tax_cost_of_debt",
            "after_tax_cost_of_debt", "equity_weight", "debt_weight",
            "marginal_tax_rate",
        ):
            v = wacc_comp.get(key)
            if v is not None:
                if isinstance(v, float) and key not in ("beta",):
                    lines.append(f"  {key}: {v:.2%}")
                else:
                    lines.append(f"  {key}: {v}")
        wacc_basis = (provenance.get("wacc") or {}).get("evidence") if isinstance(provenance, dict) else None
        if wacc_basis:
            lines.append(f"  Basis: {inline_cite_text(str(wacc_basis), source_registry)}")

        stack = wacc_comp.get("wacc_stack") or {}
        stack_components = stack.get("components") or []
        if stack_components:
            band = stack.get("profile_band") or {}
            band_str = ""
            if band:
                band_str = (
                    f" — profile band {band.get('soft_min', 0):.0%}–"
                    f"{band.get('soft_max', 0):.0%}"
                )
            lines.append("")
            lines.append(f"**WACC stack{band_str}:**")
            for comp in stack_components:
                label = comp.get("label", "?")
                value = comp.get("value")
                delta = comp.get("delta")
                if value is not None and delta in (None, 0.0) and label == "Base CAPM":
                    lines.append(f"  - {label}: {value:.2%}")
                elif delta is not None and delta != 0.0:
                    lines.append(f"  - {label}: {delta:+.2%}")
                elif value is not None:
                    lines.append(f"  - {label}: {value:.2%}")
            valuation_wacc = float((assumptions or {}).get("wacc") or 0.0)
            stack_final = stack.get("final_wacc")
            if valuation_wacc > 0:
                lines.append(f"  - **WACC used in valuation: {valuation_wacc:.2%}**")
                if stack_final is not None and abs(float(stack_final) - valuation_wacc) > 1e-6:
                    lines.append(
                        f"  - ⚠ Stack audit trail ends at {float(stack_final):.2%} "
                        f"— differs from valuation WACC"
                    )
            elif stack_final is not None:
                lines.append(f"  - **Final WACC: {float(stack_final):.2%}**")

            # Issue #1: when CAPM was clipped to the profile band, say WHY —
            # otherwise the user can't tell whether the discount rate is market-
            # derived or a heuristic override.
            base_capm = next(
                (float(c.get("value")) for c in stack_components
                 if c.get("label") == "Base CAPM" and c.get("value") is not None),
                None,
            )
            if base_capm is not None and valuation_wacc > 0 and abs(base_capm - valuation_wacc) > 1e-4:
                beta_val = wacc_comp.get("beta")
                beta_str = f" Raw beta ({float(beta_val):.2f})" if isinstance(beta_val, (int, float)) else " Raw beta"
                direction = "down" if base_capm > valuation_wacc else "up"
                edge = "ceiling" if direction == "down" else "floor"
                lines.append("")
                lines.append(
                    f"  **Why adjusted:** CAPM WACC {base_capm:.2%} was clipped "
                    f"{direction} to the profile band {edge} {valuation_wacc:.2%}."
                )
                lines.append(
                    f"  {beta_str} likely {'overstates' if direction == 'down' else 'understates'} "
                    f"the fundamental business risk of a stable mega-cap; the band "
                    f"normalizes to sector-typical discount rates rather than a "
                    f"point beta estimate."
                )
        lines.append("")

        coherence = payload.get("coherence_assessment") or {}
        coherence_adj = payload.get("coherence_adjustments") or {}
        if coherence or coherence_adj:
            lines.append("## Assumption Coherence")
            ops_tier = coherence.get("ops_tier", "neutral")
            wacc_tier = coherence.get("wacc_tier", "unknown")
            status = coherence.get("status", "ok")
            badge = "✓" if status == "ok" else "⚠"
            lines.append(
                f"{badge} **Status:** {status} — operating bundle: `{ops_tier}`, "
                f"WACC tier: `{wacc_tier}`"
            )
            rationale = coherence.get("ops_rationale") or []
            if rationale:
                lines.append(f"> Ops signals: {'; '.join(rationale)}")
            for flag in coherence.get("flags", []) or []:
                msg = flag.get("message") or flag.get("code", "")
                if msg:
                    lines.append(f"> {msg}")
            if coherence_adj:
                lines.append("")
                lines.append("**Auto-corrections applied:**")
                for field, change in coherence_adj.items():
                    if not isinstance(change, dict):
                        continue
                    old = float(change.get("old", 0.0))
                    new = float(change.get("new", 0.0))
                    delta = float(change.get("delta", 0.0))
                    reason = str(change.get("reason", ""))
                    lines.append(
                        f"  - `{field}`: {old:.2%} → {new:.2%} ({delta:+.2%}) — {reason}"
                    )
            lines.append("")

    recon_lines = market_reconciliation_section_lines(payload, source_registry)
    if recon_lines:
        lines.extend(recon_lines)

    proposals = memo.get("proposals") if isinstance(memo, dict) else None
    if proposals:
        lines.append("## Assumption Rationale")
        lines.append(
            "_Evidence = how well-supported the value is. "
            "Forecast = how reliably the future value can be predicted "
            "(intrinsic uncertainty, independent of evidence)._"
        )
        lines.append("")
        for prop in proposals:
            field = prop.get("field", "?")
            rationale = prop.get("rationale", "")
            conf = prop.get("confidence", 0.5)  # evidence confidence
            fc = forecast_confidence(field)
            refs = prop.get("evidence_refs", [])
            ref_markers = source_registry.format_refs(list(refs))
            lines.append(
                f"**{field}** (evidence: {conf:.0%} · forecast: {fc:.0%}) {ref_markers}"
            )
            lines.append(f"  {rationale}")
            lines.append("")
        overall = memo.get("overall_narrative", "")
        if overall:
            lines.append(f"**Narrative:** {overall}")
            lines.append("")
        uncertainties = memo.get("key_uncertainties", [])
        if uncertainties:
            lines.append("**Key uncertainties:**")
            for u in uncertainties:
                lines.append(f"  - {u}")
            lines.append("")

    company_state = payload.get("company_state") or {}
    if company_state:
        context_refs = source_registry.format_refs(company_state_ref_ids(company_state))
        lines.append("## Company Context")
        if context_refs != "—":
            lines.append(f"**Sources:** {context_refs}")
        lines.append("")
        overview_items = [
            ("business_summary", "Business summary"),
            ("growth_outlook", "Growth outlook"),
            ("competitive_position", "Competitive position"),
            ("macro_context", "Macro context"),
        ]
        for key, label in overview_items:
            val = company_state.get(key)
            if val:
                lines.append(f"- **{label}:** {inline_cite_text(str(val), source_registry)}")

        margin_trend = company_state.get("margin_trend")
        margin_narrative = company_state.get("margin_narrative")
        if margin_trend or margin_narrative:
            margin_parts = []
            if margin_trend:
                margin_parts.append(f"trend: `{margin_trend}`")
            if margin_narrative:
                margin_parts.append(inline_cite_text(str(margin_narrative), source_registry))
            lines.append(f"- **Margins:** {' — '.join(margin_parts)}")

        if any(company_state.get(key) for key, _label in overview_items) or margin_trend or margin_narrative:
            lines.append("")

        risks = company_state.get("key_risks", [])
        if risks:
            lines.append("### Key Risks")
            for r in risks[:5]:
                lines.append(f"- {inline_cite_text(str(r), source_registry)}")
            lines.append("")
        drivers = company_state.get("growth_drivers", [])
        if drivers:
            lines.append("### Growth Drivers")
            for d in drivers[:5]:
                lines.append(f"- {inline_cite_text(str(d), source_registry)}")
            lines.append("")
        conflicts = company_state.get("conflicts", [])
        if conflicts:
            lines.append("### Source Conflicts")
            for c in conflicts:
                lines.append(f"- {inline_cite_text(str(c), source_registry)}")
        lines.append("")

    flags = (
        list(payload.get("assumption_flags") or [])
        + list(payload.get("valuation_flags") or [])
    )
    if flags:
        lines.append("## Quality Flags")
        for flag in flags:
            sev = flag.get("severity", "?").upper()
            msg = flag.get("message", "")
            lines.append(f"- **{sev}:** {msg}")
        lines.append("")

    # Issue #8: explicit per-year forecast model. The engine already computed
    # this; surfacing it lets the user audit the FCFF build (a tiny/negative
    # FCFF row is the visible cause of an implausibly-low implied price).
    projected = payload.get("projected_fcff") or []
    if projected:
        lines.append("## Forecast Model")
        lines.append("")
        lines.append("| Year | Revenue ($B) | Growth | FCFF margin | Econ. margin (− SBC) | FCFF ($B) |")
        lines.append("|------|-------------|--------|-------------|----------------------|-----------|")
        for row in projected:
            yr = int(row.get("year", 0))
            rev_b = float(row.get("revenue", 0.0)) / 1000.0
            fcff_b = float(row.get("fcff", 0.0)) / 1000.0
            growth = float(row.get("growth", 0.0))
            margin = float(row.get("margin", 0.0))
            eff = float(row.get("effective_margin", margin))
            lines.append(
                f"| Y{yr} | ${rev_b:,.1f} | {growth*100:.1f}% | "
                f"{margin*100:.1f}% | {eff*100:.1f}% | ${fcff_b:,.1f} |"
            )
        lines.append("")
        # Revenue bridge — base → terminal year, with CAGR.
        base_rev_b = float((assumptions or {}).get("base_revenue", 0.0)) / 1000.0
        last = projected[-1]
        last_rev_b = float(last.get("revenue", 0.0)) / 1000.0
        n_yrs = int(last.get("year", len(projected))) or len(projected)
        if base_rev_b > 0 and last_rev_b > 0 and n_yrs > 0:
            cagr = (last_rev_b / base_rev_b) ** (1.0 / n_yrs) - 1.0
            lines.append(
                f"- **Revenue bridge:** ${base_rev_b:,.1f}B base → "
                f"${last_rev_b:,.1f}B by Y{n_yrs} ({cagr*100:.1f}% CAGR)."
            )
        # Value bridge — explicit PV + terminal PV → EV → equity → price.
        if valuation:
            pv = float(valuation.get("pv_cash_flows", 0.0)) / 1000.0
            tvpv = float(valuation.get("terminal_pv", 0.0)) / 1000.0
            evb = float(valuation.get("enterprise_value", 0.0)) / 1000.0
            eqb = float(valuation.get("equity_value", 0.0)) / 1000.0
            price = float(valuation.get("implied_share_price", 0.0))
            tv_share = (tvpv / evb * 100.0) if evb else 0.0
            lines.append(
                f"- **Value bridge:** explicit PV ${pv:,.1f}B + terminal PV "
                f"${tvpv:,.1f}B = EV ${evb:,.1f}B → equity ${eqb:,.1f}B → "
                f"**${price:,.2f}/share** (terminal = {tv_share:.0f}% of EV)."
            )
        lines.append("")

    if valuation:
        net_debt_refs = source_registry.format_refs(
            _provenance_ref_ids((provenance.get("net_debt") or {}) if isinstance(provenance, dict) else {}),
        )
        lines.append(f"## Valuation Detail {net_debt_refs}".rstrip() if net_debt_refs != "—" else "## Valuation Detail")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        for key, label in [
            ("pv_cash_flows", "PV of explicit cash flows"),
            ("terminal_value", "Terminal value (undiscounted)"),
            ("terminal_pv", "Terminal value (discounted to present)"),
            ("enterprise_value", "Enterprise value (= PV + discounted TV)"),
            ("equity_value", "Equity value (= EV − net debt)"),
        ]:
            v = valuation.get(key)
            if isinstance(v, (int, float)):
                lines.append(f"| {label} | ${v:,.0f}M |")
        pv = valuation.get("pv_cash_flows", 0)
        tv_pv = valuation.get("terminal_pv", 0)
        ev = valuation.get("enterprise_value", 0)
        implied_ev = pv + tv_pv
        lines.append("")
        if abs(ev - implied_ev) > 1:
            lines.append(
                f"- ⚠ **EV reconciliation:** PV (${pv:,.0f}M) + discounted TV (${tv_pv:,.0f}M) "
                f"= ${implied_ev:,.0f}M ≠ stated EV (${ev:,.0f}M)"
            )
        else:
            lines.append(f"- ✓ **EV reconciliation:** ${pv:,.0f}M + ${tv_pv:,.0f}M = ${ev:,.0f}M {net_debt_refs}".rstrip())
        lines.append("")

        shareholder_lines = _shareholder_mechanics_section_lines(
            valuation, assumptions, int(payload.get("horizon_years") or horizon),
        )
        if shareholder_lines:
            lines.extend(shareholder_lines)

    checks = _run_consistency_checks(payload)
    if checks:
        lines.append("## Consistency Checks")
        for check in checks:
            status = "✓" if check["ok"] else "⚠"
            lines.append(f"- {status} **{check['label']}:** {check['detail']}")
        lines.append("")

    ref_lines = source_registry.references_section_lines()
    if ref_lines:
        lines.extend(ref_lines)

    return "\n".join(lines)
