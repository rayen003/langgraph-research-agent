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
    inline_cite_text,
    market_reconciliation_section_lines,
    recent_developments_section_lines,
    wacc_input_ref_ids,
)
from .state import _TIER_A_FIELDS


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
        evidence_items = payload.get("_evidence_items") or []
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


def _executive_summary_lines(payload: dict[str, Any]) -> list[str]:
    ticker = str(payload.get("ticker") or "?").upper()
    valuation = payload.get("valuation") or {}
    implied = valuation.get("implied_share_price")
    spot = valuation.get("current_price") or (payload.get("profile_meta") or {}).get("spot_price")
    confidence = str(payload.get("confidence_label") or "medium")
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

    evidence_items = payload.get("_evidence_items") or []
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
    if critique:
        flags = critique.get("flags") or []
        iteration = critique.get("iteration", 0)
        stop_reason = critique.get("stop_reason", "")

        lines.append("## Analysis Journey")
        if flags:
            lines.append(f"Self-critique after valuation (iteration {iteration}):")
            for f in flags:
                sev = f.get("severity", "?").upper()
                signal = f.get("signal", "?").replace("_", " ")
                val = f.get("value", f.get("value_bps", f.get("value_pct", "?")))
                unit = "bps" if "value_bps" in f else ("%" if "value_pct" in f else "")
                lines.append(f"  [{sev}] {signal}: {val}{unit}")
            interpretation = critique.get("interpretation", "")
            if interpretation:
                lines.append(f"**Interpretation:** {interpretation}")
            adjustments = critique.get("suggested_adjustments") or {}
            if adjustments:
                lines.append("**Adjustments applied:**")
                for field, delta in adjustments.items():
                    lines.append(f"  - {field}: {'+' if delta >= 0 else ''}{delta:.4f}")
        if stop_reason:
            lines.append(f"**Final:** {stop_reason}")
        if not flags or not stop_reason:
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
            "terminal_growth", "tax_rate", "wacc",
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
            if field in _TIER_A_FIELDS:
                val_str = f"${val:,.0f}M"
            elif field == "shares_outstanding":
                val_str = f"{val:,.0f}M"
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
        lines.append("")

    recon_lines = market_reconciliation_section_lines(payload, source_registry)
    if recon_lines:
        lines.extend(recon_lines)

    proposals = memo.get("proposals") if isinstance(memo, dict) else None
    if proposals:
        lines.append("## Assumption Rationale")
        for prop in proposals:
            field = prop.get("field", "?")
            rationale = prop.get("rationale", "")
            conf = prop.get("confidence", 0.5)
            refs = prop.get("evidence_refs", [])
            ref_markers = source_registry.format_refs(list(refs))
            lines.append(f"**{field}** (confidence: {conf:.0%}) {ref_markers}")
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
        lines.append(f"## Company Context {context_refs}".rstrip())
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
