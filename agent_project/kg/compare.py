"""Side-chat over a cross-run DCF comparison artifact.

The frontend assembles a comparison (user picks N dcf_runs), computes a
structured diff, and sends it here with a question. The LLM reasons over the
STRUCTURED DIFF (not raw graph) — bounded context, no hallucinated numbers.

This is "cowork step 1": the agent discusses a specific analytical artifact the
user built, rather than answering open questions over the whole graph. Future
steps widen the context (chat history, session memory).
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_compare_llm() -> ChatOpenAI:
    """Lazy LLM (importing this module never requires an API key)."""
    return ChatOpenAI(
        model=os.getenv("KG_COMPARE_MODEL", os.getenv("KG_QUERY_MODEL", "gpt-4o-mini")),
        api_key=os.getenv("OPENAI_API_KEY"),
        timeout=45,
    )


# Schema describing the diff shape + DCF domain so the LLM reasons correctly.
_COMPARE_SCHEMA = """\
You are discussing a CROSS-RUN DCF COMPARISON the analyst assembled. You receive
a STRUCTURED DIFF — reason ONLY over it. Never invent numbers not present.

DCF DOMAIN NOTES:
- Assumptions are inputs (revenue_growth, wacc, fcff_margin, terminal_growth,
  tax_rate, base_revenue, net_debt, shares_outstanding, and terminal/SBC/buyback
  variants). Ratio fields are decimals (0.108 = 10.8%).
- Outputs are computed results (implied_share_price, equity_value,
  enterprise_value, terminal_pv, pv_cash_flows).
- Directional intuition (all else equal):
  • higher revenue_growth / fcff_margin → higher implied price
  • higher wacc → LOWER implied price (discount rate up)
  • higher terminal_growth → higher implied price
  • higher tax_rate → lower FCFF → lower implied price
- The runs are ordered oldest → newest. "What changed" = later vs earlier.

ANSWER STYLE:
- Tie assumption changes to output changes causally where the diff supports it.
- Quantify ("WACC +120bps, implied price −$18 / −9%").
- If the diff doesn't contain enough to answer, say so. Don't speculate beyond it.
- Concise analyst tone. No preamble."""


def _serialize_diff(diff: dict[str, Any]) -> str:
    """Render the structured diff payload as a compact text block for the LLM.

    Expected shape (built by the frontend KgCompareRuns):
      {
        "ticker": "AAPL",
        "runs": [{"run_id","label","date","trigger","parent_run_id"}...],  # oldest→newest
        "assumptions": [{"field","label","values":[v0,v1,...],"is_ratio":bool}...],
        "outputs":     [{"field","label","values":[...],"is_ratio":bool}...],
      }
    """
    ticker = diff.get("ticker", "?")
    runs = diff.get("runs") or []
    lines = [f"TICKER: {ticker}", f"RUNS ({len(runs)}, oldest→newest):"]
    for i, r in enumerate(runs):
        meta = []
        if r.get("date"):
            meta.append(str(r["date"]))
        if r.get("trigger") and r["trigger"] != "initial":
            meta.append(str(r["trigger"]))
        if r.get("parent_run_id"):
            meta.append(f"from {str(r['parent_run_id'])[:24]}")
        suffix = f" ({', '.join(meta)})" if meta else ""
        lines.append(f"  [{i}] {r.get('label', r.get('run_id', '?'))}{suffix}")

    def _fmt(v: Any, is_ratio: bool) -> str:
        if v is None:
            return "—"
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        if is_ratio:
            return f"{f * 100:.2f}%"
        if abs(f) >= 1e6:
            return f"{f:,.0f}"
        if abs(f) >= 1:
            return f"{f:.2f}"
        return f"{f:.4f}"

    def _section(title: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        lines.append(f"{title}:")
        for row in rows:
            is_ratio = bool(row.get("is_ratio"))
            vals = row.get("values") or []
            cells = " | ".join(_fmt(v, is_ratio) for v in vals)
            lines.append(f"  {row.get('label', row.get('field', '?'))}: {cells}")

    _section("ASSUMPTIONS (values per run, oldest→newest)", diff.get("assumptions") or [])
    _section("OUTPUTS (values per run, oldest→newest)", diff.get("outputs") or [])
    return "\n".join(lines)


def discuss_comparison(
    diff: dict[str, Any],
    question: str,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Answer a question about an assembled run comparison.

    ``history`` is optional prior turns: [{"role":"user"|"assistant","content":...}].
    Returns {"answer": str}.
    """
    serialized = _serialize_diff(diff)
    msgs: list[dict[str, str]] = [
        {"role": "system", "content": _COMPARE_SCHEMA},
        {"role": "system", "content": f"COMPARISON DIFF:\n{serialized}"},
    ]
    for turn in (history or [])[-6:]:  # keep recent context bounded
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": str(content)})
    msgs.append({"role": "user", "content": question})

    logger.info(
        "KG COMPARE-CHAT ticker=%s runs=%d q=%r",
        diff.get("ticker"), len(diff.get("runs") or []), question[:80],
    )
    try:
        resp = _get_compare_llm().invoke(msgs)
        answer = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception:  # noqa: BLE001
        logger.exception("compare-chat LLM call failed")
        return {"answer": "Couldn't reach the model. Try again."}
    return {"answer": answer.strip()}


__all__ = ["discuss_comparison"]
