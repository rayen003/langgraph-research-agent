"""Lifecycle nodes — workflow entry and KG cache probe."""

from __future__ import annotations

import logging
from typing import Any

from .activity import emit_step, emit_workflow_terminal
from .state import DCFState

logger = logging.getLogger(__name__)


def normalize_input_node(state: DCFState) -> dict:
    """Validate ticker and horizon, emit workflow-started span."""
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_workflow_terminal(
        parent_step_id=parent_step_id,
        status="started",
        payload={"ticker": str(state.get("ticker") or "").upper()},
    )
    emit_step("normalize_input", "start", parent_step_id)
    ticker = str(state.get("ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("ticker is required for DCF workflow.")
    horizon = int(state.get("horizon_years") or 5)
    horizon = min(max(horizon, 3), 10)
    logger.info(
        "DCF normalize_input ticker=%s horizon_years=%d", ticker, horizon,
    )
    emit_step(
        "normalize_input", "complete", parent_step_id,
        {"ticker": ticker, "horizon_years": horizon, "summary_line": f"{ticker} {horizon}yr horizon"},
    )
    return {"ticker": ticker, "horizon_years": horizon}


def cache_check_node(state: DCFState) -> dict:
    """Probe the Knowledge Graph cache for cached evidence/synthesis/thesis."""
    from kg import get_cache  # noqa: PLC0415

    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    ticker = state["ticker"]
    session_id = state.get("session_id") or ""
    cache = get_cache()

    if session_id:
        try:
            cache.load_session(session_id)
        except Exception:  # noqa: BLE001
            logger.warning("KG cache load_session failed session_id=%s", session_id)
    try:
        cache.load_ticker(ticker)
    except Exception:  # noqa: BLE001
        logger.warning("KG cache load_ticker failed ticker=%s", ticker)

    emit_step("cache_check", "start", parent_step_id)

    probes = [
        ("market_metric_fund", "base_revenue",       "skip_fmp_fundamentals", "kg_fundamentals_hint"),
        ("market_metric_price", "price",             "skip_price_fetch",      None),
        ("company_synthesis",  "full",               "skip_semantic_synthesis", "company_state"),
        ("company_lifecycle",  "signals",            None,                    "kg_lifecycle_hint"),
        ("thesis",             "full",               "skip_formulate_thesis", "thesis"),
    ]

    flags: dict[str, bool] = {}
    results: list[dict[str, Any]] = []
    inject: dict[str, Any] = {}

    for node_type, field, flag_name, inject_key in probes:
        node = cache.get(ticker, node_type, field)
        node_id = f"{ticker}::{node_type}::{field}"

        if node:
            age = max(0.0, __import__("time").time() - float(node.get("updated_at", 0)))
            stale = False
            if node_type in {"thesis", "company_synthesis"}:
                current_hash = cache.evidence_hash(ticker)
                stored_hash = node.get("input_hash")
                if stored_hash and stored_hash != current_hash:
                    stale = True

            if stale:
                status = "stale"
                action = f"will_refresh_{node_type}"
            else:
                status = "hit"
                action = flag_name or f"injected_{node_type}"
                if flag_name:
                    flags[flag_name] = True
                if inject_key:
                    value = node.get("value")
                    if inject_key == "kg_fundamentals_hint":
                        fund: dict[str, float] = {}
                        for f_name in ("base_revenue", "shares_outstanding", "net_debt"):
                            n2 = cache.get(ticker, "market_metric_fund", f_name)
                            if n2:
                                try:
                                    fund[f_name] = float(n2.get("value"))  # type: ignore[arg-type]
                                except (TypeError, ValueError):
                                    continue
                        if len(fund) == 3:
                            inject[inject_key] = fund
                        else:
                            if flag_name:
                                flags.pop(flag_name, None)
                            status = "miss"
                            action = "partial_kg_data"
                    else:
                        inject[inject_key] = value

            results.append({
                "node_id": node_id,
                "node_type": node_type,
                "field": field,
                "status": status,
                "age_s": round(age, 1),
                "action": action,
                "source": node.get("source"),
                "confidence": node.get("confidence"),
            })

            cache.record_traversal(
                run_id=parent_step_id,
                node_id=node_id,
                status=status,
                action=action,
                age_s=age,
            )
        else:
            results.append({
                "node_id": node_id,
                "node_type": node_type,
                "field": field,
                "status": "miss",
                "age_s": None,
                "action": "will_fetch",
            })
            cache.record_traversal(
                run_id=parent_step_id,
                node_id=node_id,
                status="miss",
                action="will_fetch",
                age_s=None,
            )

    # ── Layer 1 — Load anchored corpus (filings + news) ─────────────────────
    # Anchored facts are ADDITIVE — load all, never invalidate. Surface counts
    # so downstream nodes know corpus size and can decide whether to fetch more
    # recent news (additive) or re-use existing.
    anchored_corpus = cache.get_anchored_corpus(ticker)
    filing_count = sum(1 for n in anchored_corpus if n.get("node_type") == "filing")
    news_count = sum(1 for n in anchored_corpus if n.get("node_type") == "news_item")
    # Newest news timestamp (helps agent decide if "recent" fetch needed)
    newest_news_ts = max(
        (float(n.get("created_at", 0)) for n in anchored_corpus if n.get("node_type") == "news_item"),
        default=0.0,
    )

    hit_count = sum(1 for r in results if r["status"] == "hit")
    miss_count = len(results) - hit_count
    corpus_note = f" · corpus: {filing_count} filings, {news_count} news" if (filing_count + news_count) else ""
    summary = f"{hit_count}/{len(results)} cached{corpus_note}"

    logger.info(
        "DCF cache_check ticker=%s hits=%d misses=%d flags=%s filings=%d news=%d",
        ticker, hit_count, miss_count, list(flags.keys()), filing_count, news_count,
    )

    emit_step(
        "cache_check", "complete", parent_step_id,
        {
            "summary_line": summary,
            "kg_cache_results": results,
            "kg_cache_flags": flags,
            "hit_count": hit_count,
            "miss_count": miss_count,
            "anchored_filing_count": filing_count,
            "anchored_news_count": news_count,
            "anchored_newest_news_ts": newest_news_ts,
        },
    )

    return {
        "kg_cache_flags": flags,
        "kg_fundamentals_hint": inject.get("kg_fundamentals_hint", {}),
        "kg_cache_results": results,
        "kg_anchored_corpus_meta": {
            "filing_count": filing_count,
            "news_count": news_count,
            "newest_news_ts": newest_news_ts,
        },
        **{k: v for k, v in inject.items() if k != "kg_fundamentals_hint"},
    }


def route_after_cache_check(state: DCFState) -> str:
    """Skip semantic_synthesis if its output is cached and fresh.

    Conservatively keeps assemble_evidence + formulate_thesis in the path
    even on cache hit — they're cheap to short-circuit internally and
    valuable for activity trace continuity.
    """
    flags = state.get("kg_cache_flags") or {}
    if flags.get("skip_semantic_synthesis"):
        return "formulate_thesis"
    return "assemble_evidence"
