# AAPL FY2024 — Source Notes

Audit trail for `aapl_fy2024.json`. Every field traceable to a URL.

## SEC 10-K — Apple Inc., FY ended Sept 28, 2024 (filed Nov 1, 2024)

- Filing index: https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm
- Main doc: https://www.sec.gov/Archives/edgar/data/0000320193/000032019324000123/aapl-20240928.htm
- Q4 2024 financials PDF: https://www.apple.com/newsroom/pdfs/fy2024-q4/FY24_Q4_Consolidated_Financial_Statements.pdf

### Income statement (FY2024)

| Item | Value ($M) |
|------|-----------|
| Total net sales | 391,035 |
| Net income | 93,736 |
| Income before provision for income taxes | 123,485 |
| Provision for income taxes | 29,749 |
| **Effective tax rate** | **24.09%** (29,749 / 123,485) |
| Diluted weighted-average shares | 15,408 ($M shares) |

### Cash flow statement (FY2024)

| Item | Value ($M) |
|------|-----------|
| Cash from operating activities | 118,254 |
| Payments for acquisition of PP&E (CapEx) | 9,447 |
| **Free cash flow (OCF − CapEx)** | **108,807** |
| **FCFF margin (FCF / Revenue)** | **27.83%** (108,807 / 391,035) |

### Balance sheet (as of Sept 28, 2024)

**Cash + marketable securities:**
| Item | Value ($M) |
|------|-----------|
| Cash and cash equivalents | 29,943 |
| Current marketable securities | 35,228 |
| Non-current marketable securities | 91,479 |
| **Total cash + investments** | **156,650** |

**Interest-bearing debt:**
| Item | Value ($M) |
|------|-----------|
| Commercial paper | 9,967 |
| Current portion of term debt | 10,912 |
| Non-current term debt | 85,750 |
| **Total debt** | **106,629** |

**Net debt** = 106,629 − 156,650 = **−50,021 ($M)** (net cash position)

Convention note: Treating LT marketable securities as cash-equivalent
investments (Apple manages them in tandem with cash). If excluded:
net debt = 106,629 − 65,171 = 41,458.

## Damodaran NYU — WACC by Industry (US, Jan 2026 update)

- Source: https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/wacc.html
- Implied ERP: https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/histimpl.html

### Computers/Peripherals industry (covers AAPL)

| Metric | Value |
|--------|-------|
| 10Y T-Bond rate (Rf) | 4.18% |
| Implied ERP | 4.23% |
| Industry beta | 1.35 |
| Cost of equity | 9.97% (= 4.18 + 1.35 × 4.23 ≈ 9.89, table reports 9.97) |
| Pre-tax cost of debt | 5.29% |
| After-tax cost of debt | 3.97% |
| D/(D+E) | 4.42% |
| E/(D+E) | 95.58% |
| **WACC** | **9.71%** |

Industry sample: 36 firms. AAPL beta specifically is ~1.24, slightly below
industry; using industry value for reproducibility.

## Analyst consensus (May 2026)

- Yahoo Finance: https://finance.yahoo.com/quote/AAPL/analysis/
- stockanalysis.com: https://stockanalysis.com/stocks/aapl/forecast/
- TipRanks: https://www.tipranks.com/stocks/aapl/forecast

| Metric | Value | Source |
|--------|-------|--------|
| 12-month price target (mean, 43 analysts) | $308.07 | TipRanks |
| 12-month price target alt (29 analysts) | $318.75 | stockanalysis |
| Consensus rating | Buy | All sources |
| Forward revenue CAGR (6yr) | ~10% | stockanalysis projected |
| Historical revenue CAGR (13yr) | ~8% | stockanalysis |

Using `revenue_growth = 0.07` — biased conservative vs. 10% forward, given
the 5-year horizon and law-of-large-numbers compression at AAPL's scale
($391B base). Analyst forwards typically optimistic.

## Hand DCF sanity (preview of expected output)

Inputs:
- Revenue $391B, g 7%, margin 27.83%, WACC 9.71%, TGR 2.5%, net debt -$50B, shares 15.408B

Y1-Y5 FCFF: 116.3 → 124.4 → 133.2 → 142.5 → 152.5 ($B)
PV(CF Y1-5): ≈ $504B
TV (Y5): 156.3 / (0.0971 − 0.025) = $2,168B
PV(TV): $2,168B / 1.0971⁵ = $1,362B
**EV**: ≈ $1,866B
**Equity**: 1,866 − (−50) = **$1,916B**
**Implied share price**: $1,916B / 15.408B = **~$124**

## Range justification

Hand DCF → ~$124. Analyst consensus → $308. Wide divergence is **expected**:

- Conservative textbook DCF (this record) doesn't price in:
  - Services growth premium (higher margin)
  - Continued buyback yield (denominator shrinks)
  - AI capex monetization optionality
  - Brand/moat premium baked into market multiple
- Bull-case DCF (analyst-aligned) would use:
  - g = 10%, margin = 30%, WACC = 8.5%, TGR = 3.0%
  - → implied ≈ $200-280

Expected range `[$80, $280]`:
- Lower bound catches conservative-textbook DCF
- Upper bound catches optimistic bull DCF
- Anything outside = math bug (e.g., scale error, missing terminal value,
  wrong discount factor)
