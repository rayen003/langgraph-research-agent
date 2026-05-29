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

from utils import get_artifacts_dir, get_run_dir

from .activity import emit_step, emit_workflow_terminal
from .analysis import (
    confidence_assessment_from_positions,
    conviction_direction_from_positions,
    normalize_divergence_verdict,
)
from .priors import check_valuation_sanity, compute_confidence_breakdown, compute_confidence_label
from .sources import extract_evidence_items

logger = logging.getLogger(__name__)


def _persisted_evidence_fields(state: dict[str, Any]) -> dict[str, Any]:
    """Serialize evidence pack items for report citations and the source drawer."""
    items = extract_evidence_items(state, text_limit=4000)
    pack = dict(state.get("evidence_pack") or {})
    if items:
        pack["items"] = items
    return {
        "_evidence_items": items,
        "evidence_pack": pack,
    }


# ---------------------------------------------------------------------------
# Market data snapshot
# ---------------------------------------------------------------------------


def _fetch_spot_price(ticker: str, state: dict) -> dict:
    """Try multiple sources for spot price. Returns {price, source} or {}."""
    # Source 1: FMP profile price already in state (zero network cost)
    profile_meta = state.get("profile_meta") or {}
    fmp_price = profile_meta.get("spot_price")
    if isinstance(fmp_price, (int, float)) and fmp_price > 0:
        return {"price": float(fmp_price), "source": "fmp_profile"}
    logger.warning(
        "DCF price source1 (state.profile_meta.spot_price) empty ticker=%s "
        "profile_meta_keys=%s", ticker, list(profile_meta.keys()),
    )

    # Source 2: yfinance recent history (most reliable for live price)
    try:
        import yfinance as yf  # type: ignore[import-untyped]
        history = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
        if not history.empty:
            return {"price": float(history["Close"].iloc[-1]), "source": "yfinance_history"}
    except Exception as exc:  # noqa: BLE001
        logger.debug("DCF price yfinance_history failed ticker=%s error=%s", ticker, exc)

    # Source 3: yfinance .info fields (slower but covers more tickers)
    try:
        import yfinance as yf  # type: ignore[import-untyped]
        info = yf.Ticker(ticker).info or {}
        for field in ("currentPrice", "regularMarketPrice", "previousClose", "navPrice"):
            val = info.get(field)
            if isinstance(val, (int, float)) and val > 0:
                return {"price": float(val), "source": f"yfinance_info.{field}"}
    except Exception as exc:  # noqa: BLE001
        logger.debug("DCF price yfinance_info failed ticker=%s error=%s", ticker, exc)

    # Source 4: direct FMP profile fetch as last resort (state lossage / yf rate limit guard)
    try:
        import os as _os  # noqa: PLC0415
        from .fundamentals import _fmp_get_json  # noqa: PLC0415
        api_key = _os.getenv("FMP_API_KEY") or _os.getenv("FINANCIAL_MODELING_PREP_API_KEY")
        if api_key:
            rows = _fmp_get_json(f"profile?symbol={ticker}", api_key)
            if rows:
                price = rows[0].get("price")
                if isinstance(price, (int, float)) and price > 0:
                    return {"price": float(price), "source": "fmp_profile_direct"}
    except Exception as exc:  # noqa: BLE001
        logger.debug("DCF price fmp_profile_direct failed ticker=%s error=%s", ticker, exc)

    return {}


