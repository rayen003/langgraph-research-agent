# NVDA FY2025 — Source Notes

Audit trail for `nvda_fy2025.json`.

## SEC 10-K — NVIDIA Corp, FY ended Jan 26, 2025 (filed ~Mar 2025)

- Main doc: https://www.sec.gov/Archives/edgar/data/0001045810/000104581025000023/nvda-20250126.htm
- Q4 FY2025 press release: https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-fourth-quarter-and-fiscal-2025

### Income statement (FY2025)

| Item | Value ($M) |
|------|-----------|
| Total revenue | 130,497 (up 114% YoY) |
| Prior year (FY2024) revenue | 60,922 |
| **Effective tax rate (GAAP)** | **13.3%** |
| Diluted weighted-average shares | 24,804 (post 10-for-1 split June 2024) |

### Cash flow statement (FY2025)

| Item | Value ($M) |
|------|-----------|
| Cash from operating activities | 64,089 |
| CapEx | 3,236 |
| **Free cash flow** | **60,853** |
| **FCFF margin** | **46.63%** (software-like margins on hardware) |

### Balance sheet (Jan 26, 2025)

| Item | Value ($M) |
|------|-----------|
| Cash + marketable securities | 43,210 |
| Total debt | 10,270 |
| **Net debt** | **−32,940** (net cash) |

## Damodaran — Semiconductor, Jan 2026

| Metric | Value |
|--------|-------|
| Rf | 4.18% |
| Implied ERP | 4.23% |
| Industry beta | 1.52 |
| Cost of equity | 10.72% |
| After-tax cost of debt | 3.97% |
| D/(D+E) | 2.53% |
| E/(D+E) | 97.47% |
| **WACC** | **10.55%** |

Sample: 66 firms. Note minimal debt financing in semis.

## Analyst consensus (May 2026)

| Metric | Value | Source |
|--------|-------|--------|
| 12mo price target (61 analysts, S&P Global) | $275.31 | MarketBeat |
| Alt (30 analysts, Wall St) | $285.30 | WallStreetZen |
| Alt (37 analysts) | $276.95 | 247WallStreet |
| Rating | Strong Buy | All |
| House targets | GS/MS $250, BAC/Wedbush $275, Cantor $300 | fxopen |

Growth forecasts:
- 30.7% avg next 5 fiscal years
- 25% next 3 years
- Revenue forecasts FY27/FY28/FY29: $342B / $423B / $496B

Using **25%** for 5yr CAGR — biased conservative vs 30.7% consensus. Sustained 30%+ at $130B base implausible due to law-of-large-numbers; data center TAM not infinite.

## Hand DCF (high uncertainty case)

Inputs: Rev $130.5B, g 25%, margin 46.6%, WACC 10.55%, TGR 2.5%, net debt −$33B, shares 24,804M

- Y1 Rev: 130.5 × 1.25 = $163.1B
- Y5 Rev: 130.5 × 1.25⁵ = $398.2B
- Y5 FCFF: 398.2 × 0.466 = $185.5B
- Terminal FCFF: 185.5 × 1.025 = $190.2B
- TV: 190.2 / (0.1055 − 0.025) = $2,362B
- PV(TV): $2,362 / 1.1055⁵ = $1,430B
- PV(CF Y1-5): ≈ $400B (high growth front-loads)
- EV ≈ $1,830B
- Equity = $1,830 − (−33) = $1,863B
- **Implied / share: ~$75**

## Range justification

NVDA is **fundamentally difficult to DCF**. Conservative (g=25%) → ~$75. Aggressive bull (g=35%, margin=50%, WACC=9%) → ~$200.

Analyst consensus $280 implies sustained ~35%+ growth through Y5 + further terminal upside + multiple expansion → essentially impossible to capture in pure 5yr FCFF.

Range [$50, $300] — extra wide due to growth uncertainty. Failing this range means either math bug or assumptions wildly off (e.g., scale error). Passing tells us math handles a high-growth/high-margin profile correctly.

**Key lesson:** golden DCF tests work better for mature businesses than hypergrowth. NVDA serves to verify the math handles extreme inputs (large g, large margin) without overflow / division errors.
