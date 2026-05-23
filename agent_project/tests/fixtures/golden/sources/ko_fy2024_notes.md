# KO FY2024 — Source Notes

Audit trail for `ko_fy2024.json`. **Variant: mature_consumer**.

## SEC 10-K — The Coca-Cola Company, FY ended Dec 31, 2024 (filed Feb 20, 2025)

- Main doc: https://www.sec.gov/Archives/edgar/data/0000021344/000002134425000011/ko-20241231.htm

### Income statement (FY2024)

| Item | Value ($M) |
|------|-----------|
| Net revenues | 47,061 |
| **Effective tax rate** | **18.6%** |
| Diluted weighted-average shares | 4,320 |

### Cash flow statement (FY2024) — ⚠ ANOMALY

| Item | Value ($M) |
|------|-----------|
| Reported OCF | 6,800 |
| CapEx | 2,200 |
| **Reported FCF** | **4,600** |
| Reported FCF margin | 9.8% |

**One-time item:** 2024 OCF was depressed by a ~$6B IRS settlement payment (related to long-running transfer pricing dispute). Excluding this, normalized OCF would be ~$12.8B and **normalized FCF ~$10.6B**, giving a normalized FCF margin of **~22%**.

**Decision:** Use **normalized 20% FCF margin** for golden DCF. The reported 9.8% is not representative of KO's underlying capital-light brand-licensing economics.

### Balance sheet (Dec 31, 2024)

| Item | Value ($M) |
|------|-----------|
| Cash + ST investments + marketable securities | 14,600 |
| Total debt | 44,522 |
| **Net debt** | **+29,922** (positive — KO uses leverage for buybacks/dividends) |

## Damodaran — Beverage (Soft), Jan 2026

| Metric | Value |
|--------|-------|
| Rf | 4.18% |
| Implied ERP | 4.23% |
| Industry beta | **0.64** (very low — defensive) |
| Cost of equity | 6.81% |
| After-tax cost of debt | 3.97% |
| D/(D+E) | 17.07% |
| E/(D+E) | 82.93% |
| **WACC** | **6.33%** |

Lowest WACC in the golden dataset. Reflects KO's defensive cash flow profile — beverages are essential, demand inelastic, brand moat = pricing power.

## Analyst consensus (May 2026)

| Metric | Value | Source |
|--------|-------|--------|
| 12mo price target (27 analysts) | $85.67 | TipRanks |
| Alt median (27 Wall St) | $85.00 | WallStreetZen |
| Range high / low | $92.00 / $76.00 | MarketBeat |
| Consensus rating | Strong Buy | Multiple |
| Revenue growth forecast | 3.77% p.a. | Simply Wall St |

Growth: using **4%** — biased slightly above 3.77% consensus to capture pricing power (KO consistently lifts prices 4-6% offset by mix).

## Hand DCF

Inputs: Rev $47.06B, g 4%, margin 20% (normalized), WACC 6.33%, TGR 2.5%, net debt +$29.92B, shares 4,320M

- Y1 Rev: 47.06 × 1.04 = $48.94B
- Y5 Rev: 47.06 × 1.04⁵ = $57.25B
- Y5 FCFF: 57.25 × 0.20 = $11.45B
- Terminal FCFF: 11.45 × 1.025 = $11.74B
- TV: 11.74 / (0.0633 − 0.025) = $306.4B
- PV(TV): $306.4 / 1.0633⁵ = $225.6B
- PV(CFs Y1-5): ~$42B
- EV ≈ $267.6B
- Equity = $267.6 − $29.9 = $237.7B
- **Implied / share: ~$55**

## Range justification

DCF → ~$55. Market trades ~$70. Analyst target $85.

The gap reflects:
- KO trades at premium for **defensive characteristics** (recession-resistant)
- **3%+ dividend yield** supports floor
- International growth optionality (developing markets)
- Brand moat warrants multiple expansion

Range [$35, $85]:
- Lower bound captures recession-pricing scenario (margin compression)
- Upper bound aligns with analyst bullish target
- Outside = math bug

**Lesson:** KO demonstrates how DCF handles normalized vs. one-off cash flows. Source notes document the IRS adjustment explicitly so the golden record uses defensible normalized inputs.
