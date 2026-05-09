"""SEC EDGAR filing fetcher — free, rate-limited, source-tier "filing".

Retrieves recent 10-K/10-Q filings and extracts key sections (Risk Factors, MD&A)
as normalized evidence items with stable IDs, provenance, and as_of timestamps.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEC_BASE_URL = "https://www.sec.gov/"
SEC_DATA_URL = "https://data.sec.gov/"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/"

# Rate limiting — SEC asks for ≤10 req/s. We use 6 req/s to be safe.
_MIN_REQUEST_INTERVAL = 1.0 / 6.0

# User-Agent is required by SEC EDGAR. Identify yourself honestly.
_USER_AGENT = "langgraph-research-agent/0.1 (contact@example.com)"

# Cache the ticker→CIK mapping for the process lifetime.
_TICKER_CACHE: dict[str, str] = {}
_CIK_CACHE_LOADED = False

# Section patterns for 10-K/10-Q extraction.
# We look for Item headers in the HTML text and capture until the next Item.
_SECTION_PATTERNS: dict[str, re.Pattern] = {
    "risk_factors": re.compile(
        r"(?:Item\s*1A[\.\s:]*Risk\s*Factors)",
        re.IGNORECASE,
    ),
    "mda": re.compile(
        r"(?:Item\s*7[\.\s:]*Management'?s?\s*Discussion\s*(?:and|&)\s*Analysis)",
        re.IGNORECASE,
    ),
    "business": re.compile(
        r"(?:Item\s*1[\.\s:]*Business)",
        re.IGNORECASE,
    ),
    "legal_proceedings": re.compile(
        r"(?:Item\s*3[\.\s:]*Legal\s*Proceedings)",
        re.IGNORECASE,
    ),
    "quantitative_disclosures": re.compile(
        r"(?:Item\s*7A[\.\s:]*Quantitative)",
        re.IGNORECASE,
    ),
}

# Next-section boundary — stop capturing when we hit another Item header.
_NEXT_ITEM_PATTERN = re.compile(
    r"(?:Item\s*\d+[A-Z]?[\.\s:])|(?:PART\s+[IVX]+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_last_request_time: float = 0.0


def _rate_limit() -> None:
    """Enforce SEC's 10 req/s limit with a safety margin."""
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.time()


