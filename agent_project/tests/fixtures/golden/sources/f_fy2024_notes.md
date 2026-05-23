# F FY2024 — Source Notes

Audit trail for `f_fy2024.json`. **Variant: cyclical_industrial**.

## SEC 10-K — Ford Motor Co, FY ended Dec 31, 2024 (filed Feb 2025)

- Main doc: https://www.sec.gov/Archives/edgar/data/0000037996/000003799625000013/f-20241231.htm
- Annual report: https://www.sec.gov/Archives/edgar/data/37996/000110465925029103/tm259451d1_ars.pdf

### Income statement (FY2024)

| Item | Value ($M) |
|------|-----------|
| Total revenue | 184,992 |
| Net income | 5,900 |
| Adjusted EBIT | 10,200 |
| Diluted weighted-average shares | ~4,022 |
| **Effective tax rate** | **~15.0%** (estimated, auto industry typical 12-18%) |

### Cash flow statement (FY2024)

| Item | Value ($M) |
|------|-----------|
| Operating cash flow (consolidated) | 15,400 |
| **Adjusted free cash flow** (industrial) | **6,700** |
| **FCFF margin (FCF/Revenue)** | **3.62%** |
| CapEx (implied 2025 guide $8-9B) | ~$8,700 |

### Balance sheet — DUAL SEGMENT (industrial + Ford Credit)

This is the key complication for Ford. Two businesses:

**Industrial (auto manufacturing):**
| Item | Value ($M) |
|------|-----------|
| Cash + ST investments | ~28,500 |
| Industrial debt | ~20,000 |
| **Industrial net debt** | **−8,500** (net cash) |

**Ford Credit (financing arm):**
| Item | Value ($M) |
|------|-----------|
| Financing debt | ~137,000 |
| Financing receivables | ~137,000 (offsets) |

**Consolidated total debt: ~$157B** — but this is misleading because Ford Credit's debt is collateralized by receivables.

**Decision:** Use **industrial-only** net debt (−$8.5B) for this DCF. The FCF figure ($6.7B adjusted) is also industrial-only, so this is consistent. Treats Ford Credit as a separate business (sum-of-parts approach).

## Damodaran — Auto & Truck, Jan 2026

| Metric | Value |
|--------|-------|
| Rf | 4.18% |
| Implied ERP | 4.23% |
| Industry beta | 1.46 |
| Cost of equity | 10.45% |
| After-tax cost of debt | 3.97% |
| D/(D+E) | 16.45% |
| E/(D+E) | 83.55% |
| **WACC** | **9.38%** |

Higher beta + higher debt weight than tech. Reflects cyclicality.

## Analyst consensus (May 2026)

| Metric | Value | Source |
|--------|-------|--------|
| 12mo price target (13 analysts) | $13.41 | Benzinga |
| Alt (28 analysts) median | $11.10 | WallStreetZen |
| Range high / low | $16.00 / $9.50 | Various |
| Rating | Hold / Neutral | Mostly |
| Revenue growth forecast | −0.7% to +0.8% p.a. | Simply Wall St |

Growth: using **0%** — auto cyclical, EV transition headwinds, China competition, declining ICE share. Consensus is essentially flat.

## Hand DCF

Inputs: Rev $185B, g 0%, margin 3.62%, WACC 9.38%, TGR 2%, net debt −$8.5B (industrial), shares 4,022M

- Y1-Y5 Rev: $185B (flat)
- Y1-Y5 FCFF: $6.66B each
- Terminal FCFF: 6.66 × 1.02 = $6.79B
- TV: 6.79 / (0.0938 − 0.02) = $92.0B
- PV(TV): $92.0 / 1.0938⁵ = $58.7B
- PV(CFs Y1-5): $25.5B
- EV ≈ $84.2B
- Equity (industrial only) = $84.2 − (−$8.5) = $92.7B
- **Implied / share: ~$23**

## Range justification

DCF (industrial only) → ~$23. Market trades ~$11 → discount reflects:
- EV transition risk (lower margins, higher capex)
- Ford Credit drag on consolidated multiples
- China BYD competition
- Cyclical downturn risk

Range [$8, $35]:
- Lower bound = current market (~$11)
- Upper bound = optimistic DCF
- Outside this = math bug or wildly wrong assumptions

**Lesson:** Ford demonstrates DCF's blind spots for dual-segment financial-services businesses. Pure auto DCF over-values; consolidated DCF under-values. SOTP is the correct approach but beyond pure 5yr FCFF model.
