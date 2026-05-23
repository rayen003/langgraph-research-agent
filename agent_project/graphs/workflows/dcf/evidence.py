"""Evidence pack assembly — deterministic ingestion and normalization.

"What did we observe?" — a unified collection of normalized facts with
stable evidence IDs, provenance, source tiers, and as_of dates. No LLM
interpretation happens here.

Source tiering (highest first):
    filing > structured_api > document > news > generic_web
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .activity import emit_step
from .fundamentals import (
    _build_feature_vector,
    _fetch_fundamentals_fmp,
    _fetch_fundamentals_yfinance,
    _merge_dcf_extras,
)
from .priors import classify_profile
from .sec_filings import fetch_sec_filings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE_TIER_ORDER: dict[str, int] = {
    "filing": 1,
    "structured_api": 2,
    "document": 3,
    "news": 4,
    "generic_web": 5,
    "unknown": 99,
}

_EVIDENCE_BATCH_ID = f"evb_{int(time.time() * 1000)}"

# ---------------------------------------------------------------------------
# Evidence item builders
# ---------------------------------------------------------------------------


def _make_evidence_item(
    *,
    evidence_id: str,
    kind: str,
    source_tier: str,
    source: str,
    as_of: str,
    **fields: Any,
) -> dict[str, Any]:
    """Create a normalized evidence item dict."""
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "source_tier": source_tier,
        "source": source,
        "as_of": as_of,
        **fields,
    }


def _fundamentals_to_evidence(
    fundamentals: dict[str, dict[str, Any]],
    provider: str,
) -> list[dict[str, Any]]:
    """Convert canonical fundamental entries into evidence items."""
    items: list[dict[str, Any]] = []
    for field, meta in fundamentals.items():
        if field.startswith("__"):  # skip internal carriers
            continue
        value = meta.get("value")
        items.append(_make_evidence_item(
            evidence_id=f"ev_{provider}_{field}",
            kind="structured_fundamental",
            source_tier="structured_api",
            source=provider,
            as_of=str(meta.get("as_of", "")),
            field=field,
            value=value,
            raw_value=meta.get("raw_value"),
            raw_unit=meta.get("raw_unit"),
            confidence=meta.get("confidence"),
            evidence=meta.get("evidence"),
        ))
    return items


def _profile_to_evidence(
    profile: str,
    profile_meta: dict[str, Any],
    features: dict[str, Any],
) -> list[dict[str, Any]]:
    """Convert profile metadata and features into evidence items."""
    items: list[dict[str, Any]] = []

    # Profile classification
    items.append(_make_evidence_item(
        evidence_id=f"ev_profile_classification",
        kind="profile",
        source_tier="structured_api",
        source="derived",
        as_of=time.strftime("%Y-%m-%d"),
        profile=profile,
        sector=profile_meta.get("sector"),
        industry=profile_meta.get("industry"),
        market_cap_usd=profile_meta.get("market_cap_usd"),
        spot_price=profile_meta.get("spot_price"),
        company_name=profile_meta.get("company_name"),
    ))

    # Feature vector (beta, equity value, debt, etc.)
    for key in (
        "beta", "equity_value_usd", "total_debt_usd", "cash_usd",
        "interest_expense_usd", "net_debt_usd", "effective_tax_rate_hint",
    ):
        if key in features and features[key] is not None:
            items.append(_make_evidence_item(
                evidence_id=f"ev_feature_{key}",
                kind="market_data",
                source_tier="structured_api",
                source="fmp+yfinance",
                as_of=time.strftime("%Y-%m-%d"),
                field=key,
                value=features[key],
            ))

    return items


def _collect_raw_web_excerpts(
    ticker: str,
    max_results: int = 3,
) -> list[dict[str, Any]]:
    """Collect raw web search excerpts as evidence — no assumption parsing."""
    try:
        from web_search import search_exa  # noqa: PLC0415

        query = (
            f"{ticker} business outlook growth strategy risks "
            "competitive position market trends"
        )
        raw, _summary = search_exa(
            query,
            num_results=max_results,
            search_type="auto",
            max_characters=2000,
        )
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Evidence web excerpts failed ticker=%s error=%s", ticker, exc)
        return []

    if not isinstance(payload, dict):
        return []

    items: list[dict[str, Any]] = []
    for i, result in enumerate(payload.get("results", []) or []):
        title = result.get("title") or "untitled"
        url = result.get("url") or ""
        published = result.get("published_date") or ""
        highlights = result.get("highlights") or []
        excerpt = " ".join(str(h) for h in highlights[:3])
        if len(excerpt) > 1500:
            excerpt = excerpt[:1500] + "..."

        # Determine a rough source tier from the URL
        source_tier = "generic_web"
        if any(domain in url.lower() for domain in
               ["bloomberg", "reuters", "wsj.com", "ft.com", "cnbc.com",
                "sec.gov", "investors.", "ir.", "earnings"]):
            source_tier = "news"

        items.append(_make_evidence_item(
            evidence_id=f"ev_web_{i}_{_EVIDENCE_BATCH_ID}",
            kind="web_excerpt",
            source_tier=source_tier,
            source="exa",
            as_of=published or time.strftime("%Y-%m-%d"),
            title=title,
            url=url,
            text=excerpt,
            published_date=published,
        ))

    return items


def _collect_raw_document_excerpts(
    session_id: str,
    ticker: str,
) -> list[dict[str, Any]]:
    """Collect raw document excerpts as evidence — no assumption parsing."""
    if not session_id:
        return []

    try:
        from documents import hybrid_search, list_docs  # noqa: PLC0415

        ready_docs = [d for d in list_docs(session_id) if d.get("status") == "ready"]
        if not ready_docs:
            return []

        query = (
            f"{ticker} financial performance outlook growth risks revenue "
            "strategy market position competitive"
        )
        results = hybrid_search(query, session_id, n_results=5)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Evidence document excerpts failed session_id=%s error=%s",
            session_id, exc,
        )
        return []

    items: list[dict[str, Any]] = []
    for i, result in enumerate(results):
        meta = result.get("metadata") or {}
        filename = meta.get("filename", "uploaded document")
        page = meta.get("page", "?")
        text = str(result.get("text") or "")
        if len(text) > 2000:
            text = text[:2000] + "..."

        items.append(_make_evidence_item(
            evidence_id=f"ev_doc_{i}_{_EVIDENCE_BATCH_ID}",
            kind="document_excerpt",
            source_tier="document",
            source="uploaded_document",
            as_of=time.strftime("%Y-%m-%d"),
            filename=filename,
            page=page,
            text=text,
        ))

    return items


# ---------------------------------------------------------------------------
# Evidence pack assembler
# ---------------------------------------------------------------------------


def assemble_evidence(
    *,
    ticker: str,
    session_id: str,
    include_web: bool = True,
    include_documents: bool = True,
    include_sec: bool = True,
) -> dict[str, Any]:
    """Assemble a complete evidence pack for a ticker.

    Returns a dict with:
        - evidence_id: batch identifier
        - generated_at: epoch timestamp
        - items: list of evidence items, each with stable ID + provenance
        - summary: counts by source_tier
    """
    items: list[dict[str, Any]] = []

    # ── Tier 1: SEC filings ────────────────────────────────────────────────
    if include_sec:
        try:
            sec_items = fetch_sec_filings(ticker, max_filings=2)
            items.extend(sec_items)
            if sec_items:
                logger.info(
                    "Evidence: SEC filings collected ticker=%s count=%d",
                    ticker, len(sec_items),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evidence: SEC filings failed ticker=%s error=%s", ticker, exc)

    # ── Tier 2: Structured fundamentals (FMP + yfinance) ────────────────────
    fmp_raw = _fetch_fundamentals_fmp(ticker)
    yf_raw = _fetch_fundamentals_yfinance(ticker)

    extras_fmp = dict(fmp_raw.pop("__dcf_extras__", {}))
    extras_yf = dict(yf_raw.pop("__dcf_extras__", {}))
    dcf_extras = _merge_dcf_extras(extras_fmp, extras_yf)
    profile_meta = dict(fmp_raw.pop("__profile_meta__", {}))

    fundamentals = dict(fmp_raw)
    for field, meta in yf_raw.items():
        fundamentals.setdefault(field, meta)

    provider = "fmp"
    if not fmp_raw and yf_raw:
        provider = "yfinance"
    elif fmp_raw and yf_raw:
        provider = "fmp+fallback:yfinance"
    elif not fundamentals:
        provider = "none"

    if fundamentals:
        items.extend(_fundamentals_to_evidence(fundamentals, provider))

    # ── Profile + features ─────────────────────────────────────────────────
    profile = classify_profile(
        sector=profile_meta.get("sector"),
        market_cap_usd=profile_meta.get("market_cap_usd"),
    )
    features = _build_feature_vector(
        ticker=ticker,
        profile_bucket=profile,
        profile_meta=profile_meta,
        fundamentals=fundamentals,
        dcf_extras=dcf_extras,
    )
    items.extend(_profile_to_evidence(profile, profile_meta, features))

    # ── Tier 3: Document excerpts ──────────────────────────────────────────
    if include_documents and session_id:
        try:
            doc_items = _collect_raw_document_excerpts(session_id, ticker)
            items.extend(doc_items)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evidence: document excerpts failed error=%s", exc)

    # ── Tier 4: Web excerpts ───────────────────────────────────────────────
    if include_web:
        try:
            web_items = _collect_raw_web_excerpts(ticker)
            items.extend(web_items)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evidence: web excerpts failed error=%s", exc)

    # ── Build pack ─────────────────────────────────────────────────────────
    tier_counts: dict[str, int] = {}
    for item in items:
        tier = item.get("source_tier", "unknown")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    pack = {
        "evidence_id": _EVIDENCE_BATCH_ID,
        "generated_at": time.time(),
        "ticker": ticker.upper(),
        "total_items": len(items),
        "tier_summary": tier_counts,
        "profile": profile,
        "profile_meta": profile_meta,
        "features": features,
        "fundamentals": fundamentals,
        "items": items,
    }

    logger.info(
        "Evidence pack assembled ticker=%s items=%d tiers=%s",
        ticker, len(items), json.dumps(tier_counts),
    )
    return pack


# ---------------------------------------------------------------------------
# assemble_evidence node (new — feeds hydrate's role + adds SEC/web/doc)
# ---------------------------------------------------------------------------


def _fmt_tier_summary(tiers: dict[str, int]) -> str:
    """Compact tier summary: 'filing:6, api:13, web:3'"""
    parts = []
    for tier in ("filing", "structured_api", "document", "news", "generic_web"):
        if tier in tiers:
            short = tier.replace("structured_", "").replace("generic_", "")
            parts.append(f"{short}:{tiers[tier]}")
    return ", ".join(parts)


def assemble_evidence_node(state: dict) -> dict:
    """Assemble the evidence pack: observations before interpretation.

    Subsumes the role of ``hydrate_fundamentals_node`` and adds:
        - SEC filing excerpts (source_tier=filing)
        - Raw web excerpts (source_tier=news/generic_web)
        - Raw document excerpts (source_tier=document)

    Produces ``evidence_pack`` on the DCF state alongside the legacy
    ``fundamentals``, ``profile``, and ``features`` fields so downstream
    nodes (build_assumptions, wacc resolution) continue to work unchanged.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    ticker = state["ticker"]
    session_id = state.get("session_id") or ""
    allow_external = bool(state.get("allow_external_assumptions", True))

    emit_step("assemble_evidence", "start", parent_step_id, {"ticker": ticker})

    pack = assemble_evidence(
        ticker=ticker,
        session_id=session_id,
        include_web=allow_external,
        include_documents=bool(session_id),
        include_sec=True,
    )

    status = "complete" if pack["total_items"] > 0 else "fallback"

    # ── KG write-back: persist filing + news items as Layer 1 anchored facts ──
    # Anchored types use infinite TTL — once written, never overwritten.
    # Re-runs are ADDITIVE: new items grow the corpus; existing items stay.
    try:
        import hashlib
        from kg import get_cache  # noqa: PLC0415
        cache = get_cache()
        # Ensure company anchor exists (cheap, idempotent)
        cache.put(
            ticker=ticker, node_type="company", field="anchor",
            value={"ticker": ticker},
            source="agent_inferred", confidence=1.0, session_id=session_id,
        )
        written_filing = 0
        written_news = 0
        for item in pack.get("items", []):
            kind = item.get("kind", "")
            text = (item.get("text") or "")[:8000]
            if not text:
                continue
            if kind == "filing_excerpt":
                meta_ = item.get("metadata") or {}
                filing_type = meta_.get("filing_type") or "filing"
                section = meta_.get("section") or "body"
                as_of = item.get("as_of") or meta_.get("as_of") or "unknown"
                # Deterministic field: filing_type::as_of::section
                field_key = f"{filing_type}::{as_of}::{section}"
                cache.put(
                    ticker=ticker,
                    node_type="filing",
                    field=field_key,
                    value={
                        "filing_type": filing_type,
                        "section": section,
                        "as_of": as_of,
                        "text": text,
                        "url": item.get("url", ""),
                        "evidence_id": item.get("evidence_id", ""),
                    },
                    source="sec_edgar",
                    confidence=0.95,
                    session_id=session_id,
                )
                written_filing += 1
            elif kind in ("web_excerpt",):
                url = item.get("url", "")
                title = item.get("title") or ""
                published = item.get("as_of") or ""
                # Deterministic field: hash of URL (unique per article)
                url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12] if url else hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
                field_key = f"{published or 'undated'}::{url_hash}"
                cache.put(
                    ticker=ticker,
                    node_type="news_item",
                    field=field_key,
                    value={
                        "title": title,
                        "url": url,
                        "published_at": published,
                        "text": text,
                        "source": item.get("source", "web"),
                        "evidence_id": item.get("evidence_id", ""),
                    },
                    source="web_search",
                    confidence=0.7,
                    session_id=session_id,
                )
                written_news += 1
        logger.info(
            "DCF evidence KG write-back ticker=%s filings=%d news=%d",
            ticker, written_filing, written_news,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "DCF evidence KG write-back failed ticker=%s err=%s", ticker, exc,
        )

    # Build lightweight item previews for the UI detail panel
    item_previews = []
    for item in pack.get("items", []):
        preview = {
            "evidence_id": item.get("evidence_id", "?"),
            "kind": item.get("kind", "?"),
            "source_tier": item.get("source_tier", "?"),
            "source": item.get("source", "?"),
            "title": (item.get("title") or "")[:120],
            "url": item.get("url", ""),
            "text": (item.get("text") or "")[:300],
            "as_of": item.get("as_of", ""),
        }
        meta = item.get("metadata")
        if isinstance(meta, dict):
            preview["filing_type"] = meta.get("filing_type", "")
            preview["section"] = meta.get("section", "")
        item_previews.append(preview)

    emit_step(
        "assemble_evidence",
        status,
        parent_step_id,
        {
            "total_items": pack["total_items"],
            "tier_summary": pack["tier_summary"],
            "profile": pack["profile"],
            "features_summary": sorted(pack["features"].keys()),
            "items": item_previews,
            "summary_line": f"{pack['total_items']} items ({_fmt_tier_summary(pack['tier_summary'])}), profile={pack['profile']}",
        },
    )

    return {
        "evidence_pack": pack,
        "fundamentals": pack["fundamentals"],
        "profile": pack["profile"],
        "profile_meta": pack["profile_meta"],
        "features": pack["features"],
        "wacc_components": {},
    }
