"""Numbered source registry for DCF report citations."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urlparse

_EVIDENCE_ID_PATTERN = re.compile(r"\bev_[\w+\-:.]+\b")
_NEWS_SOURCE_TIERS = frozenset({"news", "generic_web"})
_SEC_COMPANY_URL = "https://www.sec.gov/cgi-bin/browse-edgar?CIK={cik}&action=getcompany&count=40&owner=include"
_SEC_TICKER_URL = "https://www.sec.gov/cgi-bin/browse-edgar?CIK={ticker}"


def humanize_evidence_item(item: dict[str, Any]) -> str:
    """One-line label for an evidence pack item."""
    if not item:
        return "Unknown source"
    kind = item.get("kind", "?")
    if kind == "filing_excerpt":
        return (
            f"{item.get('filing_type', 'SEC filing')} "
            f"{item.get('section', '')} ({item.get('as_of', '')})"
        ).strip()
    if kind == "structured_fundamental":
        return f"{item.get('source', 'API')}: {item.get('field', '?')}={item.get('value', '?')}"
    if kind == "web_excerpt":
        return f"web: {str(item.get('title', '?'))[:60]}"
    if kind == "document_excerpt":
        return f"doc: {item.get('filename', '?')} p.{item.get('page', '?')}"
    if kind == "market_data":
        return f"market: {item.get('field', '?')}={item.get('value', '?')}"
    if kind == "profile":
        company = item.get("company_name") or "Company profile"
        sector = item.get("sector") or "?"
        industry = item.get("industry") or "?"
        return f"{company}: {sector} / {industry}"
    eid = item.get("evidence_id", "")
    return f"{kind}: {eid[:48]}" if eid else kind


def humanize_evidence_refs(
    refs: list[str],
    evidence_items: list[dict[str, Any]],
) -> list[str]:
    """Map opaque evidence_ids to human-readable labels."""
    by_id: dict[str, dict[str, Any]] = {
        item.get("evidence_id", ""): item for item in evidence_items
    }
    result: list[str] = []
    for ref in refs:
        item = by_id.get(ref)
        if not item:
            result.append(ref)
            continue
        result.append(humanize_evidence_item(item))
    return result


def evidence_item_url(
    item: dict[str, Any] | None,
    evidence_id: str = "",
    ticker: str = "",
) -> str | None:
    """Best-effort URL for verifying a cited source."""
    if item:
        url = str(item.get("url") or "").strip()
        if url.startswith("http"):
            return url
        kind = item.get("kind")
        ticker = str(item.get("ticker") or ticker or "").strip().upper()
    else:
        kind = ""
        ticker = str(ticker or "").strip().upper()
    # API-backed evidence is presented in the report source drawer. Do not emit
    # local proxy URLs into markdown/PDF; they are implementation details.
    if kind in {"structured_fundamental", "market_data", "profile"}:
        return None
    if evidence_id.startswith(("ev_fmp_", "ev_feature_", "ev_profile_")):
        return None
    if evidence_id.startswith("ev_sec_"):
        match = re.match(r"ev_sec_(\d{10})", evidence_id)
        if match:
            return _SEC_COMPANY_URL.format(cik=match.group(1))
        if ticker:
            return _SEC_TICKER_URL.format(ticker=quote_plus(ticker))
    return None


def evidence_reference_title(item: dict[str, Any] | None, evidence_id: str = "") -> str:
    """Short human title for the References appendix."""
    if not item:
        return evidence_id or "Unknown source"
    kind = item.get("kind")
    if kind == "web_excerpt":
        title = str(item.get("title") or "").strip()
        if title and title.lower() != "untitled":
            return title[:120]
        url = str(item.get("url") or "")
        if url:
            host = urlparse(url).netloc.replace("www.", "")
            return host or url[:80]
        return "Web source"
    if kind == "filing_excerpt":
        filing = item.get("filing_type") or "SEC filing"
        section = item.get("section") or "Excerpt"
        return f"{filing} · {section}"
    if kind == "structured_fundamental":
        field = str(item.get("field", "?")).replace("_", " ")
        provider = str(item.get("source") or "FMP").split("+")[0].upper()
        return f"{provider} · {field}"
    if kind == "market_data":
        field = str(item.get("field", "?")).replace("_", " ")
        return f"Market data · {field.replace(' usd', '').strip()}"
    if kind == "profile":
        company = item.get("company_name") or "Company profile"
        sector = item.get("sector") or "?"
        industry = item.get("industry") or "?"
        return f"{company} ({sector} / {industry})"
    if kind == "document_excerpt":
        return f"Uploaded document · {item.get('filename', '?')}"
    return humanize_evidence_item(item)


def evidence_reference_meta(item: dict[str, Any] | None) -> str:
    """Secondary context shown after the em dash in References."""
    if not item:
        return ""
    parts: list[str] = []
    as_of = item.get("as_of") or item.get("published_date")
    if as_of:
        parts.append(str(as_of).split("T", 1)[0])

    tier = item.get("source_tier")
    if tier == "filing":
        parts.append("SEC EDGAR")
    elif tier == "news":
        parts.append("News")
    elif tier == "generic_web":
        parts.append("Web")
    elif tier == "structured_api":
        parts.append("Financial data API")

    evidence = str(item.get("evidence") or "").strip()
    if item.get("kind") == "structured_fundamental" and evidence:
        short = evidence[:90] + ("…" if len(evidence) > 90 else "")
        parts.append(short)
    elif item.get("kind") == "market_data" and item.get("value") is not None:
        val = item["value"]
        if isinstance(val, float) and abs(val) <= 1:
            parts.append(f"reported {val:.2%}")
        elif isinstance(val, (int, float)):
            parts.append(f"reported {val:,.4g}")

    return " · ".join(parts)


def format_reference_line(
    number: int,
    evidence_id: str,
    item: dict[str, Any] | None,
    ticker: str = "",
) -> str:
    """Bullet reference with optional markdown hyperlink."""
    if item is None and evidence_id:
        item = infer_evidence_item(evidence_id, ticker=ticker)
    title = evidence_reference_title(item, evidence_id)
    url = evidence_item_url(item, evidence_id=evidence_id, ticker=ticker)
    meta = evidence_reference_meta(item)
    body = f"[{title}]({url})" if url else title
    line = f"- **[{number}]** {body}"
    if meta:
        line += f" — {meta}"
    return line


def extract_evidence_items(
    source: dict[str, Any],
    *,
    text_limit: int = 4000,
) -> list[dict[str, Any]]:
    """Normalize evidence items from HITL snapshots or live workflow state."""
    raw_items: list[Any] = list(source.get("evidence_items") or [])
    if not raw_items:
        raw_items = list((source.get("evidence_pack") or {}).get("items") or [])

    items: list[dict[str, Any]] = []
    for candidate in raw_items:
        if not isinstance(candidate, dict):
            continue
        evidence_id = str(candidate.get("evidence_id") or "")
        if not evidence_id:
            continue
        entry = {k: v for k, v in candidate.items() if k != "metadata"}
        if "text" in entry and text_limit > 0:
            entry["text"] = str(entry["text"])[:text_limit]
        items.append(entry)
    return items


def resolve_evidence_item(
    evidence_id: str,
    by_id: dict[str, dict[str, Any]],
    *,
    all_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Look up a cited evidence_id, including common provider/field aliases."""
    if not evidence_id:
        return None
    direct = by_id.get(evidence_id)
    if direct:
        return direct

    field_hint = ""
    if evidence_id.startswith("ev_fmp+"):
        field_hint = evidence_id.split(":", 1)[-1].removeprefix("yfinance_").removeprefix("fmp_")
    elif evidence_id.startswith("ev_fmp_"):
        field_hint = evidence_id.removeprefix("ev_fmp_")
    elif evidence_id.startswith("ev_feature_"):
        field_hint = evidence_id.removeprefix("ev_feature_")

    if field_hint:
        for item in all_items or []:
            if str(item.get("field") or "") == field_hint:
                return item
        alt_ids = (
            f"ev_fmp_{field_hint}",
            f"ev_fmp+fallback:yfinance_{field_hint}",
            f"ev_feature_{field_hint}",
        )
        for alt in alt_ids:
            if alt in by_id:
                return by_id[alt]

    return None


