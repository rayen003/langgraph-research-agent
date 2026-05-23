# MSFT FY2024 — Source Notes

Audit trail for `msft_fy2024.json`.

## SEC 10-K — Microsoft Corp, FY ended June 30, 2024 (filed July 30, 2024)

- Main doc: https://www.sec.gov/Archives/edgar/data/0000789019/000095017024087843/msft-20240630.htm
- Financial tables: https://www.sec.gov/Archives/edgar/data/0000789019/000095017024087843/Financial_Report.xlsx

### Income statement (FY2024)

| Item | Value ($M) |
|------|-----------|
| Total revenue | 245,122 |
| Income before provision for income taxes | 107,787 |
| Provision for income taxes | 19,651 |
| **Effective tax rate** | **18.23%** |
| Diluted weighted-average shares | 7,469 |

### Cash flow statement (FY2024)

| Item | Value ($M) |
|------|-----------|
| Cash from operating activities | 118,548 |
| Additions to property and equipment (CapEx) | 44,477 |
| **Free cash flow** | **74,071** |
| **FCFF margin** | **30.22%** |

### Balance sheet (June 30, 2024)

| Item | Value ($M) |
|------|-----------|
| Cash + ST investments | 75,543 |
| Total debt (incl. current portion) | 51,221 |
| **Net debt** | **−24,322** (net cash) |

## Damodaran — Software (System & Application), Jan 2026

| Metric | Value |
|--------|-------|
| Rf (10Y T-Bond) | 4.18% |
| Implied ERP | 4.23% |
| Industry beta | 1.28 |
| Cost of equity | 9.64% |
| After-tax cost of debt | 3.97% |
| D/(D+E) | 5.28% |
| E/(D+E) | 94.72% |
| **WACC** | **9.34%** |

Sample: 309 firms.

## Analyst consensus (May 2026)

| Metric | Value | Source |
|--------|-------|--------|
| 12mo price target (55 analysts, S&P Global) | $560.63 | MarketBeat |
| Alt consensus (32 analysts) | $564.84 | 247WallStreet |
| Alt consensus (38 analysts) | $569.73 | MarketScreener |
| Consensus rating | Strong Buy | Multiple |

Growth: not directly published as 5yr CAGR in search results. Using 12% conservatively — biased low vs Azure/Copilot tailwind but appropriate for terminal-year stability at $245B base.

## Hand DCF

Inputs: Rev $245.1B, g 12%, margin 30.2%, WACC 9.34%, TGR 2.5%, net debt −$24.3B, shares 7,469M

- Y1 Rev: 245.1 × 1.12 = $274.5B
- Y5 Rev: 245.1 × 1.12⁵ = $432.0B
- Y5 FCFF: 432.0 × 0.302 = $130.5B
- Terminal FCFF: 130.5 × 1.025 = $133.8B
- TV: 133.8 / (0.0934 − 0.025) = $1,953B
- PV(TV): $1,953 / 1.0934⁵ = $1,250B
- PV(CF Y1-5): ≈ $375B
- EV ≈ $1,625B
- Equity = EV − net debt = $1,625 − (−24.3) = $1,649B
- **Implied / share: ~$220**

## Range justification

Conservative DCF → ~$220. Bull case (g=15%, margin=33%, WACC=8.5%) → ~$380.
Analyst consensus $560 → market premium for Azure/Copilot multiple expansion.
Range [$180, $420] captures both bear-textbook and bull-DCF interpretations.
