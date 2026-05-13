"""Deterministic valuation nodes — kept stable and unchanged.

These are the "calculator" engine: once assumptions are locked, FCFF
projections, PV, terminal value, equity bridge, and sensitivities follow
by definition. No judgment happens here.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from utils import get_run_dir

from .activity import emit_step, emit_workflow_terminal
from .priors import check_valuation_sanity, compute_confidence_breakdown, compute_confidence_label

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Market data snapshot
# ---------------------------------------------------------------------------


def collect_market_data_node(state: dict) -> dict:
    """Fetch current spot price for implied-vs-market comparison."""
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("collect_market_data", "start", parent_step_id)
    ticker = state["ticker"]
    snapshot = {"price": 100.0, "source": "fallback"}
    try:
        import yfinance as yf  # type: ignore[import-untyped]

        history = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
        if not history.empty:
            snapshot = {
                "price": float(history["Close"].iloc[-1]),
                "source": "yfinance",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("DCF market data fallback ticker=%s error=%s", ticker, exc)

    logger.info(
        "DCF collect_market_data ticker=%s snapshot=%s",
        ticker, json.dumps(snapshot),
    )
    emit_step(
        "collect_market_data", "complete", parent_step_id,
        {"market_snapshot": snapshot, "summary_line": f"spot=${snapshot['price']:.2f} ({snapshot['source']})"},
    )
    return {"market_snapshot": snapshot}


# ---------------------------------------------------------------------------
# Cash flow projections
# ---------------------------------------------------------------------------


def project_cashflows_node(state: dict) -> dict:
    """Project FCFF over the forecast horizon from locked assumptions."""
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("project_cashflows", "start", parent_step_id)
    a = state["assumptions"]
    revenue = float(a["base_revenue"])
    growth = float(a["revenue_growth"])
    margin = float(a["fcff_margin"])
    projected: list[dict[str, float]] = []
    for year in range(1, int(state["horizon_years"]) + 1):
        revenue *= 1.0 + growth
        fcff = revenue * margin
        projected.append({
            "year": float(year),
            "revenue": revenue,
            "fcff": fcff,
        })

    emit_step(
        "project_cashflows", "complete", parent_step_id,
        {
            "rows": len(projected),
            "projections": [
                {
                    "year": int(row["year"]),
                    # base_revenue is in millions (FMP /1M), so divide by 1000 for billions
                    "revenue_B": round(row["revenue"] / 1000.0, 2),
                    "fcff_B": round(row["fcff"] / 1000.0, 2),
                }
                for row in projected
            ],
            "summary_line": f"{len(projected)}yr FCFF projected",
        },
    )
    logger.info("DCF project_cashflows rows=%d", len(projected))
    return {"projected_fcff": projected}


# ---------------------------------------------------------------------------
# Valuation computation
# ---------------------------------------------------------------------------


def compute_valuation_node(state: dict) -> dict:
    """Compute PV, terminal value, enterprise value, equity value, implied price."""
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("compute_valuation", "start", parent_step_id)
    a = state["assumptions"]
    wacc = float(a["wacc"])
    terminal_growth = float(a["terminal_growth"])
    projected = state["projected_fcff"]

    pv_sum = 0.0
    for row in projected:
        year = int(row["year"])
        pv_sum += float(row["fcff"]) / ((1.0 + wacc) ** year)

    terminal_fcf = float(projected[-1]["fcff"]) * (1.0 + terminal_growth)
    terminal_value = terminal_fcf / max((wacc - terminal_growth), 1e-6)
    terminal_pv = terminal_value / ((1.0 + wacc) ** int(projected[-1]["year"]))
    enterprise_value = pv_sum + terminal_pv
    equity_value = enterprise_value - float(a["net_debt"])
    implied_share_price = equity_value / max(float(a["shares_outstanding"]), 1e-6)

    valuation = {
        "pv_cash_flows": pv_sum,
        "terminal_value": terminal_value,           # undiscounted
        "terminal_pv": terminal_pv,                  # discounted — used in EV
        "enterprise_value": enterprise_value,         # = pv_sum + terminal_pv
        "equity_value": equity_value,                 # = EV - net_debt
        "implied_share_price": implied_share_price,   # = equity / shares
        "current_price": float(state["market_snapshot"].get("price", 0.0)),
    }

    profile = state.get("profile") or "default"
    valuation_flags = check_valuation_sanity(
        valuation=valuation,
        profile=profile,
        market_snapshot=state.get("market_snapshot") or {},
    )
    if valuation_flags:
        for flag in valuation_flags:
            logger.warning(
                "DCF valuation flag severity=%s field=%s value=%s expected=%s",
                flag.get("severity"), flag.get("field"),
                flag.get("value"), flag.get("expected"),
            )

    confidence_breakdown = compute_confidence_breakdown(
        assumption_flags=state.get("assumption_flags") or [],
        valuation_flags=valuation_flags,
        provenance=state.get("assumption_provenance") or {},
        assumption_memo=state.get("assumption_memo"),
    )
    confidence_label = confidence_breakdown["label"]

    logger.info(
        "DCF compute_valuation valuation=%s confidence=%s",
        json.dumps(valuation, ensure_ascii=False),
        confidence_label,
    )
    # Scale millions → raw dollars for frontend display (per-share already in $)
    valuation_display = {
        "pv_cash_flows": pv_sum * 1e6,
        "terminal_value": terminal_value * 1e6,
        "terminal_pv": terminal_pv * 1e6,
        "enterprise_value": enterprise_value * 1e6,
        "equity_value": equity_value * 1e6,
        "implied_share_price": implied_share_price,
        "current_price": valuation["current_price"],
    }
    emit_step(
        "compute_valuation", "complete", parent_step_id,
        {
            "valuation": valuation_display,
            "valuation_flags": valuation_flags,
            "confidence_label": confidence_label,
            "confidence_breakdown": confidence_breakdown,
            "summary_line": f"implied=${implied_share_price:.2f} vs spot=${valuation['current_price']:.2f}, conf={confidence_label}",
        },
    )
    return {
        "valuation": valuation,
        "valuation_flags": valuation_flags,
        "confidence_label": confidence_label,
        "confidence_breakdown": confidence_breakdown,
    }


# ---------------------------------------------------------------------------
# Market-implied WACC sanity check
# ---------------------------------------------------------------------------


def compute_implied_wacc_node(state: dict) -> dict:
    """Reverse-solve WACC implied by current market price and compare to CAPM."""
    from .wacc import solve_implied_wacc  # noqa: PLC0415

    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("compute_implied_wacc", "start", parent_step_id)

    a = state["assumptions"]
    spot = float((state.get("market_snapshot") or {}).get("price", 0.0))
    shares_M = float(a["shares_outstanding"])   # millions
    net_debt_M = float(a["net_debt"])           # millions
    terminal_growth = float(a["terminal_growth"])
    capm_wacc = float(a["wacc"])
    projected = state["projected_fcff"]

    implied_ev_M = spot * shares_M + net_debt_M if spot > 0 else 0.0

    if implied_ev_M <= 0:
        result: dict = {
            "capm_wacc": capm_wacc,
            "implied_wacc": None,
            "gap_bps": None,
            "direction": None,
            "flag": False,
            "interpretation": "Market-implied WACC unavailable (no valid spot price).",
            "summary_line": f"CAPM {capm_wacc:.1%} — no market data for comparison",
        }
    else:
        implied = solve_implied_wacc(projected, terminal_growth, implied_ev_M)
        if implied is None:
            result = {
                "capm_wacc": capm_wacc,
                "implied_wacc": None,
                "gap_bps": None,
                "direction": None,
                "flag": False,
                "interpretation": "Market-implied WACC solver inconclusive (may indicate negative equity or extreme assumptions).",
                "summary_line": f"CAPM {capm_wacc:.1%} — solver inconclusive",
            }
        else:
            gap = capm_wacc - implied
            gap_bps = int(round(gap * 10_000))
            direction = "capm_above" if gap > 0 else "capm_below"
            flag = abs(gap) >= 0.015  # 150 bps threshold
            if direction == "capm_above":
                interpretation = (
                    f"Market prices in WACC ~{abs(gap_bps)}bps below CAPM estimate. "
                    "Consider whether quality/moat premium warrants a lower discount rate."
                )
            else:
                interpretation = (
                    f"Market prices in WACC ~{abs(gap_bps)}bps above CAPM estimate. "
                    "Market may be pricing in higher risk or slower growth than assumed."
                )
            flag_str = " ⚠" if flag else " ✓"
            result = {
                "capm_wacc": capm_wacc,
                "implied_wacc": round(implied, 5),
                "gap_bps": gap_bps,
                "direction": direction,
                "flag": flag,
                "interpretation": interpretation,
                "summary_line": f"CAPM {capm_wacc:.1%} vs implied {implied:.1%} ({gap_bps:+d}bps{flag_str})",
            }

    logger.info("DCF compute_implied_wacc summary=%s", result.get("summary_line"))
    emit_step("compute_implied_wacc", "complete", parent_step_id, {**result})
    return {"wacc_sanity": result}


# ---------------------------------------------------------------------------
# Sensitivity table
# ---------------------------------------------------------------------------


def sensitivity_node(state: dict) -> dict:
    """Build a WACC × terminal growth sensitivity matrix."""
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("sensitivity", "start", parent_step_id)
    projected = state["projected_fcff"]
    terminal_base = float(state["assumptions"]["terminal_growth"])
    wacc_base = float(state["assumptions"]["wacc"])
    shares = max(float(state["assumptions"]["shares_outstanding"]), 1e-6)
    net_debt = float(state["assumptions"]["net_debt"])
    table: list[dict[str, float]] = []

    for wacc in [wacc_base - 0.01, wacc_base, wacc_base + 0.01]:
        for tg in [terminal_base - 0.005, terminal_base, terminal_base + 0.005]:
            pv_sum = 0.0
            for row in projected:
                year = int(row["year"])
                pv_sum += float(row["fcff"]) / ((1.0 + wacc) ** year)
            terminal_fcf = float(projected[-1]["fcff"]) * (1.0 + tg)
            terminal_value = terminal_fcf / max((wacc - tg), 1e-6)
            terminal_pv = terminal_value / (
                (1.0 + wacc) ** int(projected[-1]["year"])
            )
            equity = pv_sum + terminal_pv - net_debt
            table.append({
                "wacc": round(wacc, 4),
                "terminal_growth": round(tg, 4),
                "implied_share_price": round(equity / shares, 4),
            })

    logger.info("DCF sensitivity rows=%d", len(table))
    emit_step("sensitivity", "complete", parent_step_id,
              {
                  "rows": len(table),
                  "sensitivity_table": table,
                  "wacc_base": round(wacc_base, 4),
                  "tgr_base": round(terminal_base, 4),
                  "summary_line": f"{len(table)} scenarios (WACC±1%, TGR±0.5%)",
              })
    return {"sensitivity_table": table}


# ---------------------------------------------------------------------------
# Finalize — persist output
# ---------------------------------------------------------------------------


def finalize_node(state: dict) -> dict:
    """Persist the full DCF output to disk and emit the terminal workflow span."""
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    logger.info("DCF finalize_node RUNNING thread=%s", get_run_dir().name if get_run_dir() else "?")
    emit_step("finalize", "start", parent_step_id)
    run_dir = get_run_dir()
    out_path = Path(run_dir) / "dcf_output.json"
    payload = {
        "workflow": "dcf",
        "generated_at": time.time(),
        "ticker": state["ticker"],
        "horizon_years": state["horizon_years"],
        "profile": state.get("profile", "default"),
        "profile_meta": state.get("profile_meta", {}),
        "confidence_label": state.get("confidence_label", "medium"),
        "assumptions": state["assumptions"],
        "assumption_provenance": state.get("assumption_provenance", {}),
        "fundamentals": state.get("fundamentals", {}),
        "assumption_conflicts": state.get("assumption_conflicts", []),
        "assumption_flags": state.get("assumption_flags", []),
        "valuation_flags": state.get("valuation_flags", []),
        "market_snapshot": state["market_snapshot"],
        "valuation": state["valuation"],
        "sensitivity_table": state["sensitivity_table"],
        "features": state.get("features") or {},
        "wacc_components": state.get("wacc_components") or {},
        # ── Reasoning artifacts (new in target architecture) ──
        "assumption_memo": state.get("assumption_memo") or {},
        "company_state": state.get("company_state") or {},
        # ── Confidence decomposition & WACC sanity ──
        "confidence_breakdown": state.get("confidence_breakdown") or {},
        "wacc_sanity": state.get("wacc_sanity") or {},
        # ── Thesis & analysis loop (new) ──
        "thesis": state.get("thesis") or {},
        "critique": state.get("critique") or {},
        # ── Evidence items (for human-readable ref resolution) ──
        "_evidence_items": (state.get("evidence_pack") or {}).get("items", []),
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "DCF finalize output_path=%s confidence=%s flags=%d",
        out_path,
        payload["confidence_label"],
        len(payload["assumption_flags"]) + len(payload["valuation_flags"]),
    )
    emit_step(
        "finalize", "complete", parent_step_id,
        {
            "result_path": str(out_path),
            "implied_share_price": payload["valuation"]["implied_share_price"],
            "confidence_label": payload["confidence_label"],
            "summary_line": f"saved dcf_output.json, implied=${payload['valuation']['implied_share_price']:.2f}",
        },
    )
    emit_workflow_terminal(
        parent_step_id=parent_step_id,
        status="completed",
        payload={
            "result_path": str(out_path),
            "implied_share_price": payload["valuation"]["implied_share_price"],
            "confidence_label": payload["confidence_label"],
            "flag_count": (
                len(payload["assumption_flags"])
                + len(payload["valuation_flags"])
            ),
        },
    )
    return {"result_path": str(out_path)}
