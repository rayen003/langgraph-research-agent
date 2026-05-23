"""Serialize / restore workflow context across DCF HITL approval + fast path."""

from __future__ import annotations

from typing import Any


def build_hitl_snapshot(source: dict[str, Any]) -> dict[str, Any]:
    """Extract fields needed to complete valuation after user approval."""
    return {
        "ticker": source.get("ticker", "?"),
        "horizon_years": source.get("horizon_years", 5),
        "assumptions": source.get("assumptions") or {},
        "assumption_provenance": source.get("assumption_provenance") or {},
        "assumption_memo": source.get("assumption_memo"),
        "memo_proposals": source.get("memo_proposals") or {},
        "evidence_items": source.get("evidence_items") or [],
        "scenarios": source.get("scenarios") or [],
        "company_state": source.get("company_state"),
        "thesis": source.get("thesis"),
        "features": source.get("features") or {},
        "fundamentals": source.get("fundamentals") or {},
        "profile": source.get("profile", "default"),
        "profile_meta": source.get("profile_meta") or {},
        "wacc_components": source.get("wacc_components") or {},
    }


def apply_hitl_snapshot(state: dict[str, Any], hitl: dict[str, Any]) -> None:
    """Merge cached HITL snapshot into a DCF state dict (in place)."""
    if not hitl:
        return
    for key in (
        "scenarios",
        "company_state",
        "thesis",
        "features",
        "fundamentals",
        "profile",
        "profile_meta",
        "wacc_components",
    ):
        value = hitl.get(key)
        if value:
            state[key] = value

    evidence_items = hitl.get("evidence_items") or []
    if evidence_items:
        state["evidence_pack"] = {"items": evidence_items}

    memo = hitl.get("assumption_memo")
    if isinstance(memo, dict) and memo.get("proposals"):
        state["assumption_memo"] = memo