def _sec_get(url: str, params: dict | None = None) -> dict | list | None:
    """GET a SEC EDGAR JSON endpoint with rate limiting and error handling."""
    _rate_limit()
    try:
        resp = requests.get(
            url,
            params=params,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("SEC EDGAR request failed url=%s error=%s", url, exc)
        return None


def _sec_get_text(url: str) -> str | None:
    """GET raw text/HTML from SEC with rate limiting."""
    _rate_limit()
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html, text/plain",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        logger.warning("SEC EDGAR text fetch failed url=%s error=%s", url, exc)
        return None


def _load_ticker_map() -> dict[str, str]:
    """Load SEC company_tickers.json → {ticker: CIK} mapping.

    Cached in memory for the process lifetime.
    """
    global _TICKER_CACHE, _CIK_CACHE_LOADED
    if _CIK_CACHE_LOADED:
        return _TICKER_CACHE

    _rate_limit()
    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": _USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for entry in data.values():
            ticker = (entry.get("ticker") or "").upper().strip()
            cik = str(entry.get("cik_str", ""))
            if ticker and cik:
                _TICKER_CACHE[ticker] = cik
    except Exception as exc:
        logger.warning("SEC ticker map load failed: %s", exc)

    _CIK_CACHE_LOADED = True
    return _TICKER_CACHE


def _cik_for_ticker(ticker: str) -> str | None:
    """Look up CIK from ticker. Returns zero-padded 10-digit CIK or None."""
    ticker_map = _load_ticker_map()
    cik_raw = ticker_map.get(ticker.upper())
    if not cik_raw:
        return None
    # CIK must be zero-padded to 10 digits for EDGAR endpoints
    return str(int(cik_raw)).zfill(10)


def _strip_html(text: str) -> str:
    """Crude HTML→text: remove tags, decode entities, collapse whitespace."""
    # Remove script/style blocks
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    text = text.replace("&#x27;", "'").replace("&#x2F;", "/")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_sections(html_text: str) -> dict[str, str]:
    """Extract key 10-K/10-Q sections by regex-matching Item headers.

    Returns a dict mapping section_name → text (up to 8000 chars each).
    """
    clean = _strip_html(html_text)
    if len(clean) < 500:
        return {}

    sections: dict[str, str] = {}
    for name, pattern in _SECTION_PATTERNS.items():
        match = pattern.search(clean)
        if not match:
            continue
        start = match.start()
        # Look for the next Item header after this section
        next_match = _NEXT_ITEM_PATTERN.search(clean, match.end())
        end = next_match.start() if next_match else min(start + 12000, len(clean))
        section_text = clean[start:end].strip()
        # Truncate to reasonable size
        if len(section_text) > 8000:
            section_text = section_text[:8000] + "..."
        sections[name] = section_text

    return sections


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_sec_filings(
    ticker: str,
    *,
    max_filings: int = 2,
) -> list[dict[str, Any]]:
    """Fetch recent 10-K/10-Q filings and return evidence items.

    Each item carries:
        - evidence_id: stable identifier
        - kind: "filing_excerpt"
        - source_tier: "filing" (highest tier)
        - source: "sec_edgar"
        - as_of: filing date
        - text: extracted section content
        - metadata: filing_type, section, accession_number, url
    """
    cik = _cik_for_ticker(ticker)
    if not cik:
        logger.info("SEC filings: no CIK found for ticker=%s", ticker)
        return []

    # Get submissions (filing history)
    submissions = _sec_get(f"{SEC_DATA_URL}submissions/CIK{cik}.json")
    if not submissions or not isinstance(submissions, dict):
        logger.info("SEC filings: no submissions data for CIK=%s", cik)
        return []

    # Filter to recent 10-K and 10-Q
    filings_raw = submissions.get("filings", {})
    recent = filings_raw.get("recent", {})
    if not recent:
        return []

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    evidence_items: list[dict[str, Any]] = []
    collected = 0

    for i in range(len(forms)):
        if collected >= max_filings:
            break
        form = (forms[i] or "").upper().strip()
        if form not in {"10-K", "10-Q"}:
            continue

        filing_date = dates[i] if i < len(dates) else ""
        accession = accessions[i] if i < len(accessions) else ""
        primary = primary_docs[i] if i < len(primary_docs) else ""

        if not accession or not primary:
            continue

        # Build the document URL
        # Accession numbers have dashes that need to be stripped for the URL
        clean_acc = accession.replace("-", "")
        doc_url = f"{SEC_ARCHIVE_URL}{cik}/{clean_acc}/{primary}"

        # Fetch and extract
        html_text = _sec_get_text(doc_url)
        if not html_text:
            continue

        sections = _extract_sections(html_text)
        if not sections:
            continue

        for section_name, section_text in sections.items():
            section_label = {
                "risk_factors": "Risk Factors (Item 1A)",
                "mda": "MD&A (Item 7)",
                "business": "Business (Item 1)",
                "legal_proceedings": "Legal Proceedings (Item 3)",
                "quantitative_disclosures": "Quantitative Disclosures (Item 7A)",
            }.get(section_name, section_name)

            evidence_id = f"ev_sec_{clean_acc[:16]}_{section_name}"
            evidence_items.append({
                "evidence_id": evidence_id,
                "kind": "filing_excerpt",
                "source_tier": "filing",
                "source": "sec_edgar",
                "as_of": filing_date,
                "section": section_label,
                "filing_type": form,
                "accession_number": accession,
                "ticker": ticker.upper(),
                "text": section_text,
                "url": doc_url,
            })

        collected += 1

    logger.info(
        "SEC filings: ticker=%s filings=%d sections=%d",
        ticker, collected, len(evidence_items),
    )
    return evidence_items
