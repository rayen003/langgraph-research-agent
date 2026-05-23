# Golden Dataset

Hand-curated DCF records with full provenance. Each record encodes:
- Authoritative assumptions (from SEC filings + Damodaran + analyst consensus)
- Expected implied-price *range* (not point estimate — DCF inherently imprecise)
- Audit trail (source URL + page/section per field)

## Purpose

These records provide **external ground truth** for the DCF math pipeline.

Unit tests catch **regressions** (did the code change?). Golden tests catch
**errors** (does the model produce defensible numbers?).

A passing golden test means: given hand-vetted inputs from real filings, the
DCF math produces a price inside a range that any analyst would accept.

A failing golden test means: either the math has a bug, or the assumptions
need re-curation (filing updated, analyst consensus shifted, sector WACC changed).

## How to add a new ticker

1. **Copy `_template.json`** → `<ticker>_<fy>.json` (e.g. `msft_fy2024.json`).
2. **Extract Tier-A factuals** from the most recent 10-K (SEC EDGAR):
   - `base_revenue` — Income statement: Total net sales
   - `net_debt` — Balance sheet: Total debt (current term + non-current term + commercial paper) **minus** Cash + cash equivalents + ST marketable securities + (optionally) LT marketable securities
   - `shares_outstanding` — Income statement: Diluted weighted-average shares ($M)
   - `tax_rate` — Income statement: Provision for income taxes / Income before tax
3. **Derive `fcff_margin`** from the cash flow statement:
   - `(Operating cash flow − CapEx) / Total revenue`
4. **Fetch Damodaran WACC** for the ticker's industry from
   <https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/wacc.html>:
   - Look up the industry row → take the **WACC** column directly
   - Cost of equity = Rf + β × ERP (sanity check: should match the WACC table)
   - Use the **industry** WACC, not firm-specific — more reproducible
5. **Set `revenue_growth`** from analyst consensus (Yahoo Finance Analysis tab):
   - Use 5-year forward CAGR
   - For startups / no consensus → use ARR or revenue/employee derivation
6. **Set `terminal_growth`** by convention:
   - US: 2.5% (below long-run GDP)
   - EU: 2.0%
   - High-growth EM: 3.0%
   - Never exceed Rf
7. **Set `analyst_consensus_target`** from Yahoo Finance summary
   (12-month forward target mean across all covering analysts).
8. **Set `implied_share_price_range`** with **±50% around your hand-DCF**.
   Range should be:
   - Wide enough that DCF imprecision doesn't break the test
   - Narrow enough that a scale error (e.g. millions vs thousands) breaks it
   - Suggested: max(±50%, [0.6 × consensus, 1.3 × consensus])
9. **Sanity check by hand** before committing — compute implied price manually
   with the assumptions you wrote. It should land near the middle of the range.
10. **Run** `pytest agent_project/tests/golden/ -k <ticker>` to verify.

## Field source tiers

| Tier | Source | Stability |
|------|--------|-----------|
| `factual` | SEC 10-K | Stable for the fiscal year |
| `derived` | Computed from filing line items | Stable |
| `consensus` | Analyst aggregates | Updated monthly |
| `industry` | Damodaran / convention | Updated yearly |

## Variant handling

DCF assumptions differ by company stage:

- **Mature mega-cap**: use historical revenue + analyst consensus (this template)
- **High-growth tech**: revenue forecast often >20% — widen the range
- **Startups (pre-profit)**: replace `base_revenue` with ARR; `fcff_margin` may be
  the target steady-state margin, not current. Add a `model_variant: "startup"`
  field and document the substitution.
- **Cyclicals (energy, materials)**: use through-cycle normalized margin, not
  trailing — document with `fcff_margin.note = "5-yr through-cycle avg"`.

## Files

```
golden/
├── _schema.md          spec for the JSON shape
├── _template.json      blank — copy when adding tickers
├── aapl_fy2024.json    first golden record
└── sources/
    └── aapl_fy2024_notes.md   raw figures + URLs (audit trail)
```

## Re-validation cadence

- **Quarterly**: refresh `analyst_consensus_target` (cheap)
- **Annual**: refresh `wacc` (Damodaran updates each January) + new 10-K figures
- Never mutate a record in place — bump the filename (`aapl_fy2025.json`)
  so historical records remain reproducible.
