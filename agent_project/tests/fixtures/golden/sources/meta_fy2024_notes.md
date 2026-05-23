# META FY2024 — Source Notes

Audit trail for `meta_fy2024.json`.

## SEC 10-K — Meta Platforms, FY ended Dec 31, 2024 (filed Jan 30, 2025)

- Main doc: https://www.sec.gov/Archives/edgar/data/0001326801/000132680125000017/meta-20241231.htm
- Q4 2024 press release: https://www.sec.gov/Archives/edgar/data/1326801/000132680125000014/meta-12312024xexhibit991.htm

### Income statement (FY2024)

| Item | Value ($M) |
|------|-----------|
| Total revenue | 164,501 |
| Net income | 62,360 |
| Diluted EPS | $23.86 |
| Operating income | 69,380 |
| **Effective tax rate** | **12.0%** |
| Diluted weighted-average shares | 2,614 |

### Cash flow statement (FY2024)

| Item | Value ($M) |
|------|-----------|
| Cash from operating activities | 91,330 |
| CapEx (incl. principal payments on finance leases) | 39,230 |
| **Free cash flow** | **52,100** |
| **FCFF margin** | **31.67%** |

### Balance sheet (Dec 31, 2024)

| Item | Value ($M) |
|------|-----------|
| Cash + marketable securities | 77,810 |
| Long-term debt | 28,830 |
| **Net debt** | **−48,980** (net cash) |

## Damodaran — Software (Internet), Jan 2026

| Metric | Value |
|--------|-------|
| Rf | 4.18% |
| Implied ERP | 4.23% |
| Industry beta | 1.69 |
| Cost of equity | 11.48% |
| After-tax cost of debt | 3.97% |
| D/(D+E) | 10.95% |
| E/(D+E) | 89.05% |
| **WACC** | **10.66%** |

Sample: 29 firms (smaller than systems/app due to narrow definition).

## Analyst consensus (May 2026)

| Metric | Value | Source |
|--------|-------|--------|
| 12mo price target (37 analysts) | $834.57 | TipRanks |
| Avg target | $817.71 | MarketBeat |
| High / low | $1,015 / $622.25 | MarketBeat |
| Rating (38 analysts) | Buy | MarketBeat |

Growth: 15% p.a. next 3 years; 14% projected CAGR next 8 years. Using **14%** as 5yr midpoint.

## Hand DCF

Inputs: Rev $164.5B, g 14%, margin 31.67%, WACC 10.66%, TGR 2.5%, net debt −$49B, shares 2,614M

- Y1 Rev: 164.5 × 1.14 = $187.5B
- Y5 Rev: 164.5 × 1.14⁵ = $316.7B
- Y5 FCFF: 316.7 × 0.3167 = $100.3B
- Terminal FCFF: 100.3 × 1.025 = $102.8B
- TV: 102.8 / (0.1066 − 0.025) = $1,260B
- PV(TV): $1,260 / 1.1066⁵ = $760B
- PV(CF Y1-5): ≈ $280B
- EV ≈ $1,040B
- Equity = $1,040 − (−49) = $1,089B
- **Implied / share: ~$417**

## Range justification

Conservative DCF → ~$417. Bull case (g=18%, margin=35%, WACC=9%) → ~$650.
Analyst consensus $835 → market prices Reels monetization, WhatsApp Business, AI optionality.
Range [$280, $700].
