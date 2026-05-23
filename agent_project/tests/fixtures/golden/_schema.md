# Golden Record JSON Schema

Top-level keys (all required unless marked optional):

```json
{
  "ticker": "AAPL",                     // uppercase ticker
  "as_of": "2026-05-19",                // ISO date when record was assembled
  "fiscal_year": "FY2024",              // FY label of source filing
  "horizon_years": 5,                   // DCF projection horizon
  "model_variant": "mature_megacap",    // optional — startup / cyclical / etc.

  "assumptions": {
    "<field>": {
      "value": <number>,                // numeric value used by DCF engine
      "tier": "factual|derived|consensus|industry",
      "source": "string",               // human-readable source label
      "url": "string",                  // optional — URL for re-verification
      "note": "string"                  // 1-line explanation
    }
  },

  "expected_output": {
    "implied_share_price_range": {
      "low": <number>,                  // dollars per share
      "high": <number>
    },
    "analyst_consensus_target": <number>,
    "target_source": "string",
    "tolerance_rationale": "string"     // why this range, not tighter
  }
}
```

## Required assumption fields

The DCF math engine consumes exactly these:

| Field | Type | Unit |
|-------|------|------|
| `base_revenue` | number | $M, most recent FY |
| `revenue_growth` | number | fraction (0.07 = 7%) |
| `fcff_margin` | number | fraction |
| `wacc` | number | fraction |
| `terminal_growth` | number | fraction |
| `net_debt` | number | $M (negative = net cash) |
| `shares_outstanding` | number | million shares |
| `tax_rate` | number | fraction |

## Tier definitions

- **factual**: directly from a filing line item, no judgment
- **derived**: computed from filing line items by a fixed formula
- **consensus**: aggregated analyst expectation (changes monthly)
- **industry**: Damodaran sector data or industry convention (changes yearly)