def payload_evidence_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return de-duplicated evidence items from all payload shapes we persist."""
    candidates: list[Any] = []
    candidates.extend(payload.get("_evidence_items") or [])

    evidence_pack = payload.get("evidence_pack") or {}
    if isinstance(evidence_pack, dict):
        candidates.extend(evidence_pack.get("items") or [])

    hitl_snapshot = payload.get("hitl_snapshot") or {}
    if isinstance(hitl_snapshot, dict):
        candidates.extend(hitl_snapshot.get("evidence_items") or [])

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        evidence_id = str(candidate.get("evidence_id") or "")
        if not evidence_id or evidence_id in seen:
            continue
        seen.add(evidence_id)
        items.append(candidate)
    return items


def infer_evidence_item(evidence_id: str, ticker: str = "") -> dict[str, Any]:
    """Create a presentable source-card fallback for a cited-but-missing ID."""
    symbol = str(ticker or "").strip().upper()
    base: dict[str, Any] = {
        "evidence_id": evidence_id,
        "as_of": "",
        "source": "inferred",
        "inferred": True,
    }

    if evidence_id.startswith("ev_sec_"):
        return {
            **base,
            "kind": "filing_excerpt",
            "source_tier": "filing",
            "source": "sec",
            "filing_type": "SEC filing",
            "section": "Referenced filing",
            "title": "SEC filing reference",
            "url": evidence_item_url(None, evidence_id=evidence_id, ticker=symbol),
        }

    if evidence_id.startswith("ev_web_"):
        return {
            **base,
            "kind": "web_excerpt",
            "source_tier": "generic_web",
            "source": "web",
            "title": "Web source reference",
        }

    if evidence_id.startswith("ev_feature_"):
        field = evidence_id.removeprefix("ev_feature_")
        return {
            **base,
            "kind": "market_data",
            "source_tier": "structured_api",
            "source": "fmp+yfinance",
            "field": field,
            "title": f"Market data · {field.replace('_', ' ')}",
        }

    if evidence_id.startswith("ev_fmp_"):
        field = evidence_id.removeprefix("ev_fmp_")
        return {
            **base,
            "kind": "structured_fundamental",
            "source_tier": "structured_api",
            "source": "fmp",
            "field": field,
            "title": f"FMP · {field.replace('_', ' ')}",
        }

    if evidence_id.startswith("ev_fmp+"):
        _, _, field_raw = evidence_id.removeprefix("ev_fmp+").partition(":")
        field = field_raw.removeprefix("yfinance_").removeprefix("fmp_") or field_raw
        return {
            **base,
            "kind": "structured_fundamental",
            "source_tier": "structured_api",
            "source": "fmp",
            "field": field,
            "title": f"FMP · {field.replace('_', ' ')}",
        }

    if evidence_id.startswith("ev_profile_"):
        return {
            **base,
            "kind": "profile",
            "source_tier": "structured_api",
            "source": "derived",
            "title": "Company profile reference",
        }

    return {
        **base,
        "kind": "unknown",
        "source_tier": "unknown",
        "title": evidence_id,
    }


def _parse_reference_ids(reference: str) -> list[str]:
    return [part.strip() for part in reference.split(",") if part.strip()]


def _provenance_ref_ids(prov: dict[str, Any]) -> list[str]:
    refs = list(prov.get("evidence_refs") or [])
    if refs:
        return refs
    reference = prov.get("reference")
    if reference:
        return _parse_reference_ids(str(reference))
    return []


def inline_cite_text(text: str, registry: SourceRegistry) -> str:
    """Replace embedded evidence_ids in prose with [n] citation markers."""
    if not text:
        return text
    result = text
    ids = _EVIDENCE_ID_PATTERN.findall(text)
    for evidence_id in sorted(set(ids), key=len, reverse=True):
        number = registry.register(evidence_id)
        if not number:
            continue
        marker = f"[{number}]"
        result = result.replace(f"({evidence_id})", marker)
        result = re.sub(
            rf"(?<!\[)\b{re.escape(evidence_id)}\b(?!\])",
            marker,
            result,
        )
    return result


def company_state_ref_ids(company_state: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for entry in company_state.get("evidence_refs") or []:
        if isinstance(entry, dict) and entry.get("evidence_id"):
            refs.append(str(entry["evidence_id"]))
        elif isinstance(entry, str):
            refs.append(entry)
    for value in company_state.values():
        if isinstance(value, str):
            refs.extend(_EVIDENCE_ID_PATTERN.findall(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    refs.extend(_EVIDENCE_ID_PATTERN.findall(item))
    return refs


def resolve_feature_evidence_id(
    field: str,
    evidence_items: list[dict[str, Any]],
) -> str | None:
    for item in evidence_items:
        if item.get("field") != field:
            continue
        if item.get("kind") in ("market_data", "structured_fundamental", "profile"):
            eid = item.get("evidence_id")
            if eid:
                return str(eid)
    return None


def wacc_input_ref_ids(
    wacc_comp: dict[str, Any],
    features: dict[str, Any],
    evidence_items: list[dict[str, Any]],
) -> list[str]:
    """Evidence ids backing CAPM / capital-structure inputs."""
    refs: list[str] = []
    field_map = (
        ("beta", "beta"),
        ("equity_value_usd", "equity_value_usd"),
        ("total_debt_usd", "total_debt_usd"),
        ("interest_expense_usd", "interest_expense_usd"),
        ("net_debt_usd", "net_debt_usd"),
        ("effective_tax_rate_hint", "effective_tax_rate_hint"),
    )
    for _key, feature_field in field_map:
        if features.get(feature_field) is None and wacc_comp.get(_key) is None:
            continue
        eid = resolve_feature_evidence_id(feature_field, evidence_items)
        if eid:
            refs.append(eid)
    tax_id = resolve_feature_evidence_id("tax_rate", evidence_items)
    if tax_id:
        refs.append(tax_id)
    return refs


def filter_profile_item(
    evidence_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for item in evidence_items:
        if item.get("kind") == "profile":
            return item
    return None


def filter_news_items(
    evidence_items: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    news = [
        item for item in evidence_items
        if item.get("kind") == "web_excerpt"
        and item.get("source_tier") in _NEWS_SOURCE_TIERS
    ]
    news.sort(
        key=lambda item: str(item.get("published_date") or item.get("as_of") or ""),
        reverse=True,
    )
    return news[:limit]


def format_usd_compact(value: float | int | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    if amount >= 1_000_000_000_000:
        return f"${amount / 1_000_000_000_000:.2f}T"
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.1f}B"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.0f}M"
    return f"${amount:,.0f}"


def company_profile_section_lines(
    payload: dict[str, Any],
    registry: SourceRegistry,
) -> list[str]:
    profile_meta = payload.get("profile_meta") or {}
    evidence_items = payload_evidence_items(payload)
    if not profile_meta and not filter_profile_item(evidence_items):
        return []

    profile_item = filter_profile_item(evidence_items)
    profile_ref = registry.format_refs(
        [profile_item["evidence_id"]] if profile_item and profile_item.get("evidence_id") else [],
    )

    company = profile_meta.get("company_name") or (
        profile_item.get("company_name") if profile_item else None
    ) or str(payload.get("ticker") or "?")
    sector = profile_meta.get("sector") or (profile_item or {}).get("sector") or "?"
    industry = profile_meta.get("industry") or (profile_item or {}).get("industry") or "?"
    profile_name = payload.get("profile") or (profile_item or {}).get("profile") or "default"
    market_cap = profile_meta.get("market_cap_usd") or (profile_item or {}).get("market_cap_usd")
    spot = profile_meta.get("spot_price") or (profile_item or {}).get("spot_price")
    currency = profile_meta.get("currency") or "USD"

    lines = ["## Company Profile", ""]
    lines.append(
        f"**{company}** · {sector} / {industry} · `{profile_name}` profile {profile_ref}".rstrip()
    )
    detail_parts: list[str] = []
    if market_cap is not None:
        detail_parts.append(f"Market cap: {format_usd_compact(market_cap)}")
    if spot is not None:
        detail_parts.append(f"Spot: ${float(spot):.2f}")
    if currency:
        detail_parts.append(f"Currency: {currency}")
    if detail_parts:
        lines.append(" · ".join(detail_parts) + (f" {profile_ref}" if profile_ref != "—" else ""))
    lines.append("")
    return lines


def recent_developments_section_lines(
    payload: dict[str, Any],
    registry: SourceRegistry,
    *,
    limit: int = 5,
) -> list[str]:
    news_items = filter_news_items(payload_evidence_items(payload), limit=limit)
    if not news_items:
        return []

    lines = ["## Recent Developments", ""]
    for item in news_items:
        eid = item.get("evidence_id", "")
        refs = registry.format_refs([str(eid)] if eid else [])
        title = item.get("title") or "Untitled"
        if title == "untitled":
            title = item.get("url", "Web source")[:80]
        date = item.get("published_date") or item.get("as_of") or ""
        if isinstance(date, str) and "T" in date:
            date = date.split("T", 1)[0]
        snippet = str(item.get("text") or "").replace("\n", " ").strip()
        if len(snippet) > 180:
            snippet = snippet[:177] + "…"
        date_str = f" ({date})" if date else ""
        lines.append(f"- **{title}**{date_str} — {snippet} {refs}".rstrip())
    lines.append("")
    return lines


def market_reconciliation_section_lines(
    payload: dict[str, Any],
    registry: SourceRegistry,
) -> list[str]:
    wacc_sanity = payload.get("wacc_sanity") or {}
    assumptions = payload.get("assumptions") or {}
    features = payload.get("features") or {}
    evidence_items = payload_evidence_items(payload)
    wacc_refs = registry.format_refs(
        wacc_input_ref_ids(payload.get("wacc_components") or {}, features, evidence_items),
    )

    has_wacc = wacc_sanity.get("capm_wacc") is not None or assumptions.get("wacc") is not None
    has_growth = payload.get("implied_growth") is not None
    has_margin = payload.get("implied_margin") is not None
    if not has_wacc and not has_growth and not has_margin:
        return []

    lines = [
        "## Market Reconciliation",
        "",
        "Reverse-DCF signals compare **model assumptions** to values required under the **current DCF structure**.",
        "These are DCF-consistent implied values, not direct market forecasts. A large gap means the price embeds different growth/margin/discount-rate expectations — "
        "**not** that the spreadsheet failed or the model is invalid.",
        "",
        "| Signal | Model | DCF-consistent implied | Gap / status | Refs |",
        "|--------|-------|----------------|--------------|------|",
    ]

    model_wacc = wacc_sanity.get("capm_wacc") or assumptions.get("wacc")
    implied_wacc = wacc_sanity.get("implied_wacc")
    if model_wacc is not None:
        if implied_wacc is not None:
            gap_bps = wacc_sanity.get("gap_bps")
            gap_str = f"{gap_bps:+}bps" if gap_bps is not None else "—"
            lines.append(
                f"| WACC | {float(model_wacc):.2%} | {float(implied_wacc):.2%} | {gap_str} | {wacc_refs} |"
            )
        else:
            status = wacc_sanity.get("solver_status") or wacc_sanity.get("flag") or "unavailable"
            lines.append(
                f"| WACC | {float(model_wacc):.2%} | — | {status} | {wacc_refs} |"
            )

    model_growth = assumptions.get("revenue_growth")
    implied_growth = payload.get("implied_growth")
    if model_growth is not None and implied_growth is not None:
        gap_pp = (float(model_growth) - float(implied_growth)) * 100
        growth_refs = registry.format_refs(
            _provenance_ref_ids((payload.get("assumption_provenance") or {}).get("revenue_growth") or {}),
        )
        lines.append(
            f"| Revenue growth | {float(model_growth):.2%} | {float(implied_growth):.2%} | {gap_pp:+.1f}pp | {growth_refs} |"
        )

    model_margin = assumptions.get("fcff_margin")
    implied_margin = payload.get("implied_margin")
    if model_margin is not None and implied_margin is not None:
        gap_pp = (float(model_margin) - float(implied_margin)) * 100
        margin_refs = registry.format_refs(
            _provenance_ref_ids((payload.get("assumption_provenance") or {}).get("fcff_margin") or {}),
        )
        lines.append(
            f"| FCFF margin | {float(model_margin):.2%} | {float(implied_margin):.2%} | {gap_pp:+.1f}pp | {margin_refs} |"
        )

    plausibility = wacc_sanity.get("implied_plausibility") or {}
    plaus_label = plausibility.get("label")
    if plaus_label and plaus_label != "unavailable":
        badge = {
            "economically_implausible": "⚠ Economically implausible",
            "aggressive": "⚠ Aggressive",
            "reasonable": "✓ Reasonable",
            "conservative": "✓ Conservative",
        }.get(plaus_label, plaus_label)
        lines.append("")
        lines.append(f"**Implied WACC plausibility:** {badge}")
        narrative = plausibility.get("narrative")
        if narrative:
            lines.append(f"> {narrative}")

    interpretation = wacc_sanity.get("interpretation")
    if interpretation:
        lines.append("")
        lines.append(f"**Read-through:** {inline_cite_text(str(interpretation), registry)}")

    signals_meta = payload.get("market_signals_meta") or {}
    if signals_meta.get("growth_margin_suppressed"):
        lines.append("")
        lines.append(
            "**Note:** Implied revenue growth and FCFF margin are omitted because the "
            "WACC gap is the binding constraint at fixed model structure — holding WACC "
            "constant, no plausible growth/margin lever alone reconciles spot. Consider "
            "quality/moat premium, duration, or non-FCF value not captured in this DCF."
        )

    lines.append("")
    return lines


class SourceRegistry:
    """Assign stable [1], [2], … citation numbers to evidence_ids."""

    def __init__(self, evidence_items: list[dict[str, Any]] | None = None, ticker: str = ""):
        self._ticker = str(ticker or "").strip().upper()
        self._by_id: dict[str, dict[str, Any]] = {
            item.get("evidence_id", ""): item
            for item in (evidence_items or [])
            if item.get("evidence_id")
        }
        self._numbers: dict[str, int] = {}
        self._order: list[str] = []

    def register(self, evidence_id: str) -> int | None:
        if not evidence_id:
            return None
        if evidence_id not in self._numbers:
            self._numbers[evidence_id] = len(self._order) + 1
            self._order.append(evidence_id)
        return self._numbers[evidence_id]

    def register_many(self, evidence_ids: list[str]) -> list[int]:
        nums: list[int] = []
        for eid in evidence_ids:
            num = self.register(eid)
            if num is not None:
                nums.append(num)
        return nums

    def format_refs(self, evidence_ids: list[str]) -> str:
        nums = sorted(set(self.register_many(evidence_ids)))
        return " ".join(f"[{n}]" for n in nums) if nums else "—"

    def citation_map(self) -> dict[str, str]:
        return {str(number): evidence_id for evidence_id, number in self._numbers.items()}

    def reference_line(self, number: int, evidence_id: str) -> str:
        item = self._by_id.get(evidence_id)
        return format_reference_line(number, evidence_id, item, ticker=self._ticker)

    def references_section_lines(self) -> list[str]:
        if not self._order:
            return []
        lines = [
            "## References",
            "",
            "Numbered citations in the report map to the sources below. "
            "Linked entries open the underlying filing, article, or data page.",
            "",
        ]
        for evidence_id in self._order:
            number = self._numbers[evidence_id]
            lines.append(self.reference_line(number, evidence_id))
        lines.append("")
        return lines

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SourceRegistry:
        registry = cls(payload_evidence_items(payload), ticker=str(payload.get("ticker") or ""))

        memo = payload.get("assumption_memo") or {}
        if isinstance(memo, dict):
            for proposal in memo.get("proposals") or []:
                if isinstance(proposal, dict):
                    registry.register_many(list(proposal.get("evidence_refs") or []))

        provenance = payload.get("assumption_provenance") or {}
        if isinstance(provenance, dict):
            for prov in provenance.values():
                if isinstance(prov, dict):
                    registry.register_many(_provenance_ref_ids(prov))

        company_state = payload.get("company_state") or {}
        if isinstance(company_state, dict):
            registry.register_many(company_state_ref_ids(company_state))

        evidence_items = payload_evidence_items(payload)
        profile_item = filter_profile_item(evidence_items)
        if profile_item and profile_item.get("evidence_id"):
            registry.register(str(profile_item["evidence_id"]))

        for item in filter_news_items(evidence_items, limit=5):
            if item.get("evidence_id"):
                registry.register(str(item["evidence_id"]))

        features = payload.get("features") or {}
        wacc_comp = payload.get("wacc_components") or {}
        registry.register_many(
            wacc_input_ref_ids(wacc_comp, features, evidence_items),
        )

        return registry


_SOURCE_LABELS: dict[str, str] = {
    "fmp": "FMP financial statements",
    "canonical": "Canonical fundamentals",
    "llm_memo": "Analyst memo",
    "capm": "CAPM / WACC model",
    "profile_prior_fallback": "Sector profile prior",
    "user_override": "User override",
    "user_provided": "User-provided input",
    "user_edited": "User edit at review",
    "test": "Test fixture",
}


def field_basis(
    field: str,
    prov: dict[str, Any],
    memo_proposal: dict[str, Any] | None = None,
) -> str:
    """Short basis text for the assumptions table (no citation numbers)."""
    source = str(prov.get("source") or "unknown")
    evidence = str(prov.get("evidence") or "").strip()

    if prov.get("user_edited"):
        memo_rationale = (memo_proposal or {}).get("rationale", "")
        if memo_rationale:
            short = memo_rationale[:120] + ("…" if len(memo_rationale) > 120 else "")
            return f"User override at approval (memo: {short})"
        if evidence:
            return f"User override at approval ({evidence[:100]})"
        return "User override at approval"

    if prov.get("approved_by") == "user" and source not in ("user_provided", "user_override"):
        prefix = _SOURCE_LABELS.get(source, source.replace("_", " "))
        if evidence:
            short = evidence[:140] + ("…" if len(evidence) > 140 else "")
            return f"{short} (approved by user)"
        return f"{prefix} (approved by user)"

    if source == "llm_memo" and evidence:
        return evidence[:160] + ("…" if len(evidence) > 160 else "")

    if evidence:
        return evidence[:160] + ("…" if len(evidence) > 160 else "")

    return _SOURCE_LABELS.get(source, source.replace("_", " "))


def merge_hitl_provenance(
    provenance: dict[str, dict[str, Any]],
    overrides: dict[str, float],
    original_assumptions: dict[str, float],
) -> dict[str, dict[str, Any]]:
    """Preserve memo/filing lineage; mark user approval and edits."""
    merged: dict[str, dict[str, Any]] = {
        k: dict(v) if isinstance(v, dict) else {}
        for k, v in provenance.items()
    }
    for field, value in overrides.items():
        prov = dict(merged.get(field) or {})
        original = original_assumptions.get(field)
        if original is not None and abs(float(value) - float(original)) > 1e-9:
            prov["user_edited"] = True
        prov["approved_by"] = "user"
        merged[field] = prov
    return merged


def reconstruct_memo_from_provenance(
    provenance: dict[str, Any],
    assumptions: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild assumption_memo proposals from stored provenance (fast-path helper)."""
    proposals: list[dict[str, Any]] = []
    if not isinstance(provenance, dict):
        return {}
    for field, prov in provenance.items():
        if not isinstance(prov, dict):
            continue
        refs = _provenance_ref_ids(prov)
        if prov.get("source") != "llm_memo" and not refs:
            continue
        proposals.append({
            "field": field,
            "value": assumptions.get(field),
            "rationale": prov.get("evidence", ""),
            "confidence": prov.get("confidence", 0.5),
            "evidence_refs": refs,
            "range_low": prov.get("range_low"),
            "range_high": prov.get("range_high"),
        })
    return {"proposals": proposals} if proposals else {}