def collect_market_data_node(state: dict) -> dict:
    """Fetch current spot price for implied-vs-market comparison.

    Source priority: FMP profile (already in state) → yfinance history →
    yfinance info. Falls back to sentinel 0.0 (signals unavailable) rather
    than a fake $100 that poisons implied-WACC solver.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("collect_market_data", "start", parent_step_id)
    ticker = state["ticker"]

    snapshot = _fetch_spot_price(ticker, state)
    if not snapshot:
        logger.warning(
            "DCF collect_market_data: all price sources failed ticker=%s — "
            "market-implied signals will be skipped", ticker,
        )
        snapshot = {"price": 0.0, "source": "unavailable"}

    logger.info("DCF collect_market_data ticker=%s snapshot=%s", ticker, json.dumps(snapshot))
    price_str = f"${snapshot['price']:.2f}" if snapshot["price"] > 0 else "unavailable"
    emit_step(
        "collect_market_data", "complete", parent_step_id,
        {"market_snapshot": snapshot, "summary_line": f"spot={price_str} ({snapshot['source']})"},
    )
    return {"market_snapshot": snapshot}


# ---------------------------------------------------------------------------
# Cash flow projections
# ---------------------------------------------------------------------------


def project_cashflows_node(state: dict) -> dict:
    """Project FCFF over the forecast horizon from locked assumptions.

    Two glide paths supported, both linear from Y1 to Y_N:

    1. Revenue growth glide: ``revenue_growth_terminal`` (optional).
       Hypergrowth fades to mature (NVDA 25%→12%); declining accelerates (F 0%→-1%).
       Defaults to ``revenue_growth`` for all years.

    2. FCFF margin glide: ``fcff_margin_terminal`` (optional).
       Expansion (WMT ads 1.87%→3.5%); compression (NVDA 46%→38%).
       Defaults to ``fcff_margin`` for all years.

    Both glide rates and assumption derivation belong in the semantic layer
    (synthesis → memo); math layer just consumes the values.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("project_cashflows", "start", parent_step_id)
    a = state["assumptions"]
    revenue = float(a["base_revenue"])
    growth_start = float(a["revenue_growth"])
    growth_end = float(a.get("revenue_growth_terminal", growth_start) or growth_start)
    margin_start = float(a["fcff_margin"])
    margin_end = float(a.get("fcff_margin_terminal", margin_start) or margin_start)
    # SBC adjustment: stock-based comp is real shareholder dilution but OCF
    # adds it back (non-cash item). Subtract from FCFF margin to capture
    # economic cost. Tech: 3-10% of revenue. Industrials: <1%. Default 0.
    sbc_pct = float(a.get("sbc_pct_revenue", 0.0) or 0.0)
    horizon = int(state["horizon_years"])
    denom = max(horizon - 1, 1)  # avoid div-by-zero for horizon=1
    projected: list[dict[str, float]] = []
    for year in range(1, horizon + 1):
        growth_n = growth_start + (growth_end - growth_start) * (year - 1) / denom
        margin_n = margin_start + (margin_end - margin_start) * (year - 1) / denom
        effective_margin_n = margin_n - sbc_pct
        revenue *= 1.0 + growth_n
        fcff = revenue * effective_margin_n
        projected.append({
            "year": float(year),
            "revenue": revenue,
            "fcff": fcff,
            "margin": margin_n,
            "effective_margin": effective_margin_n,
            "growth": growth_n,
        })

    glide_notes = []
    if growth_start != growth_end:
        glide_notes.append(f"growth {growth_start*100:.1f}%→{growth_end*100:.1f}%")
    if margin_start != margin_end:
        glide_notes.append(f"margin {margin_start*100:.1f}%→{margin_end*100:.1f}%")
    if sbc_pct > 0:
        glide_notes.append(f"SBC −{sbc_pct*100:.1f}%")
    glide_suffix = (", " + ", ".join(glide_notes)) if glide_notes else ""

    emit_step(
        "project_cashflows", "complete", parent_step_id,
        {
            "rows": len(projected),
            "growth_start": growth_start,
            "growth_end": growth_end,
            "margin_start": margin_start,
            "margin_end": margin_end,
            "sbc_pct_revenue": sbc_pct,
            "projections": [
                {
                    "year": int(row["year"]),
                    # base_revenue is in millions (FMP /1M), so divide by 1000 for billions
                    "revenue_B": round(row["revenue"] / 1000.0, 2),
                    "fcff_B": round(row["fcff"] / 1000.0, 2),
                    "margin_pct": round(row["margin"] * 100, 2),
                    "effective_margin_pct": round(row["effective_margin"] * 100, 2),
                    "growth_pct": round(row["growth"] * 100, 2),
                }
                for row in projected
            ],
            "summary_line": f"{len(projected)}yr FCFF projected{glide_suffix}",
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

    horizon = int(projected[-1]["year"]) if projected else 5
    buyback_yield = float(a.get("buyback_yield", 0.0) or 0.0)
    shares_initial = float(a["shares_outstanding"])
    shares_end = shares_initial * ((1.0 - buyback_yield) ** horizon)

    # Terminal buyback compounding: a holder of one share today sees their
    # claim on aggregate equity grow at the buyback yield in perpetuity, since
    # other shares are being retired. So per-share equity grows at
    # (terminal_growth + effective_perpetual_buyback) in steady state.
    #
    # Economic cap: the perpetual buyback rate cannot exceed the FCF yield
    # against terminal equity, otherwise buybacks would consume more cash
    # than the business produces. Cap at min(buyback_yield, fcff_yield, 0.04)
    # to prevent runaway compounding.
    terminal_fcf_aggregate = float(projected[-1]["fcff"]) * (1.0 + terminal_growth)
    pre_buyback_terminal_value = terminal_fcf_aggregate / max((wacc - terminal_growth), 1e-6)
    pre_buyback_terminal_equity = pre_buyback_terminal_value - float(a["net_debt"])
    fcff_yield_terminal = (
        terminal_fcf_aggregate / pre_buyback_terminal_equity
        if pre_buyback_terminal_equity > 0
        else 0.0
    )
    perpetual_buyback_cap = max(0.0, min(buyback_yield, fcff_yield_terminal, 0.04))
    perpetual_buyback_yield = perpetual_buyback_cap
    # Safety: keep WACC strictly above (g + buyback) with min spread of 50bps
    if wacc - terminal_growth - perpetual_buyback_yield < 0.005:
        perpetual_buyback_yield = max(0.0, wacc - terminal_growth - 0.005)

    effective_terminal_growth = terminal_growth + perpetual_buyback_yield
    terminal_value = terminal_fcf_aggregate / max((wacc - effective_terminal_growth), 1e-6)
    terminal_pv = terminal_value / ((1.0 + wacc) ** int(projected[-1]["year"]))
    enterprise_value = pv_sum + terminal_pv
    equity_value = enterprise_value - float(a["net_debt"])

    implied_share_price = equity_value / max(shares_end, 1e-6)

    valuation = {
        "pv_cash_flows": pv_sum,
        "terminal_value": terminal_value,           # undiscounted
        "terminal_pv": terminal_pv,                  # discounted — used in EV
        "enterprise_value": enterprise_value,         # = pv_sum + terminal_pv
        "equity_value": equity_value,                 # = EV - net_debt
        "implied_share_price": implied_share_price,   # = equity / shares_end
        "shares_end": shares_end,                     # after horizon of buybacks
        "shares_initial": shares_initial,
        "buyback_yield": buyback_yield,
        "perpetual_buyback_yield": perpetual_buyback_yield,
        "perpetual_buyback_cap_source": (
            "input" if perpetual_buyback_cap == buyback_yield
            else "fcff_yield_cap" if perpetual_buyback_cap == fcff_yield_terminal
            else "hard_cap_4pct"
        ) if buyback_yield > 0 else "no_buyback",
        "effective_terminal_growth": effective_terminal_growth,
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


def _render_sensitivity_heatmap(table: list[dict[str, float]], ticker: str) -> str | None:
    """Write a WACC × terminal-growth heatmap PNG; return path relative to run dir."""
    if not table:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        waccs = sorted({float(r["wacc"]) for r in table})
        tgrs = sorted({float(r["terminal_growth"]) for r in table})
        matrix = np.full((len(tgrs), len(waccs)), np.nan)
        for row in table:
            wi = waccs.index(float(row["wacc"]))
            ti = tgrs.index(float(row["terminal_growth"]))
            matrix[ti, wi] = float(row["implied_share_price"])

        fig, ax = plt.subplots(figsize=(7, 4.5))
        im = ax.imshow(matrix, aspect="auto", cmap="RdYlGn")
        ax.set_xticks(range(len(waccs)))
        ax.set_xticklabels([f"{w:.1%}" for w in waccs], rotation=45, ha="right")
        ax.set_yticks(range(len(tgrs)))
        ax.set_yticklabels([f"{t:.1%}" for t in tgrs])
        ax.set_xlabel("WACC")
        ax.set_ylabel("Terminal growth")
        ax.set_title(f"{ticker.upper()} — implied share price sensitivity")
        for ti in range(len(tgrs)):
            for wi in range(len(waccs)):
                val = matrix[ti, wi]
                if not np.isnan(val):
                    ax.text(wi, ti, f"${val:.0f}", ha="center", va="center", fontsize=8)
        fig.colorbar(im, ax=ax, label="Implied price ($)")
        fig.tight_layout()
        # Include parent_step_id (run_id) in filename so multiple DCF runs
        # in the same session/run-dir don't clobber each other's charts.
        # Falls back to generic name when run_id is the default string.
        run_dir = get_run_dir()
        run_suffix = run_dir.name if run_dir else "run"
        out_path = get_artifacts_dir() / f"sensitivity_{ticker.lower()}_{run_suffix[:16]}.png"
        fig.savefig(out_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        # `relative_to` would raise ValueError if get_run_dir() doesn't share
        # a common prefix with out_path (happens when executor workers don't
        # inherit the run_id ContextVar). Fall back to absolute path so the
        # chart never silently drops out of dcf_output.json["sensitivity_chart"].
        try:
            return str(out_path.relative_to(get_run_dir()))
        except ValueError:
            logger.warning(
                "Sensitivity chart written outside run dir; using absolute path: %s",
                out_path,
            )
            return str(out_path)
    except Exception:
        logger.exception("Failed to render sensitivity heatmap ticker=%s", ticker)
        return None


def sensitivity_node(state: dict) -> dict:
    """Build a WACC × terminal growth sensitivity matrix."""
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("sensitivity", "start", parent_step_id)
    projected = state["projected_fcff"]
    terminal_base = float(state["assumptions"]["terminal_growth"])
    wacc_base = float(state["assumptions"]["wacc"])
    shares_initial = max(float(state["assumptions"]["shares_outstanding"]), 1e-6)
    net_debt = float(state["assumptions"]["net_debt"])
    buyback_yield = float(state["assumptions"].get("buyback_yield", 0.0) or 0.0)
    horizon = int(projected[-1]["year"]) if projected else int(state.get("horizon_years", 5))
    shares_end = shares_initial * ((1.0 - buyback_yield) ** horizon)
    table: list[dict[str, float]] = []

    for wacc in [wacc_base - 0.01, wacc_base, wacc_base + 0.01]:
        for tg in [terminal_base - 0.005, terminal_base, terminal_base + 0.005]:
            pv_sum = 0.0
            for row in projected:
                year = int(row["year"])
                pv_sum += float(row["fcff"]) / ((1.0 + wacc) ** year)
            terminal_fcf = float(projected[-1]["fcff"]) * (1.0 + tg)
            pre_buyback_terminal_value = terminal_fcf / max((wacc - tg), 1e-6)
            pre_buyback_terminal_equity = pre_buyback_terminal_value - net_debt
            fcff_yield_terminal = (
                terminal_fcf / pre_buyback_terminal_equity
                if pre_buyback_terminal_equity > 0
                else 0.0
            )
            perpetual_buyback_yield = max(0.0, min(buyback_yield, fcff_yield_terminal, 0.04))
            if wacc - tg - perpetual_buyback_yield < 0.005:
                perpetual_buyback_yield = max(0.0, wacc - tg - 0.005)
            effective_terminal_growth = tg + perpetual_buyback_yield
            terminal_value = terminal_fcf / max((wacc - effective_terminal_growth), 1e-6)
            terminal_pv = terminal_value / (
                (1.0 + wacc) ** int(projected[-1]["year"])
            )
            equity = pv_sum + terminal_pv - net_debt
            table.append({
                "wacc": round(wacc, 4),
                "terminal_growth": round(tg, 4),
                "implied_share_price": round(equity / max(shares_end, 1e-6), 4),
            })

    logger.info("DCF sensitivity rows=%d", len(table))
    chart_path = _render_sensitivity_heatmap(table, str(state.get("ticker") or "?"))
    emit_step("sensitivity", "complete", parent_step_id,
              {
                  "rows": len(table),
                  "sensitivity_table": table,
                  "wacc_base": round(wacc_base, 4),
                  "tgr_base": round(terminal_base, 4),
                  "summary_line": f"{len(table)} scenarios (WACC±1%, TGR±0.5%)",
              })
    result: dict[str, Any] = {"sensitivity_table": table}
    if chart_path:
        result["sensitivity_chart"] = chart_path
    return result


# ---------------------------------------------------------------------------
# Finalize — persist output
# ---------------------------------------------------------------------------


def _resolve_chart_path(raw: str | None, run_dir: Path) -> str | None:
    """Normalize sensitivity_chart to a usable absolute path or None.

    sensitivity_node returns either a relative path (`artifacts/foo.png`)
    when get_run_dir() shares a prefix, or an absolute path otherwise.
    Downstream consumers (deck adapter, report exporter) want a path that
    .exists() returns True against without knowing the run dir. Also
    falls back to disk discovery so a chart written via an out-of-context
    executor still surfaces in dcf_output.json.
    """
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = run_dir / raw
        if p.exists():
            return str(p)
    # Disk discovery fallback — chart may exist even if state lost the path.
    # Use most-recently-modified file so a new run's chart isn't shadowed by
    # an older one with an earlier alphabetical name in the same directory.
    artifacts_dir = run_dir / "artifacts"
    if artifacts_dir.exists():
        candidates = sorted(
            artifacts_dir.glob("sensitivity_*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return str(candidates[0])
    return None


def finalize_node(state: dict) -> dict:
    """Persist the full DCF output to disk and emit the terminal workflow span."""
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    logger.info("DCF finalize_node RUNNING thread=%s", get_run_dir().name if get_run_dir() else "?")
    emit_step("finalize", "start", parent_step_id)

    # ── Phase 1: re-gate confidence with validity / solver / divergence info ─
    # compute_valuation_node ran before convergence_gate, so its confidence
    # score doesn't yet reflect model_validity, solver_failed, or unresolved
    # divergence verdicts. Recompute here so finalize-time confidence is honest.
    _model_validity = state.get("model_validity", "valid")
    _solver_status = (state.get("wacc_sanity") or {}).get("solver_status", "ok")
    _solver_failed = _solver_status in {"no_convergence", "degenerate", "exception", "no_input"}
    _positions = state.get("analysis_positions") or []
    _unresolved = sum(
        1
        for p in _positions
        if normalize_divergence_verdict(p) in {"contradicted", "unsupported", "insufficient_evidence"}
    )
    final_confidence_breakdown = state.get("confidence_breakdown")
    final_confidence_label = state.get("confidence_label", "medium")
    if _model_validity != "valid" or _solver_failed or _unresolved > 0:
        regated = compute_confidence_breakdown(
            assumption_flags=state.get("assumption_flags") or [],
            valuation_flags=state.get("valuation_flags") or [],
            provenance=state.get("assumption_provenance") or {},
            assumption_memo=state.get("assumption_memo"),
            model_validity=_model_validity,
            solver_failed=_solver_failed,
            unexplained_count=_unresolved,
        )
        final_confidence_breakdown = regated
        final_confidence_label = regated["label"]
        logger.info(
            "DCF finalize re-gated confidence label=%s aggregate=%.3f validity=%s solver_failed=%s unresolved=%d",
            regated["label"], regated.get("aggregate_score", 0.0),
            _model_validity, _solver_failed, _unresolved,
        )
        # Leak fix #2: the earlier compute_valuation activity card still
        # shows the pre-gate (often HIGH) confidence — re-emit the same
        # activity_id with completed status + new confidence so the UI
        # overwrites the stale chip via mergeActivity.
        try:
            emit_step(
                "compute_valuation", "complete", parent_step_id,
                {
                    "summary_line": (
                        f"re-gated after validity check: "
                        f"conf={regated['label']} (was earlier label, now reflects validity)"
                    ),
                    "confidence_label": regated["label"],
                    "confidence_breakdown": regated,
                    "model_validity_at_finalize": _model_validity,
                },
            )
        except Exception:
            logger.exception("Failed to re-emit compute_valuation activity after re-gate")

    final_confidence_assessment = state.get("confidence_assessment") or confidence_assessment_from_positions(
        positions=_positions,
        model_validity=_model_validity,
        procedural_base=(final_confidence_breakdown or {}).get("aggregate_score"),
    )

    # Leak fix #3: when the model is invalid AND the analysis loop produced
    # queued adjustments, surface whether those adjustments affected the price.
    #
    # Two sub-cases:
    #   a) Option C ran (analysis_iteration > max_iter+1):  the adjustments
    #      from earlier iterations WERE applied via the bonus valuation pass.
    #      The queued positions in state come from the FINAL analysis pass
    #      (run after the bonus pass) — correctly not applied (hard stop).
    #      Displayed price already reflects the earlier adjustments.
    #   b) Option C did NOT run (analysis_iteration <= max_iter+1): the gate
    #      went straight to finalize — queued adjustments were never applied
    #      and the displayed price is stale. Warn clearly.
    _CONVERGENCE_MAX_ITER = 2  # keep in sync with convergence_gate_node
    _analysis_iteration = state.get("analysis_iteration", 0)
    _option_c_ran = _analysis_iteration > _CONVERGENCE_MAX_ITER + 1

    if _model_validity == "invalid":
        queued = [
            p for p in _positions
            if p.get("position") == "EXPLAINED" and p.get("adjustment")
        ]
        if queued:
            try:
                fields = [
                    str(((p.get("adjustment") or {}).get("field")) or "?")
                    for p in queued
                ]
                if _option_c_ran:
                    # Post-bonus-pass suggestions — price is up-to-date.
                    summary = (
                        f"After final adjustment pass, {len(queued)} additional "
                        f"adjustment(s) suggested ({', '.join(fields)}) but not "
                        f"applied — hard stop reached. Displayed price reflects "
                        f"all previously applied adjustments."
                    )
                    reason = "post_final_pass_suggestions_not_applied"
                else:
                    # No bonus pass — price is genuinely pre-adjustment.
                    summary = (
                        f"{len(queued)} adjustment(s) queued ({', '.join(fields)}) "
                        f"but valuation was NOT re-run — model halted as invalid. "
                        f"Displayed price reflects pre-adjustment assumptions."
                    )
                    reason = "model_validity=invalid before re-iteration"
                emit_step(
                    "adjustments_not_executed", "complete", parent_step_id,
                    {
                        "summary_line": summary,
                        "queued_count": len(queued),
                        "queued_fields": fields,
                        "option_c_ran": _option_c_ran,
                        "reason": reason,
                    },
                )
            except Exception:
                logger.exception("Failed to emit adjustments_not_executed activity")
    run_dir = get_run_dir()
    out_path = Path(run_dir) / "dcf_output.json"
    payload = {
        "workflow": "dcf",
        "generated_at": time.time(),
        "ticker": state["ticker"],
        "horizon_years": state["horizon_years"],
        "profile": state.get("profile", "default"),
        "profile_meta": state.get("profile_meta", {}),
        "confidence_label": final_confidence_label,
        "assumptions": state["assumptions"],
        "assumption_provenance": state.get("assumption_provenance", {}),
        "fundamentals": state.get("fundamentals", {}),
        "assumption_conflicts": state.get("assumption_conflicts", []),
        "assumption_flags": state.get("assumption_flags", []),
        "valuation_flags": state.get("valuation_flags", []),
        "market_snapshot": state["market_snapshot"],
        "valuation": state["valuation"],
        "sensitivity_table": state["sensitivity_table"],
        "sensitivity_chart": _resolve_chart_path(state.get("sensitivity_chart"), run_dir),
        "features": state.get("features") or {},
        "wacc_components": state.get("wacc_components") or {},
        # ── Reasoning artifacts (new in target architecture) ──
        "assumption_memo": state.get("assumption_memo") or {},
        "company_state": state.get("company_state") or {},
        # ── Confidence decomposition & WACC sanity ──
        "confidence_breakdown": final_confidence_breakdown or {},
        "confidence_assessment": final_confidence_assessment or {},
        "wacc_sanity": state.get("wacc_sanity") or {},
        # ── Thesis & analysis loop (new) ──
        "thesis": state.get("thesis") or {},
        "critique": state.get("critique") or {},
        # ── Divergence analysis layer ──
        "divergences": state.get("divergences") or [],
        "analysis_positions": state.get("analysis_positions") or [],
        "model_validity": state.get("model_validity", "valid"),
        "invalidation_reason": state.get("invalidation_reason", ""),
        "reconciliation_status": state.get("reconciliation_status", "aligned"),
        "reconciliation_note": state.get("reconciliation_note", ""),
        "implied_growth": state.get("implied_growth"),
        "implied_margin": state.get("implied_margin"),
        "market_signals_meta": state.get("market_signals_meta") or {},
        "effective_confidence": state.get("effective_confidence"),
        "conviction_direction": state.get("conviction_direction"),
        # ── Coherence gate (pre-valuation) ──
        "coherence_assessment": state.get("coherence_assessment") or {},
        "coherence_adjustments": state.get("coherence_adjustments") or {},
        # ── Scenario-based valuation ──
        "scenarios": state.get("scenarios") or [],
        "scenario_results": state.get("scenario_results") or [],
        # ── Evidence items (for human-readable ref resolution) ──
        **_persisted_evidence_fields(state),
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── Conviction — deterministic judgment (output-only, not state) ────
    critique = state.get("critique") or {}
    thesis = state.get("thesis") or {}
    implied_growth = state.get("implied_growth")
    implied_margin = state.get("implied_margin")
    market_price = float((state.get("market_snapshot") or {}).get("price", 0))
    scenarios = state.get("scenario_results") or []

    conviction = {
        "direction": state.get("conviction_direction") or "genuine_uncertainty",
        "confidence": 0.3,
        "gap_pct": 0.0,
        "dispersion_pct": 0.0,
        "unanchored_count": 0,
    }
    try:
        gap = abs(payload["valuation"]["implied_share_price"] / max(market_price, 1) - 1)
        prices = [r["valuation"].get("implied_share_price", 0) for r in scenarios if r.get("valuation", {}).get("implied_share_price")]
        dispersion = (max(prices) - min(prices)) / (sum(prices) / len(prices)) if len(prices) >= 3 else 0
        unanchored = sum(1 for f in critique.get("findings", []) if f.get("is_unanchored"))

        direction = state.get("conviction_direction") or conviction_direction_from_positions(_positions)
        if unanchored >= 3 or dispersion > 0.5:
            direction, conf = "genuine_uncertainty", 0.25
        elif direction == "market_overpaying":
            conf = 0.6
        elif direction in {"unresolved_expectations", "structural_premium"}:
            conf = 0.4
        elif direction == "model_too_conservative":
            conf = 0.5
        else:
            direction, conf = "genuine_uncertainty", 0.3

        conviction = {"direction": direction, "confidence": conf, "gap_pct": round(gap * 100, 1), "dispersion_pct": round(dispersion * 100, 1), "unanchored_count": unanchored}
        payload["conviction"] = conviction
        logger.info("DCF conviction ticker=%s direction=%s confidence=%.2f", state["ticker"], direction, conf)
    except Exception:
        logger.warning("DCF conviction computation failed for %s", state["ticker"], exc_info=True)
        payload["conviction"] = conviction

    # ── Re-write with conviction ────────────────────────────────────────
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
    implied_str = f"${payload['valuation']['implied_share_price']:.2f}"
    if scenarios and len(scenarios) >= 3:
        prices = [r["valuation"].get("implied_share_price", 0) for r in scenarios]
        summary_line = (
            f"Expected=${payload['valuation'].get('implied_share_price', 0):.2f} "
            f"range=${min(prices):.2f}–${max(prices):.2f}"
        )
    else:
        summary_line = f"saved dcf_output.json, implied={implied_str}"
    # ── Emit assumption_journey if review iterations happened ─────────────
    assumption_history = state.get("assumption_history") or []
    initial_assumptions = state.get("initial_assumptions") or {}
    if assumption_history and initial_assumptions:
        final_base = state.get("assumptions") or {}
        final_scenarios = {
            s.get("name", f"sc_{i}"): dict(s.get("assumptions") or {})
            for i, s in enumerate(state.get("scenarios") or [])
        }
        emit_step(
            "assumption_journey", "complete", parent_step_id,
            {
                "summary_line": (
                    f"{len(assumption_history)} review iteration"
                    f"{'s' if len(assumption_history) != 1 else ''}"
                ),
                "iterations": assumption_history,
                "initial": initial_assumptions,
                "final": {"base": final_base, "scenarios": final_scenarios},
            },
        )

    emit_step(
        "finalize", "complete", parent_step_id,
        {
            "result_path": str(out_path),
            "implied_share_price": payload["valuation"]["implied_share_price"],
            "confidence_label": payload["confidence_label"],
            "summary_line": summary_line,
            "scenario_count": len(scenarios),
        },
    )
    # Phase 2: surface model_validity so the frontend can render a degraded
    # banner. "completed_degraded" is a distinct terminal status the UI maps
    # to a red/amber banner; "completed" stays green.
    _validity_for_emit = payload.get("model_validity", "valid")
    _terminal_status = "completed_degraded" if _validity_for_emit == "invalid" else "completed"
    emit_workflow_terminal(
        parent_step_id=parent_step_id,
        status=_terminal_status,
        payload={
            "result_path": str(out_path),
            "implied_share_price": payload["valuation"]["implied_share_price"],
            "confidence_label": payload["confidence_label"],
            "model_validity": _validity_for_emit,
            "invalidation_reason": payload.get("invalidation_reason") or "",
            "flag_count": (
                len(payload["assumption_flags"])
                + len(payload["valuation_flags"])
            ),
        },
    )

    # ── KG quality gate ─────────────────────────────────────────────────────
    # Two-tier write:
    #   • Run-scoped records (dcf_run, run_assumption, run_output): ALWAYS
    #     written. These are immutable audit history, can't poison anything.
    #   • Shared facts (company anchor, market_metric, synthesis, thesis,
    #     drivers): gated on model_validity + effective_confidence so a bad
    #     run doesn't pollute the cache that future runs hit.
    model_validity = state.get("model_validity", "valid")
    eff_conf = state.get("effective_confidence")
    kg_min_confidence = 0.55
    write_shared = (
        model_validity != "invalid"
        and not (isinstance(eff_conf, (int, float)) and eff_conf < kg_min_confidence)
    )
    try:
        _write_to_kg(state, payload, parent_step_id, write_shared=write_shared)
    except Exception:  # noqa: BLE001
        logger.exception("DCF KG back-write failed (non-fatal)")

    return {
        "result_path": str(out_path),
        "confidence_label": final_confidence_label,
        "confidence_breakdown": final_confidence_breakdown or {},
    }


def _write_to_kg(state: dict, payload: dict, run_id: str, write_shared: bool = True) -> None:
    """Write DCF run results into the Knowledge Graph.

    Two-tier persistence:
      • Run-scoped records (always written): dcf_run, run_assumption, run_output.
        Immutable audit history keyed by run_id.
      • Shared facts (only when ``write_shared`` is True): company anchor,
        market_metric_fund, company_synthesis, thesis, drivers. These power
        future cache hits, so a low-quality run is excluded to avoid
        poisoning the cache.
    """
    from kg import get_cache  # noqa: PLC0415

    cache = get_cache()
    ticker = payload["ticker"]
    session_id = state.get("session_id") or ""

    # ── Company anchor + DCFRun ─────────────────────────────────────────────
    # Company anchor is shared but cheap and idempotent — write always so
    # run-scoped edges (HAS_RUN) have a valid src_id even on invalid runs.
    cache.put(
        ticker=ticker, node_type="company", field="anchor",
        value={"ticker": ticker},
        source="agent_inferred", confidence=1.0, session_id=session_id,
    )
    company_node_id = f"{ticker}::company::anchor"

    cache.put(
        ticker=ticker, node_type="dcf_run", field="meta",
        value={
            "horizon_years": payload["horizon_years"],
            "profile": payload.get("profile"),
            "confidence_label": payload["confidence_label"],
            "result_path": payload.get("result_path") or "",
            # Phase 2: persist validity so KG can distinguish reliable runs
            # from degraded ones (the UI can colour-code dcf_run nodes).
            "model_validity": payload.get("model_validity", "valid"),
            "invalidation_reason": payload.get("invalidation_reason") or "",
        },
        source="dcf_output", confidence=1.0,
        run_id=run_id, session_id=session_id,
    )
    dcf_run_id = f"{ticker}::dcf_run::{run_id}::meta"
    cache.add_edge(
        src_id=company_node_id, tgt_id=dcf_run_id,
        relation="HAS_RUN", session_id=session_id, source="dcf_output",
    )

    # ── Run-scoped assumptions ──────────────────────────────────────────────
    assumptions = payload.get("assumptions") or {}
    provenance = payload.get("assumption_provenance") or {}
    for field, value in assumptions.items():
        prov = provenance.get(field, "dcf_output")
        source = prov.get("source", "dcf_output") if isinstance(prov, dict) else str(prov)
        cache.put(
            ticker=ticker, node_type="run_assumption", field=field,
            value=value, source=source, confidence=0.95,
            run_id=run_id, session_id=session_id, respect_user_lock=False,
        )
        cache.add_edge(
            src_id=dcf_run_id,
            tgt_id=f"{ticker}::run_assumption::{run_id}::{field}",
            relation="LOCKED_ASSUMPTION", session_id=session_id,
            source="dcf_output",
        )

    # ── Run-scoped outputs ──────────────────────────────────────────────────
    valuation = payload.get("valuation") or {}
    output_fields = ("implied_share_price", "enterprise_value", "equity_value",
                     "pv_cash_flows", "terminal_pv")
    for field in output_fields:
        if field in valuation:
            cache.put(
                ticker=ticker, node_type="run_output", field=field,
                value=valuation[field], source="dcf_output", confidence=1.0,
                run_id=run_id, session_id=session_id, respect_user_lock=False,
            )
            cache.add_edge(
                src_id=dcf_run_id,
                tgt_id=f"{ticker}::run_output::{run_id}::{field}",
                relation="PRODUCES", session_id=session_id, source="dcf_output",
            )

    shared_written = 0
    if write_shared:
        # ── Shared fundamentals refresh ────────────────────────────────────
        for field in ("base_revenue", "shares_outstanding", "net_debt"):
            if field in assumptions:
                cache.put(
                    ticker=ticker, node_type="market_metric_fund", field=field,
                    value=assumptions[field],
                    source=(lambda p: p.get("source", "fmp") if isinstance(p, dict) else str(p))(provenance.get(field, "fmp")),
                    confidence=0.95, session_id=session_id,
                )
                cache.add_edge(
                    src_id=company_node_id,
                    tgt_id=f"{ticker}::market_metric_fund::{field}",
                    relation="HAS_METRIC", session_id=session_id, source="dcf_output",
                )
                shared_written += 1

        # ── Shared synthesis + thesis (input_hash for compound staleness) ─
        input_hash = cache.evidence_hash(ticker)
        company_state = state.get("company_state") or {}
        if company_state:
            cache.put(
                ticker=ticker, node_type="company_synthesis", field="full",
                value=company_state, source="llm_inferred", confidence=0.85,
                input_hash=input_hash, session_id=session_id,
            )
            cache.add_edge(
                src_id=company_node_id,
                tgt_id=f"{ticker}::company_synthesis::full",
                relation="HAS_SYNTHESIS", session_id=session_id, source="llm_inferred",
            )
            shared_written += 1
        thesis = state.get("thesis") or {}
        if thesis and not thesis.get("_fallback"):
            cache.put(
                ticker=ticker, node_type="thesis", field="full",
                value=thesis, source="llm_inferred", confidence=0.85,
                input_hash=input_hash, session_id=session_id,
            )
            cache.add_edge(
                src_id=company_node_id,
                tgt_id=f"{ticker}::thesis::full",
                relation="HAS_THESIS", session_id=session_id, source="llm_inferred",
            )
            shared_written += 1

        # ── Drivers from thesis (shared, company-level) ───────────────────
        for driver in (thesis.get("key_drivers") or []):
            if not isinstance(driver, dict):
                continue
            d_name = str(driver.get("driver", "")).strip().replace(" ", "_")
            if not d_name:
                continue
            cache.put(
                ticker=ticker, node_type="driver", field=d_name,
                value={
                    "direction": driver.get("direction", "neutral"),
                    "conviction": driver.get("conviction", "medium"),
                },
                source="llm_inferred", confidence=0.75, session_id=session_id,
            )
            cache.add_edge(
                src_id=company_node_id,
                tgt_id=f"{ticker}::driver::{d_name}",
                relation="HAS_DRIVER", session_id=session_id, source="llm_inferred",
            )
            shared_written += 1

    emit_step(
        "kg_backwrite", "complete", run_id,
        {
            "summary_line": (
                f"wrote DCFRun + {len(assumptions)} assumptions, "
                f"{sum(1 for f in output_fields if f in valuation)} outputs"
                + (f", {shared_written} shared facts refreshed" if write_shared else " (shared facts skipped: model invalid)")
            ),
            "run_node_id": dcf_run_id,
            "ticker": ticker,
            "shared_written": write_shared,
            "shared_count": shared_written,
        },
    )


# ---------------------------------------------------------------------------
# Market-implied signals — reverse DCF via bisection
# ---------------------------------------------------------------------------


def _compute_dcf_value(
    base_revenue: float,
    revenue_growth: float,
    fcff_margin: float,
    wacc: float,
    terminal_growth: float,
    horizon: int,
    net_debt: float,
    shares_outstanding: float,
    *,
    revenue_growth_terminal: float | None = None,
    fcff_margin_terminal: float | None = None,
    sbc_pct_revenue: float = 0.0,
    buyback_yield: float = 0.0,
) -> float:
    """Compute implied share price from assumptions (pure function, no state).

    Mirrors ``project_cashflows_node`` + ``compute_valuation_node`` so reverse-DCF
    implied growth/margin are comparable to the forward model.
    """
    growth_start = float(revenue_growth)
    growth_end = float(revenue_growth_terminal if revenue_growth_terminal is not None else revenue_growth)
    margin_start = float(fcff_margin)
    margin_end = float(fcff_margin_terminal if fcff_margin_terminal is not None else fcff_margin)
    denom = max(horizon - 1, 1)

    revenue = base_revenue
    projected: list[dict[str, float]] = []
    for year in range(1, horizon + 1):
        growth_n = growth_start + (growth_end - growth_start) * (year - 1) / denom
        margin_n = margin_start + (margin_end - margin_start) * (year - 1) / denom
        effective_margin_n = margin_n - float(sbc_pct_revenue)
        revenue *= 1.0 + growth_n
        fcff = revenue * effective_margin_n
        projected.append({"year": float(year), "fcff": fcff})

    pv_sum = sum(row["fcff"] / ((1.0 + wacc) ** int(row["year"])) for row in projected)
    terminal_fcf = projected[-1]["fcff"] * (1.0 + terminal_growth)
    pre_buyback_terminal_value = terminal_fcf / max((wacc - terminal_growth), 1e-6)
    pre_buyback_terminal_equity = pre_buyback_terminal_value - net_debt
    fcff_yield_terminal = (
        terminal_fcf / pre_buyback_terminal_equity
        if pre_buyback_terminal_equity > 0 else 0.0
    )
    perpetual_buyback = max(0.0, min(float(buyback_yield), fcff_yield_terminal, 0.04))
    if wacc - terminal_growth - perpetual_buyback < 0.005:
        perpetual_buyback = max(0.0, wacc - terminal_growth - 0.005)
    effective_terminal_growth = terminal_growth + perpetual_buyback
    terminal_value = terminal_fcf / max((wacc - effective_terminal_growth), 1e-6)
    terminal_pv = terminal_value / ((1.0 + wacc) ** horizon)
    enterprise_value = pv_sum + terminal_pv
    equity_value = enterprise_value - net_debt
    shares_end = shares_outstanding * ((1.0 - float(buyback_yield)) ** horizon)
    return equity_value / max(shares_end, 1e-6)


def _dcf_value_from_assumptions(assumptions: dict[str, float]) -> float:
    """Shared forward-DCF price from a full assumptions dict."""
    horizon = 5
    return _compute_dcf_value(
        base_revenue=float(assumptions.get("base_revenue", 0)),
        revenue_growth=float(assumptions.get("revenue_growth", 0)),
        fcff_margin=float(assumptions.get("fcff_margin", 0)),
        wacc=float(assumptions.get("wacc", 0.10)),
        terminal_growth=float(assumptions.get("terminal_growth", 0.025)),
        horizon=horizon,
        net_debt=float(assumptions.get("net_debt", 0)),
        shares_outstanding=float(assumptions.get("shares_outstanding", 1)),
        revenue_growth_terminal=assumptions.get("revenue_growth_terminal"),  # type: ignore[arg-type]
        fcff_margin_terminal=assumptions.get("fcff_margin_terminal"),  # type: ignore[arg-type]
        sbc_pct_revenue=float(assumptions.get("sbc_pct_revenue", 0.0) or 0.0),
        buyback_yield=float(assumptions.get("buyback_yield", 0.0) or 0.0),
    )


def compute_implied_growth(assumptions: dict[str, float], market_price: float) -> float | None:
    """Bisection: find revenue_growth (Y1) that makes implied_price == market_price."""
    lo, hi = 0.01, 0.50
    if _dcf_value_from_assumptions({**assumptions, "revenue_growth": hi}) < market_price:
        return None
    if _dcf_value_from_assumptions({**assumptions, "revenue_growth": lo}) > market_price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        trial = {**assumptions, "revenue_growth": mid}
        price = _dcf_value_from_assumptions(trial)
        if abs(price - market_price) < 1.0:
            return round(mid, 4)
        if price < market_price:
            lo = mid
        else:
            hi = mid
    result = round((lo + hi) / 2.0, 4)
    return result if 0.005 <= result <= 0.50 else None


def compute_implied_margin(assumptions: dict[str, float], market_price: float) -> float | None:
    """Bisection: find fcff_margin (Y1) that makes implied_price == market_price."""
    lo, hi = 0.01, 0.60
    if _dcf_value_from_assumptions({**assumptions, "fcff_margin": hi}) < market_price:
        return None
    if _dcf_value_from_assumptions({**assumptions, "fcff_margin": lo}) > market_price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        trial = {**assumptions, "fcff_margin": mid}
        price = _dcf_value_from_assumptions(trial)
        if abs(price - market_price) < 1.0:
            return round(mid, 4)
        if price < market_price:
            lo = mid
        else:
            hi = mid
    result = round((lo + hi) / 2.0, 4)
    return result if 0.005 <= result <= 0.60 else None


def classify_implied_signal(
    implied_wacc: float | None,
    risk_free_rate: float,
) -> dict[str, Any]:
    """Classify the plausibility of a market-implied discount rate.

    Returns a dict with ``label``, ``spread_bps``, and ``narrative``.
    A market-implied WACC that prices equity at less than ~150bps over the
    risk-free rate is economically implausible for any equity claim, and
    typically reflects terminal-value dominance, solver edge, or unmodeled
    structural premium — not a literal market discount rate.
    """
    if not isinstance(implied_wacc, (int, float)) or implied_wacc <= 0:
        return {
            "label": "unavailable",
            "spread_bps": None,
            "narrative": "Implied discount rate unavailable.",
        }
    spread = float(implied_wacc) - float(risk_free_rate)
    spread_bps = int(round(spread * 10_000))
    if spread < 0.015:
        label = "economically_implausible"
        narrative = (
            f"Implied WACC of {implied_wacc:.2%} sits only {spread_bps:+d}bps over the risk-free rate, "
            "which is economically implausible for an equity claim. This typically reflects "
            "terminal-value dominance, reverse-solver instability, or a structural premium the DCF "
            "does not model — not a literal market discount rate."
        )
    elif spread < 0.030:
        label = "aggressive"
        narrative = (
            f"Implied WACC of {implied_wacc:.2%} ({spread_bps:+d}bps over Rf) is aggressive for equity. "
            "Interpret as a signal that the market embeds either a quality/duration premium or growth "
            "expectations beyond the modeled assumptions, rather than a literal discount-rate consensus."
        )
    elif spread < 0.060:
        label = "reasonable"
        narrative = (
            f"Implied WACC of {implied_wacc:.2%} ({spread_bps:+d}bps over Rf) sits in a defensible range "
            "for equity claims; comparison to model WACC is meaningful."
        )
    else:
        label = "conservative"
        narrative = (
            f"Implied WACC of {implied_wacc:.2%} ({spread_bps:+d}bps over Rf) is conservative — the market "
            "may be pricing significant cyclical, leverage, or execution risk above the model."
        )
    return {"label": label, "spread_bps": spread_bps, "narrative": narrative}


def wacc_gap_is_binding(
    wacc_sanity: dict[str, Any],
    *,
    implied_share_price: float | None = None,
    spot_price: float | None = None,
    gap_bps_threshold: int = 150,
) -> bool:
    """True when discount-rate gap likely drives the model-vs-spot spread.

    When binding, implied growth/margin at fixed WACC are misleading — the
    price is reconciled primarily via WACC (or non-modeled premium), not via
    absurd growth/margin levers.
    """
    if wacc_sanity.get("solver_status") != "ok":
        return False
    gap_bps = abs(int(wacc_sanity.get("gap_bps") or 0))
    if gap_bps < gap_bps_threshold:
        return False
    if (
        isinstance(implied_share_price, (int, float))
        and isinstance(spot_price, (int, float))
        and spot_price > 0
        and implied_share_price > 0
    ):
        return implied_share_price / spot_price < 0.85
    return gap_bps >= 250


def compute_market_signals_node(state: dict) -> dict:
    """Compute all three market-implied signals: WACC, growth, margin."""
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("compute_market_signals", "start", parent_step_id)

    assumptions = state.get("assumptions") or {}
    market_price = float((state.get("market_snapshot") or {}).get("price", 0))
    ticker = state.get("ticker", "?")

    # Implied WACC (existing logic + solver-failure flag)
    model_wacc = float(assumptions.get("wacc", 0.10))
    wacc_sanity = {
        "method": "capm_vs_implied",
        "capm_wacc": model_wacc,
        "implied_wacc": None,
        "gap_bps": None,
        "direction": "neutral",
        "flag": "solver_failed",
        "solver_status": "no_input",
        "interpretation": "Unable to compute implied WACC (missing market or projection inputs).",
    }
    try:
        from .wacc import solve_implied_wacc
        projected_fcff = state.get("projected_fcff") or []
        terminal_growth = float(assumptions.get("terminal_growth", 0.025))
        # Units: market_price is $/share, shares_outstanding is in MILLIONS,
        # net_debt is in MILLIONS. price × shares_M = $-millions directly.
        # Do NOT divide by 1_000_000 again — that was a bug giving ev 1e6× too small,
        # causing the solver to fail "no_convergence" on otherwise valid inputs.
        implied_ev_M = (
            (float(market_price) * float(assumptions.get("shares_outstanding", 1)))
            + float(assumptions.get("net_debt", 0))
        )

        if market_price <= 0 or implied_ev_M <= 0 or not projected_fcff:
            wacc_sanity["solver_status"] = "no_input"
            wacc_sanity["interpretation"] = (
                f"Solver skipped: market_price={market_price}, implied_ev_M={implied_ev_M:.0f}, "
                f"projection_rows={len(projected_fcff)}"
            )
        else:
            implied_wacc = solve_implied_wacc(
                projected_fcff,
                terminal_growth,
                implied_ev_M,
                buyback_yield=float(assumptions.get("buyback_yield", 0.0) or 0.0),
                net_debt_M=float(assumptions.get("net_debt", 0)),
            )
            if implied_wacc is None:
                wacc_sanity["solver_status"] = "no_convergence"
                wacc_sanity["flag"] = "solver_failed"
                wacc_sanity["interpretation"] = (
                    "Bisection solver did not converge within [1%, 50%]. "
                    "Likely: projected FCFF cannot support implied EV at any plausible discount rate."
                )
            elif implied_wacc < 0.01:
                # Suspicious result — solver returned but at boundary
                wacc_sanity["solver_status"] = "degenerate"
                wacc_sanity["implied_wacc"] = round(implied_wacc, 4)
                wacc_sanity["flag"] = "solver_failed"
                wacc_sanity["interpretation"] = (
                    f"Implied WACC at solver floor ({implied_wacc:.2%}) — not a credible signal."
                )
            else:
                capm = wacc_sanity["capm_wacc"]
                gap = round((capm - implied_wacc) * 10000)
                direction = "model_wacc_above_implied" if gap > 0 else "model_wacc_below_implied"
                flag = "severe" if abs(gap) > 200 else ("warning" if abs(gap) > 100 else "ok")
                rf = (state.get("wacc_components") or {}).get("risk_free_rate", 0.045)
                plausibility = classify_implied_signal(implied_wacc, float(rf))
                wacc_sanity = {
                    "method": "capm_vs_implied",
                    "capm_wacc": capm,
                    "implied_wacc": round(implied_wacc, 4),
                    "gap_bps": gap,
                    "direction": direction,
                    "flag": flag,
                    "solver_status": "ok",
                    "implied_plausibility": plausibility,
                    "interpretation": (
                        f"CAPM {capm:.1%} vs implied {implied_wacc:.1%} "
                        f"({'+' if gap > 0 else ''}{gap}bps {'⚠' if abs(gap) > 100 else ''})"
                    ),
                }
    except Exception as exc:
        logger.warning("Implied WACC computation failed for %s", ticker, exc_info=True)
        wacc_sanity["solver_status"] = "exception"
        wacc_sanity["flag"] = "solver_failed"
        wacc_sanity["interpretation"] = f"Solver raised exception: {exc!s}"

    # Implied growth / margin — skip when WACC gap is the binding constraint
    implied_share = (state.get("valuation") or {}).get("implied_share_price")
    wacc_binding = wacc_gap_is_binding(
        wacc_sanity,
        implied_share_price=implied_share,
        spot_price=market_price if market_price > 0 else None,
    )
    implied_growth: float | None = None
    implied_margin: float | None = None
    if market_price > 0 and not wacc_binding:
        try:
            implied_growth = compute_implied_growth(assumptions, market_price)
        except Exception:
            logger.warning("Implied growth computation failed for %s", ticker, exc_info=True)
        try:
            implied_margin = compute_implied_margin(assumptions, market_price)
        except Exception:
            logger.warning("Implied margin computation failed for %s", ticker, exc_info=True)

    # Consolidated signals — persist everything downstream consumers need in one place.
    _implied_wacc_val = wacc_sanity.get("implied_wacc") if wacc_sanity.get("solver_status") == "ok" else None
    _model_wacc_val = float(assumptions.get("wacc", 0))
    _wacc_gap_bps = (
        round((_model_wacc_val - float(_implied_wacc_val)) * 10000)
        if _implied_wacc_val is not None else None
    )
    _model_growth = float(assumptions.get("revenue_growth", 0))
    _growth_gap_pp = (
        round((float(implied_growth) - _model_growth) * 100, 2)
        if implied_growth is not None else None
    )
    _plausibility = (wacc_sanity.get("implied_plausibility") or {}).get("label")
    _plausibility_narrative = (wacc_sanity.get("implied_plausibility") or {}).get("narrative")
    market_signals_meta = {
        # Flags
        "wacc_binding": wacc_binding,
        "growth_margin_suppressed": wacc_binding,
        # Consolidated WACC signals
        "model_wacc": _model_wacc_val,
        "implied_wacc": _implied_wacc_val,
        "wacc_gap_bps": _wacc_gap_bps,
        # Consolidated growth signals
        "modeled_growth": _model_growth,
        "implied_growth": implied_growth,
        "growth_gap_pp": _growth_gap_pp,
        # Plausibility classification
        "plausibility_label": _plausibility,
        "plausibility_narrative": _plausibility_narrative,
        # Solver provenance
        "solver_status": wacc_sanity.get("solver_status"),
    }
    if wacc_binding:
        wacc_sanity = dict(wacc_sanity)
        wacc_sanity["binding_constraint"] = "wacc"
        wacc_sanity["interpretation"] = (
            f"{wacc_sanity.get('interpretation', '').rstrip('.')}. "
            "At fixed model WACC, growth/margin alone cannot reconcile spot — "
            "discount-rate or structural-premium gap is the primary read-through."
        )

    # Summary
    parts: list[str] = []
    model_wacc = assumptions.get("wacc", 0)
    model_growth = assumptions.get("revenue_growth", 0)
    model_margin = assumptions.get("fcff_margin", 0)
    iw = wacc_sanity.get("implied_wacc")
    if isinstance(iw, (int, float)) and iw > 0:
        parts.append(f"WACC: {model_wacc:.1%} vs DCF-implied {iw:.1%}")
    else:
        parts.append(f"WACC: {model_wacc:.1%} (solver: {wacc_sanity.get('solver_status', 'unknown')})")
    if implied_growth is not None:
        parts.append(f"growth: {model_growth:.1%} vs DCF-implied {implied_growth:.1%}")
    if implied_margin is not None:
        parts.append(f"margin: {model_margin:.1%} vs DCF-implied {implied_margin:.1%}")

    emit_step(
        "compute_market_signals", "complete", parent_step_id,
        {
            "summary_line": ", ".join(parts),
            "wacc_sanity": wacc_sanity,
            "implied_growth": implied_growth,
            "implied_margin": implied_margin,
            "market_signals_meta": market_signals_meta,
        },
    )
    return {
        "wacc_sanity": wacc_sanity,
        "implied_growth": implied_growth,
        "implied_margin": implied_margin,
        "market_signals_meta": market_signals_meta,
    }
