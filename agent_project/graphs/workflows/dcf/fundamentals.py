"""Fundamental-data fetchers (FMP, yfinance) and the hydrate_fundamentals node.

The hydrate node pulls canonical Tier-A levels (revenue, shares, net debt)
and builds the ``features`` vector used by downstream WACC estimation.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

from .activity import emit_step
from .priors import classify_profile
from .state import canonical_numeric, coerce_finite_float

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _merge_dcf_extras(
    fmp_extras: dict[str, Any],
    yfinance_extras: dict[str, Any],
) -> dict[str, Any]:
    """Combine API-specific markers; FMP wins on duplicate keys."""
    merged = dict(yfinance_extras)
    merged.update(fmp_extras)
    return merged


def _build_feature_vector(
    *,
    ticker: str,
    profile_bucket: str,
    profile_meta: dict[str, Any],
    fundamentals: dict[str, dict[str, Any]],
    dcf_extras: dict[str, Any],
) -> dict[str, Any]:
    """Single dict of CAPM / capital-structure inputs (+ coarse labels).

    Lives on state for auditing and deterministic WACC; not an assumption
    merge target.
    """
    features: dict[str, Any] = {
        "ticker": ticker.upper(),
        "profile_bucket": profile_bucket,
        "sector": profile_meta.get("sector"),
        "industry": profile_meta.get("industry"),
    }
    for key in (
        "beta",
        "market_cap_usd",
        "total_debt_usd",
        "cash_usd",
        "interest_expense_usd",
    ):
        v = coerce_finite_float(dcf_extras.get(key))
        if v is not None:
            features[key] = v

    tax = canonical_numeric(fundamentals, "tax_rate")
    if tax is not None:
        features["effective_tax_rate_hint"] = tax

    mc = features.get("market_cap_usd")
    mc_f = coerce_finite_float(mc)
    if mc_f is not None and mc_f > 0:
        features["equity_value_usd"] = mc_f
    else:
        spot = coerce_finite_float(profile_meta.get("spot_price"))
        sh_mil = canonical_numeric(fundamentals, "shares_outstanding")
        if spot is not None and sh_mil is not None and sh_mil > 0 and spot > 0:
            features["equity_value_usd"] = float(sh_mil) * 1_000_000.0 * float(spot)

    debt = features.get("total_debt_usd")
    cash = features.get("cash_usd")
    nd_mil = canonical_numeric(fundamentals, "net_debt")
    if nd_mil is not None:
        features["net_debt_usd"] = float(nd_mil) * 1_000_000.0
    elif isinstance(debt, (int, float)) and isinstance(cash, (int, float)):
        features["net_debt_usd"] = float(debt) - float(cash)

    return features


# ---------------------------------------------------------------------------
# FMP fetcher
# ---------------------------------------------------------------------------


def _fmp_get_json(path: str, api_key: str) -> list[dict[str, Any]]:
    """Fetch a JSON list from FMP's /stable API."""
    url = f"https://financialmodelingprep.com/stable/{path}"
    try:
        response = requests.get(url, params={"apikey": api_key}, timeout=12)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("DCF fundamentals: FMP request failed path=%s error=%s", path, exc)
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if payload.get("Error Message") or payload.get("error"):
            logger.warning("DCF fundamentals: FMP returned error payload for path=%s", path)
        return []
    return []


def _compute_effective_tax_rate(income: dict[str, Any]) -> float | None:
    """Effective tax rate from FMP income statement.

    Returns None when components missing or pre-tax income is non-positive.
    """
    pretax = income.get("incomeBeforeTax")
    tax = income.get("incomeTaxExpense")
    if not isinstance(pretax, (int, float)) or pretax <= 0:
        return None
    if not isinstance(tax, (int, float)):
        return None
    rate = float(tax) / float(pretax)
    if rate < 0.0 or rate > 0.45:
        return None
    return rate


