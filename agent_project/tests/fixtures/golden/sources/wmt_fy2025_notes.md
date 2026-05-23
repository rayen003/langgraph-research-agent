# WMT FY2025 — Source Notes

Audit trail for `wmt_fy2025.json`. **Variant: low_margin_retail**.

## SEC 10-K — Walmart Inc, FY ended Jan 31, 2025 (filed Mar 14, 2025)

- Main doc: https://www.sec.gov/Archives/edgar/data/0000104169/000010416925000021/wmt-20250131.htm
- FY25 Q4 earnings release: https://www.sec.gov/Archives/edgar/data/0000104169/000010416925000010/earningsreleasefy25q4.htm

### Income statement (FY2025)

| Item | Value ($M) |
|------|-----------|
| Total revenues | 680,985 |
| Net income | 20,200 |
| Diluted weighted-average shares | 8,024 |
| **Effective tax rate** | **23.4%** |

### Cash flow statement (FY2025)

| Item | Value ($M) |
|------|-----------|
| Cash from operating activities | 36,400 |
| **Free cash flow** | **12,700** |
| CapEx (derived: OCF − FCF) | 23,700 |
| **FCFF margin (FCF/Revenue)** | **1.87%** |

Classic retail economics: razor-thin margin × massive scale. CapEx heavy due to store remodels + automation + e-commerce fulfillment.

### Balance sheet (Jan 31, 2025)

| Item | Value ($M) |
|------|-----------|
| Cash and equivalents | 9,037 |
| Total debt (long-term + current portion) | 45,800 |
| **Net debt** | **+36,763** (positive) |

Walmart doesn't hoard cash — earnings deployed into capex + buybacks + dividends. Maintains modest leverage for capital structure efficiency.

## Damodaran — Retail (General), Jan 2026

| Metric | Value |
|--------|-------|
| Rf | 4.18% |
| Implied ERP | 4.23% |
| Industry beta | 0.81 |
| Cost of equity | 7.54% |
| After-tax cost of debt | 3.97% |
| D/(D+E) | 7.36% |
| E/(D+E) | 92.64% |
| **WACC** | **7.27%** |

Low-mid beta — consumer staple. WACC lower than tech but higher than beverages (KO 6.33%).

## Analyst consensus (May 2026)

| Metric | Value | Source |
|--------|-------|--------|
| 12mo price target (48 analysts) | $138.72 | TipRanks |
| Alt (56 analysts) median | $130.00 | Multiple |
| Alt (29 Strong Buy) | $134.48 | Various |
| Consensus rating | Buy / Strong Buy | Multiple |
| Revenue growth forecast | 3.83% p.a. | Simply Wall St |

Growth: using **4%** — slightly above 3.83% consensus for e-commerce upside (Walmart e-commerce growing 20%+ within total).

## Hand DCF

Inputs: Rev $681.0B, g 4%, margin 1.87%, WACC 7.27%, TGR 2.5%, net debt +$36.76B, shares 8,024M

- Y1 Rev: 680.985 × 1.04 = $708.22B
- Y5 Rev: 680.985 × 1.04⁵ = $828.50B
- Y5 FCFF: 828.50 × 0.0187 = $15.49B
- Terminal FCFF: 15.49 × 1.025 = $15.88B
- TV: 15.88 / (0.0727 − 0.025) = $332.93B
- PV(TV): $332.93 / 1.0727⁵ = $234.43B
- PV(CFs Y1-5): ~$55B
- EV ≈ $289B
- Equity = $289 − $36.76 = $252.5B
- **Implied / share: ~$31**

## Range justification

DCF → ~$31. Market trades ~$135. Analyst target $138.

The huge gap reflects:
- WMT trades at **multiple expansion** vs. pure FCF math
- E-commerce growth (Walmart Connect ad business, OneAtWMT subscription)
- Margin expansion thesis (high-margin ads + memberships add to thin retail margin)
- Defensive moat (largest US grocer, recession-resistant)
- 5yr FCFF margin (1.87%) is captures *current* retail economics; bull case sees 3-4% blended margin as ads/membership scale

Range [$20, $90]:
- Lower bound = bear case (no margin expansion, demographic headwinds)
- Upper bound = bull DCF with margin expansion to 3%
- Outside = math bug

**Lesson:** WMT demonstrates how pure 5yr FCFF model can't capture business model transitions. The DCF answer ($31) is "correct" given inputs but irrelevant because the inputs don't capture the multi-business reality. Comps analysis or SOTP would value better.
