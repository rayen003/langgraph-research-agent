# DCF Test Suite

Pure-Python unit tests for the DCF workflow. **No LLM calls, no network.**

## Run

```bash
# From project root
.venv/bin/python -m pytest agent_project/tests/unit/ -v

# Quick:
.venv/bin/python -m pytest agent_project/tests/unit/ -q
```

## Layout

```
tests/
├── conftest.py                          # pytest fixtures (aapl_payload, etc.)
├── helpers.py                           # build_test_state(), build_test_payload()
├── fixtures/payloads/
│   ├── valid_aapl.json                  # captured live DCF run (regression baseline)
│   └── invalid_solver.json              # crafted failure case (solver divergence)
└── unit/
    ├── test_routing.py                  # all router functions
    ├── test_scenarios_validation.py     # monotonicity validator
    ├── test_deterministic_flags.py      # severity thresholds
    ├── test_consistency_checks.py       # EV reconciliation, TGR, evidence coverage
    ├── test_humanize_refs.py            # evidence ref formatting
    ├── test_sources.py                  # SourceRegistry, citations, Reference links
    ├── test_convergence_gate.py         # structural_gap vs invalid validity
    ├── test_report_export.py            # PDF/MD export
    ├── test_memo_validation.py          # assumption memo schema
    ├── test_kg_anchored.py              # KG cache anchoring
    ├── test_synthesis_lifecycle.py      # evidence synthesis lifecycle
    ├── test_initial_state.py            # DCFState factory
    ├── test_fcff_math.py                # FCFF projection + valuation math (hypothesis)
    ├── test_payload_invalid.py          # invalid banner, fixture contracts
    ├── test_priors.py                   # profile classify, bands, confidence scoring
    └── test_refine_assumptions.py       # bounded adjustment application
```

## What's tested

| Tier | Test type | Ground truth source |
|------|-----------|---------------------|
| 1 | Math identities | Accounting identity (EV = PV + TV_pv, etc.) |
| 2 | Spec-as-code | Threshold constants in source — catches regressions, not errors |
| 3 | Fixture contract | One real AAPL run — catches structural breakage |
| 4 | Property-based | Hypothesis 200 examples — catches edge cases |

## What's NOT tested

- **LLM nodes** (`formulate_thesis`, `propose_assumptions`, `semantic_synthesis`,
  `scenario_generator`, `analyze_result`'s LLM step) — require LangSmith evals.
- **External APIs** (yfinance, FMP, Tavily) — mocked or skipped.
- **Correctness** (is implied price *right*?) — requires golden dataset (P0 roadmap).

## Adding tests

1. **Pure deterministic function?** → add to `unit/`, use `build_test_state(...)`.
2. **Needs LLM?** → mark `@pytest.mark.llm`, runs only on demand.
3. **Needs network?** → mark `@pytest.mark.integration`, run via `pytest -m integration`.

## Refactor resistance

Tests deliberately assert **contracts not implementations**:
- Use `<=`/`<`/`in` not `==` for thresholds.
- Use `pytest.approx` for floats.
- Use `build_test_state(**overrides)` so adding state keys doesn't break every test.
- Don't assert exact strings — assert key substrings or keys-in-dict.

## Fixtures

`valid_aapl.json` is a **real captured run** (live AAPL DCF). It happens to have
`model_validity=invalid` because the live workflow detected a critical
divergence — that's real data, not a bug. The fixture is a regression baseline,
not a correctness anchor.

To recapture:
```bash
.venv/bin/python /tmp/capture_aapl.py
```