def _compute_fcff_components(
    income: dict[str, Any],
    cashflow: dict[str, Any],
) -> dict[str, float] | None:
    """Compute unlevered free cash flow (FCFF) from FMP statement components.

    Uses the cleaner derivation:
        FCFF = FCF + InterestExpense * (1 - tax_rate)
    where FCF is FMP's ``freeCashFlow``. Falls back to ``FCF / Revenue``
    as a lower-confidence proxy when full components are unavailable.
    """
    fcf_raw = cashflow.get("freeCashFlow")
    if not isinstance(fcf_raw, (int, float)) or fcf_raw == 0:
        return None
    interest_raw = income.get("interestExpense") or 0.0
    if not isinstance(interest_raw, (int, float)):
        interest_raw = 0.0
    tax_rate = _compute_effective_tax_rate(income)
    if tax_rate is None:
        tax_rate = 0.21
    fcff = float(fcf_raw) + float(interest_raw) * (1.0 - float(tax_rate))
    return {"fcff": fcff, "tax_rate": tax_rate, "fcf": float(fcf_raw)}


def _fetch_fundamentals_fmp(ticker: str) -> dict[str, dict[str, Any]]:
    """Pull canonical fundamentals from FMP, normalized to millions."""
    api_key = (
        os.getenv("FMP_API_KEY")
        or os.getenv("FINANCIAL_MODELING_PREP_API_KEY")
    )
    if not api_key:
        return {}

    income_rows = _fmp_get_json(f"income-statement?symbol={ticker}&period=annual&limit=1", api_key)
    balance_rows = _fmp_get_json(f"balance-sheet-statement?symbol={ticker}&period=annual&limit=1", api_key)
    cashflow_rows = _fmp_get_json(f"cash-flow-statement?symbol={ticker}&period=annual&limit=1", api_key)
    profile_rows = _fmp_get_json(f"profile?symbol={ticker}", api_key)

    income = income_rows[0] if income_rows else {}
    balance = balance_rows[0] if balance_rows else {}
    cashflow = cashflow_rows[0] if cashflow_rows else {}
    profile = profile_rows[0] if profile_rows else {}
    as_of = (
        str(income.get("date") or balance.get("date") or cashflow.get("date") or "")
        or time.strftime("%Y-%m-%d")
    )

    out: dict[str, dict[str, Any]] = {}

    # --- shares outstanding ---
    weighted_shares = income.get("weightedAverageShsOutDil")
    market_cap = None
    spot_price = None
    if isinstance(profile, dict):
        market_cap = profile.get("marketCap") or profile.get("mktCap")
        spot_price = profile.get("price")
    derived_shares = None
    if (
        isinstance(market_cap, (int, float)) and market_cap > 0
        and isinstance(spot_price, (int, float)) and spot_price > 0
    ):
        derived_shares = float(market_cap) / float(spot_price)  # always raw share count
    shares_final = (
        weighted_shares
        if isinstance(weighted_shares, (int, float)) and weighted_shares > 0
        else derived_shares
    )
    if isinstance(shares_final, (int, float)) and shares_final > 0:
        shares_in_millions = float(shares_final) / 1_000_000.0
        shares_field = "weightedAverageShsOutDil"
        # Sanity check: any listed company has >1M shares. If < 1M after dividing,
        # FMP likely returned shares already in millions (unit mismatch). Cross-check
        # against market-cap-derived count.
        if shares_in_millions < 1.0:
            if derived_shares and derived_shares / 1_000_000.0 >= 1.0:
                # Use market-cap-implied shares (reliable for large caps)
                shares_final = derived_shares
                shares_in_millions = derived_shares / 1_000_000.0
                shares_field = "marketCap/price"
                logger.warning(
                    "DCF fundamentals: FMP share count looks wrong (<1M after /1e6), "
                    "using marketCap/price fallback ticker=%s raw=%s derived_M=%.1f",
                    ticker, weighted_shares, shares_in_millions,
                )
            else:
                logger.warning(
                    "DCF fundamentals: share count unusable ticker=%s shares_M=%.4f",
                    ticker, shares_in_millions,
                )
                shares_final = None
        if isinstance(shares_final, (int, float)) and shares_final > 0:
            out["shares_outstanding"] = {
                "value": shares_in_millions,
                "source": "fmp",
                "field": shares_field,
                "raw_value": float(shares_final),
                "raw_unit": "shares",
                "as_of": as_of,
                "confidence": 0.97 if shares_field == "weightedAverageShsOutDil" else 0.90,
                "evidence": "FMP annual statement share count." if shares_field == "weightedAverageShsOutDil"
                            else "Shares derived from FMP marketCap / price.",
            }

    # --- base revenue ---
    revenue_raw = income.get("revenue")
    if isinstance(revenue_raw, (int, float)) and revenue_raw > 0:
        out["base_revenue"] = {
            "value": float(revenue_raw) / 1_000_000.0,
            "source": "fmp",
            "field": "revenue",
            "raw_value": float(revenue_raw),
            "raw_unit": "USD",
            "as_of": as_of,
            "confidence": 0.97,
            "evidence": "FMP annual revenue.",
        }

    # --- net debt ---
    # Debt: prefer longTermDebt + shortTermDebt (financial debt only — excludes
    # operating lease liabilities that FMP bundles into totalDebt for many companies).
    # Cash: prefer cashAndShortTermInvestments (broadest liquid measure) to avoid
    # overstating net debt for cash-rich companies (META, AAPL, MSFT hold large ST
    # investment portfolios not captured by narrow cashAndCashEquivalents).
    ltd = balance.get("longTermDebt")
    std = balance.get("shortTermDebt")
    if isinstance(ltd, (int, float)) or isinstance(std, (int, float)):
        debt_raw = (float(ltd) if isinstance(ltd, (int, float)) else 0.0
                    + float(std) if isinstance(std, (int, float)) else 0.0)
        debt_field = "longTermDebt + shortTermDebt"
    else:
        debt_raw = balance.get("totalDebt")
        debt_field = "totalDebt"
    cash_raw = (
        balance.get("cashAndShortTermInvestments")  # cash + ST investments (preferred)
        or balance.get("cashAndCashEquivalents")    # narrow cash (fallback)
    )
    cash_field = (
        "cashAndShortTermInvestments"
        if isinstance(balance.get("cashAndShortTermInvestments"), (int, float))
        else "cashAndCashEquivalents"
    )
    if isinstance(debt_raw, (int, float)) or isinstance(cash_raw, (int, float)):
        debt = float(debt_raw) if isinstance(debt_raw, (int, float)) else 0.0
        cash = float(cash_raw) if isinstance(cash_raw, (int, float)) else 0.0
        net_debt_usd = debt - cash
        # Sanity log: net_debt > 2× revenue is unusual — may indicate bad data
        revenue_usd = float(income.get("revenue") or 0)
        if revenue_usd > 0 and net_debt_usd > revenue_usd * 2:
            logger.warning(
                "DCF fundamentals: net_debt may be overstated ticker=%s "
                "net_debt=%.0fM revenue=%.0fM (debt_field=%s cash_field=%s)",
                ticker, net_debt_usd / 1e6, revenue_usd / 1e6, debt_field, cash_field,
            )
        out["net_debt"] = {
            "value": net_debt_usd / 1_000_000.0,
            "source": "fmp",
            "field": f"{debt_field} - {cash_field}",
            "raw_value": net_debt_usd,
            "raw_unit": "USD",
            "as_of": as_of,
            "confidence": 0.9,
            "evidence": f"FMP balance sheet: {debt_field} minus {cash_field}.",
        }

    # --- FCFF margin (Tier B but high-quality from statement components) ---
    components = _compute_fcff_components(income, cashflow)
    if (
        components is not None
        and isinstance(revenue_raw, (int, float))
        and float(revenue_raw) != 0.0
    ):
        margin = float(components["fcff"]) / float(revenue_raw)
        out["fcff_margin"] = {
            "value": margin,
            "source": "fmp",
            "field": "(freeCashFlow + interestExpense*(1-tax)) / revenue",
            "raw_value": margin,
            "raw_unit": "ratio",
            "as_of": as_of,
            "confidence": 0.85,
            "evidence": (
                "FMP unlevered FCF margin: FCF + after-tax interest, divided by revenue."
            ),
        }
        if (
            components.get("tax_rate") is not None
            and isinstance(components["tax_rate"], (int, float))
        ):
            out["tax_rate"] = {
                "value": float(components["tax_rate"]),
                "source": "fmp",
                "field": "incomeTaxExpense / incomeBeforeTax",
                "raw_value": float(components["tax_rate"]),
                "raw_unit": "ratio",
                "as_of": as_of,
                "confidence": 0.85,
                "evidence": "FMP effective tax rate from income statement.",
            }
    elif (
        isinstance(cashflow.get("freeCashFlow"), (int, float))
        and isinstance(revenue_raw, (int, float))
        and float(revenue_raw) != 0.0
    ):
        margin = float(cashflow.get("freeCashFlow", 0.0)) / float(revenue_raw)
        out["fcff_margin"] = {
            "value": margin,
            "source": "fmp",
            "field": "freeCashFlow / revenue",
            "raw_value": margin,
            "raw_unit": "ratio",
            "as_of": as_of,
            "confidence": 0.6,
            "evidence": "FMP FCF margin (proxy: components for full FCFF unavailable).",
        }

    # --- profile metadata ---
    profile_meta: dict[str, Any] = {}
    if isinstance(profile, dict) and profile:
        profile_meta = {
            "sector": profile.get("sector"),
            "industry": profile.get("industry"),
            "market_cap_usd": profile.get("marketCap") or profile.get("mktCap"),
            "spot_price": profile.get("price"),
            "currency": profile.get("currency"),
            "company_name": profile.get("companyName"),
        }
    if profile_meta:
        out["__profile_meta__"] = profile_meta

    # --- extras (beta, debt, cash, interest, market cap) ---
    extras: dict[str, Any] = {}
    interest_raw = income.get("interestExpense")
    if isinstance(interest_raw, (int, float)) and interest_raw >= 0:
        extras["interest_expense_usd"] = float(interest_raw)
    debt_b = balance.get("totalDebt")
    if isinstance(debt_b, (int, float)) and debt_b >= 0:
        extras["total_debt_usd"] = float(debt_b)
    cash_b = balance.get("cashAndCashEquivalents")
    if isinstance(cash_b, (int, float)) and cash_b >= 0:
        extras["cash_usd"] = float(cash_b)
    if isinstance(market_cap, (int, float)) and market_cap > 0:
        extras["market_cap_usd"] = float(market_cap)

    km_rows = _fmp_get_json(
        f"key-metrics?symbol={ticker}&period=annual&limit=1",
        api_key,
    )
    km = km_rows[0] if km_rows else {}
    beta_km = km.get("beta") if isinstance(km, dict) else None
    beta_val = beta_km if isinstance(beta_km, (int, float)) else None
    if beta_val is None and isinstance(profile.get("beta"), (int, float)):
        beta_val = float(profile["beta"])
    if beta_val is not None and 0 < beta_val <= 2.5:
        extras["beta"] = float(beta_val)

    if extras:
        out["__dcf_extras__"] = extras

    return out


