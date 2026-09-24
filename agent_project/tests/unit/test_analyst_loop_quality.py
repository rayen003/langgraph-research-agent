from __future__ import annotations


def _financial_ref(ticker: str, revenue: int, net_income: int) -> dict:
    return {
        "kind": "tool_result",
        "payload": {
            "tool_name": "get_company_financials",
            "result_schema": "company_financial_history.v1",
            "ticker": ticker,
            "period": "annual",
            "result": {
                "ticker": ticker,
                "currency": "USD",
                "series": [{
                    "fiscal_year": "2025",
                    "revenue": revenue,
                    "net_income": net_income,
                }],
            },
        },
    }


def test_result_refs_hydrate_exact_financial_evidence(monkeypatch) -> None:
    import graphs.conversational as conversational

    refs = {
        "aapl_2025": _financial_ref("AAPL", 416_161_000_000, 112_010_000_000),
        "nvda_2025": _financial_ref("NVDA", 130_497_000_000, 72_880_000_000),
    }
    monkeypatch.setattr(conversational, "resolve_ref", lambda ref_id: refs[ref_id])

    evidence = conversational._compact_synthesis_evidence(
        {"result_refs": ["aapl_2025", "nvda_2025"]},
        "Compare Apple and NVIDIA financial performance for 2025",
    )

    assert [item["ticker"] for item in evidence] == ["AAPL", "NVDA"]
    assert evidence[0]["series"][0]["revenue"] == 416_161_000_000
    assert evidence[1]["series"][0]["net_income"] == 72_880_000_000


def test_quality_gate_rejects_generic_financial_report(monkeypatch) -> None:
    import graphs.conversational as conversational

    refs = {
        "aapl_2025": _financial_ref("AAPL", 416_161_000_000, 112_010_000_000),
        "nvda_2025": _financial_ref("NVDA", 130_497_000_000, 72_880_000_000),
    }
    monkeypatch.setattr(conversational, "resolve_ref", lambda ref_id: refs[ref_id])
    turn = {"result_refs": list(refs), "artifact_refs": [], "artifact_paths": []}

    gaps = conversational._analyst_quality_gaps(
        ["report"],
        "Revenue: Detailed analysis showing revenue trends. Net income: Insights into profitability.",
        turn,
        "Generate a document comparing NVIDIA and Apple financials for 2025",
    )

    assert "grounded_analysis" in gaps
    assert "report_depth" in gaps


def test_quality_gate_accepts_substantive_grounded_financial_report(monkeypatch) -> None:
    import graphs.conversational as conversational

    refs = {
        "aapl_2025": _financial_ref("AAPL", 416_161_000_000, 112_010_000_000),
        "nvda_2025": _financial_ref("NVDA", 130_497_000_000, 72_880_000_000),
    }
    monkeypatch.setattr(conversational, "resolve_ref", lambda ref_id: refs[ref_id])
    turn = {"result_refs": list(refs), "artifact_refs": [], "artifact_paths": []}
    report = (
        "Apple (AAPL) reported 2025 revenue of $416.2bn and net income of $112.0bn, "
        "while NVIDIA (NVDA) reported $130.5bn and $72.9bn respectively. "
        "Apple remained much larger by revenue, but NVIDIA converted a greater share of sales into profit. "
        "This reflects NVIDIA's high-margin accelerator mix and Apple's broader hardware and services base. "
        "On these reported figures, NVIDIA's net margin was about 55.8% versus Apple's 26.9%. "
        "Investors should compare fiscal calendars, segment concentration, cash conversion, and forward demand "
        "before treating headline growth or margins as directly comparable. Source figures come from structured "
        "annual company-financial results stored under the current analysis turn."
    )

    assert conversational._analyst_quality_gaps(
        ["report"], report, turn, "Compare Apple and NVIDIA financials for 2025"
    ) == []
