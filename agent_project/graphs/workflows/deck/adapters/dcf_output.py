"""DCF output adapter — converts a completed DCF run into normalized blocks.

Loads the source from one of three places (priority order):
  1. ``payload_inline`` — full dcf_output dict passed directly (in-session use)
  2. ``payload_path`` — disk path to ``dcf_output.json``
  3. ``run_id`` — KG dcf_run node lookup (cross-session use; placeholder for now)

Produces blocks for: executive metric, thesis narrative, scenario table,
sensitivity chart, key risks, assumptions table, valuation breakdown.
All blocks carry stable IDs derived from run + content signature.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..state import DcfOutputSource, NormalizedBlock
from .base import SourceAdapter, make_block_id
from .dcf_expectations_blocks import build_expectations_blocks

logger = logging.getLogger(__name__)


class DcfOutputAdapter(SourceAdapter):
    source_type = "dcf_output"

    def normalize(self, source: DcfOutputSource, *, session_id: str = "") -> list[NormalizedBlock]:  # type: ignore[override]
        payload = self._load(source)
        if not payload:
            logger.warning("DcfOutputAdapter: no payload loaded for source=%s", source)
            return []

        ticker = str(payload.get("ticker") or "UNKNOWN")
        source_ref = source.run_id or source.payload_path or f"inline::{ticker}"

        blocks: list[NormalizedBlock] = []
        idx = 0

        # ── 1. Executive metric — implied price vs spot ────────────────────
        valuation = payload.get("valuation") or {}
        market_snapshot = payload.get("market_snapshot") or {}
        implied = valuation.get("implied_share_price")
        spot = market_snapshot.get("price") or valuation.get("current_price")
        if implied is not None and spot:
            try:
                upside_pct = ((float(implied) / float(spot)) - 1.0) * 100.0
            except (TypeError, ZeroDivisionError, ValueError):
                upside_pct = 0.0
            sig = f"metric:implied:{implied}:{spot}"
            blocks.append(NormalizedBlock(
                block_id=make_block_id(source_type=self.source_type, source_ref=source_ref, idx=idx, content_signature=sig),
                kind="metric",
                title=f"{ticker} — Implied vs Spot",
                content={
                    "ticker": ticker,
                    "implied_price": implied,
                    "spot_price": spot,
                    "upside_pct": round(upside_pct, 1),
                    "confidence_label": payload.get("confidence_label"),
                    "model_validity": payload.get("model_validity"),
                },
                source_type=self.source_type,
                source_ref=source_ref,
                suggested_slide_layouts=["metric_callout", "executive_summary"],
            ))
            idx += 1

        # ── 2. Thesis (bull / bear / narrative) ────────────────────────────
        thesis = payload.get("thesis") or {}
        if thesis.get("bull_thesis") or thesis.get("bear_thesis") or thesis.get("narrative"):
            sig = f"thesis:{thesis.get('narrative', '')[:60]}"
            blocks.append(NormalizedBlock(
                block_id=make_block_id(source_type=self.source_type, source_ref=source_ref, idx=idx, content_signature=sig),
                kind="narrative",
                title=f"{ticker} — Investment Thesis",
                content={
                    "bull": thesis.get("bull_thesis", ""),
                    "bear": thesis.get("bear_thesis", ""),
                    "narrative": thesis.get("narrative", ""),
                    "key_drivers": thesis.get("key_drivers", []),
                },
                source_type=self.source_type,
                source_ref=source_ref,
                suggested_slide_layouts=["thesis", "narrative"],
            ))
            idx += 1

        # ── 3. Scenario table ──────────────────────────────────────────────
        scenarios = payload.get("scenario_results") or payload.get("scenarios") or []
        # Fast-path DCF runs (`assumption_review_mode=False` + overrides) skip
        # scenario_generator / scenario_runner entirely, so both keys are empty.
        # Synthesize bear/base/bull from the sensitivity grid as a fallback so
        # the deck always has a scenarios slide with real numbers.
        if not scenarios:
            sens_table = payload.get("sensitivity_table") or []
            if isinstance(sens_table, list) and len(sens_table) >= 3:
                sorted_rows = sorted(
                    [r for r in sens_table if r.get("implied_share_price") is not None],
                    key=lambda r: r["implied_share_price"],
                )
                if len(sorted_rows) >= 3:
                    bear_row = sorted_rows[0]
                    bull_row = sorted_rows[-1]
                    base_price = valuation.get("implied_share_price")
                    base_assumptions = payload.get("assumptions") or {}
                    scenarios = [
                        {
                            "name": "bear",
                            "probability": 0.25,
                            "valuation": {"implied_share_price": bear_row.get("implied_share_price")},
                            "assumptions": {
                                "wacc": bear_row.get("wacc"),
                                "terminal_growth": bear_row.get("terminal_growth"),
                            },
                            "rationale": "Low end of WACC × terminal-growth sensitivity grid.",
                        },
                        {
                            "name": "base",
                            "probability": 0.50,
                            "valuation": {"implied_share_price": base_price},
                            "assumptions": base_assumptions,
                            "rationale": "Base-case assumptions.",
                        },
                        {
                            "name": "bull",
                            "probability": 0.25,
                            "valuation": {"implied_share_price": bull_row.get("implied_share_price")},
                            "assumptions": {
                                "wacc": bull_row.get("wacc"),
                                "terminal_growth": bull_row.get("terminal_growth"),
                            },
                            "rationale": "High end of WACC × terminal-growth sensitivity grid.",
                        },
                    ]
                    logger.info(
                        "DcfOutputAdapter ticker=%s synthesized %d scenarios from sensitivity_table",
                        ticker, len(scenarios),
                    )
        if scenarios:
            sig = f"scenarios:{len(scenarios)}"
            rows = []
            for sc in scenarios:
                v = sc.get("valuation") or {}
                rows.append({
                    "name": sc.get("name", ""),
                    "probability": sc.get("probability"),
                    "implied_price": v.get("implied_share_price"),
                    "assumptions": sc.get("assumptions", {}),
                    "rationale": sc.get("rationale", ""),
                })
            blocks.append(NormalizedBlock(
                block_id=make_block_id(source_type=self.source_type, source_ref=source_ref, idx=idx, content_signature=sig),
                kind="table",
                title=f"{ticker} — Scenarios",
                content={
                    "rows": rows,
                    "expected_value": valuation.get("implied_share_price"),
                    "range_low": valuation.get("range_low"),
                    "range_high": valuation.get("range_high"),
                },
                source_type=self.source_type,
                source_ref=source_ref,
                suggested_slide_layouts=["scenario_table"],
            ))
            idx += 1

        # ── 4. Sensitivity chart (if artifact path embedded) ───────────────
        sens_path = payload.get("sensitivity_chart")
        if not sens_path:
            artifacts = payload.get("artifacts") or {}
            if isinstance(artifacts, dict):
                sens_path = artifacts.get("sensitivity_chart")
        # Disk discovery fallback — DCF workflow doesn't always persist the
        # chart path. Glob the run's artifacts dir for sensitivity_*.png.
        if not sens_path and source.payload_path:
            artifacts_dir = Path(source.payload_path).parent / "artifacts"
            if artifacts_dir.exists():
                candidates = sorted(artifacts_dir.glob("sensitivity_*.png"))
                if candidates:
                    sens_path = str(candidates[0])
                    logger.info(
                        "DcfOutputAdapter ticker=%s discovered sensitivity chart on disk: %s",
                        ticker, sens_path,
                    )
        if sens_path and Path(sens_path).exists():
            blocks.append(NormalizedBlock(
                block_id=make_block_id(source_type=self.source_type, source_ref=source_ref, idx=idx, content_signature=sens_path),
                kind="chart",
                title=f"{ticker} — Sensitivity (WACC × TGR)",
                content={"path": sens_path, "caption": "Implied price across WACC and terminal-growth grid."},
                source_type=self.source_type,
                source_ref=source_ref,
                suggested_slide_layouts=["chart_caption"],
            ))
            idx += 1

        # ── 5. Key risks (from company_state.key_risks) ────────────────────
        company_state = payload.get("company_state") or {}
        risks = company_state.get("key_risks") or []
        if risks:
            sig = f"risks:{len(risks)}"
            blocks.append(NormalizedBlock(
                block_id=make_block_id(source_type=self.source_type, source_ref=source_ref, idx=idx, content_signature=sig),
                kind="list",
                title=f"{ticker} — Key Risks",
                content={"items": [str(r) for r in risks][:6]},
                source_type=self.source_type,
                source_ref=source_ref,
                suggested_slide_layouts=["risk_summary", "bullets"],
            ))
            idx += 1

        # ── 6. Assumptions table ───────────────────────────────────────────
        assumptions = payload.get("assumptions") or {}
        if assumptions:
            sig = f"assumptions:{','.join(sorted(assumptions.keys()))}"
            blocks.append(NormalizedBlock(
                block_id=make_block_id(source_type=self.source_type, source_ref=source_ref, idx=idx, content_signature=sig),
                kind="table",
                title=f"{ticker} — Base-Case Assumptions",
                content={
                    "assumptions": assumptions,
                    "provenance": payload.get("assumption_provenance") or {},
                },
                source_type=self.source_type,
                source_ref=source_ref,
                evidence_refs=_collect_assumption_evidence_refs(payload.get("assumption_provenance") or {}),
                suggested_slide_layouts=["scenario_table", "bullets"],
            ))
            idx += 1

        # ── 7. Valuation breakdown (EV → equity → per share) ───────────────
        if valuation.get("enterprise_value") is not None:
            sig = f"val:{valuation.get('enterprise_value')}"
            blocks.append(NormalizedBlock(
                block_id=make_block_id(source_type=self.source_type, source_ref=source_ref, idx=idx, content_signature=sig),
                kind="table",
                title=f"{ticker} — Valuation Breakdown",
                content={
                    "enterprise_value": valuation.get("enterprise_value"),
                    "pv_cash_flows": valuation.get("pv_cash_flows"),
                    "terminal_pv": valuation.get("terminal_pv"),
                    "net_debt": valuation.get("net_debt") or (payload.get("fundamentals") or {}).get("net_debt"),
                    "equity_value": valuation.get("equity_value"),
                    "shares_outstanding": assumptions.get("shares_outstanding"),
                    "implied_share_price": valuation.get("implied_share_price"),
                },
                source_type=self.source_type,
                source_ref=source_ref,
                suggested_slide_layouts=["scenario_table"],
            ))
            idx += 1

        # ── 8. Expectations-first blocks ───────────────────────────────────
        # Prepend so they appear before the legacy descriptive blocks. The
        # outline template prefers expectations blocks for the institutional
        # deck shape; legacy blocks remain available as fallback content for
        # any slide the new shape doesn't cover.
        expectations = build_expectations_blocks(payload, source_ref, ticker)
        blocks = expectations + blocks

        logger.info(
            "DcfOutputAdapter ticker=%s source_ref=%s produced %d blocks "
            "(%d expectations + %d legacy)",
            ticker, source_ref, len(blocks), len(expectations), len(blocks) - len(expectations),
        )
        return blocks

    # ── Loader -----------------------------------------------------------------
    def collect_evidence(self, source: DcfOutputSource, *, session_id: str = "") -> dict[str, dict[str, Any]]:
        """Expose the DCF run's evidence corpus keyed by ``evidence_id``.

        Optional adapter hook (see base.SourceAdapter docstring). The references
        slide resolves block ``evidence_refs`` against this index to render
        human-readable citations instead of raw KG IDs.
        """
        payload = self._load(source)
        if not payload:
            return {}
        items = payload.get("_evidence_items") or []
        index: dict[str, dict[str, Any]] = {}
        for it in items:
            if isinstance(it, dict) and it.get("evidence_id"):
                # First write wins — keep the earliest (most authoritative) item.
                index.setdefault(str(it["evidence_id"]), it)
        return index

    def _load(self, source: DcfOutputSource) -> dict[str, Any] | None:
        if source.payload_inline:
            return source.payload_inline
        if source.payload_path:
            try:
                return json.loads(Path(source.payload_path).read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("DcfOutputAdapter: failed to read %s: %s", source.payload_path, exc)
                return None
        if source.run_id:
            # KG lookup — placeholder.  Wired once kg/cache.py reconstruction
            # for dcf_run + run_assumption + run_output is in place.
            logger.warning("DcfOutputAdapter: KG run_id lookup not yet implemented (run_id=%s)", source.run_id)
            return None
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_assumption_evidence_refs(provenance: dict[str, Any]) -> list[str]:
    """Flatten evidence_refs across all assumption provenance entries."""
    refs: list[str] = []
    seen: set[str] = set()
    for prov in provenance.values():
        if not isinstance(prov, dict):
            continue
        for r in (prov.get("evidence_refs") or []):
            if isinstance(r, str) and r not in seen:
                seen.add(r)
                refs.append(r)
    return refs


__all__ = ["DcfOutputAdapter"]