# ---------------------------------------------------------------------------
# yfinance fallback fetcher
# ---------------------------------------------------------------------------


def _fetch_fundamentals_yfinance(ticker: str) -> dict[str, dict[str, Any]]:
    """Pull canonical Tier A fundamentals from yfinance, normalized to millions.

    Each entry carries the fully resolved value plus provenance metadata.
    """
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except Exception as exc:  # noqa: BLE001
        logger.warning("DCF fundamentals: yfinance unavailable error=%s", exc)
        return {}

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("DCF fundamentals fetch failed ticker=%s error=%s", ticker, exc)
        return {}

    as_of = time.strftime("%Y-%m-%d")
    out: dict[str, dict[str, Any]] = {}

    shares_raw = info.get("sharesOutstanding")
    if isinstance(shares_raw, (int, float)) and shares_raw > 0:
        out["shares_outstanding"] = {
            "value": float(shares_raw) / 1_000_000.0,
            "source": "yfinance.info",
            "field": "sharesOutstanding",
            "raw_value": float(shares_raw),
            "raw_unit": "shares",
            "as_of": as_of,
            "confidence": 0.95,
            "evidence": "Canonical share count from yfinance.",
        }

    revenue_raw = info.get("totalRevenue")
    if isinstance(revenue_raw, (int, float)) and revenue_raw > 0:
        out["base_revenue"] = {
            "value": float(revenue_raw) / 1_000_000.0,
            "source": "yfinance.info",
            "field": "totalRevenue",
            "raw_value": float(revenue_raw),
            "raw_unit": "USD",
            "as_of": as_of,
            "confidence": 0.95,
            "evidence": "Trailing 12-month revenue from yfinance.",
        }

    debt_raw = info.get("totalDebt")
    cash_raw = info.get("totalCash")
    if isinstance(debt_raw, (int, float)) or isinstance(cash_raw, (int, float)):
        debt = float(debt_raw) if isinstance(debt_raw, (int, float)) else 0.0
        cash = float(cash_raw) if isinstance(cash_raw, (int, float)) else 0.0
        net_debt_usd = debt - cash
        out["net_debt"] = {
            "value": net_debt_usd / 1_000_000.0,
            "source": "yfinance.info",
            "field": "totalDebt - totalCash",
            "raw_value": net_debt_usd,
            "raw_unit": "USD",
            "as_of": as_of,
            "confidence": 0.85,
            "evidence": "Net debt computed from yfinance balance sheet snapshot.",
        }

    fcf_raw = info.get("freeCashflow") or info.get("operatingCashflow")
    if (
        isinstance(fcf_raw, (int, float))
        and isinstance(revenue_raw, (int, float))
        and revenue_raw
    ):
        margin = float(fcf_raw) / float(revenue_raw)
        out["fcff_margin"] = {
            "value": margin,
            "source": "yfinance.info",
            "field": "freeCashflow / totalRevenue",
            "raw_value": margin,
            "raw_unit": "ratio",
            "as_of": as_of,
            "confidence": 0.6,
            "evidence": "FCF margin proxy from yfinance (FCF over revenue).",
        }

    extras_yf: dict[str, Any] = {}
    beta_yf = info.get("beta")
    if isinstance(beta_yf, (int, float)) and 0 < float(beta_yf) <= 2.5:
        extras_yf["beta"] = float(beta_yf)
    mcap_yf = info.get("marketCap")
    if isinstance(mcap_yf, (int, float)) and float(mcap_yf) > 0:
        extras_yf["market_cap_usd"] = float(mcap_yf)
    interest_yf = info.get("interestExpense")
    if isinstance(interest_yf, (int, float)) and float(interest_yf) >= 0:
        extras_yf["interest_expense_usd"] = float(interest_yf)
    if isinstance(debt_raw, (int, float)) and float(debt_raw) >= 0:
        extras_yf["total_debt_usd"] = float(debt_raw)
    if isinstance(cash_raw, (int, float)) and float(cash_raw) >= 0:
        extras_yf["cash_usd"] = float(cash_raw)

    if extras_yf:
        out["__dcf_extras__"] = extras_yf

    return out


# ---------------------------------------------------------------------------
# hydrate_fundamentals node
# ---------------------------------------------------------------------------


def hydrate_fundamentals_node(state: dict) -> dict:
    """Pull canonical Tier A fundamentals and classify into a sector/size profile.

    Also builds the ``features`` vector for downstream CAPM-style WACC
    estimation.
    """
    from .state import DCFState  # noqa: PLC0415

    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    ticker = state["ticker"]
    emit_step("hydrate_fundamentals", "start", parent_step_id, {"ticker": ticker})

    fundamentals_fmp = _fetch_fundamentals_fmp(ticker)
    fundamentals_yf = _fetch_fundamentals_yfinance(ticker)

    extras_fmp = dict(fundamentals_fmp.pop("__dcf_extras__", {}))
    extras_yf = dict(fundamentals_yf.pop("__dcf_extras__", {}))
    dcf_extras = _merge_dcf_extras(extras_fmp, extras_yf)

    profile_meta = dict(fundamentals_fmp.pop("__profile_meta__", {}))

    fundamentals = dict(fundamentals_fmp)
    for field, meta in fundamentals_yf.items():
        fundamentals.setdefault(field, meta)

    provider = "fmp"
    if not fundamentals_fmp and fundamentals_yf:
        provider = "yfinance"
    elif fundamentals_fmp and fundamentals_yf:
        provider = "fmp+fallback:yfinance"
    elif not fundamentals:
        provider = "none"

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

    if fundamentals:
        logger.info(
            "DCF hydrate_fundamentals ticker=%s provider=%s profile=%s fields=%s",
            ticker,
            provider,
            profile,
            sorted(fundamentals.keys()),
        )
    else:
        logger.warning(
            "DCF hydrate_fundamentals ticker=%s: no canonical fundamentals available",
            ticker,
        )

    fields_summary = {
        field: {"value": meta["value"], "source": meta["source"], "as_of": meta.get("as_of")}
        for field, meta in fundamentals.items()
    }
    emit_step(
        "hydrate_fundamentals",
        "complete" if fundamentals else "fallback",
        parent_step_id,
        {
            "fields": sorted(fundamentals.keys()),
            "provider": provider,
            "fundamentals": fields_summary,
            "profile": profile,
            "profile_meta": profile_meta,
            "features_summary": sorted(features.keys()),
        },
    )
    return {
        "fundamentals": fundamentals,
        "profile": profile,
        "profile_meta": profile_meta,
        "features": features,
        "wacc_components": {},
    }
