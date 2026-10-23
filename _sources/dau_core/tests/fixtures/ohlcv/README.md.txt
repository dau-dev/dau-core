# OHLCV golden fixtures

Generated files — do not edit by hand. Deterministic synthetic TAQ-shaped
trades plus their expected bars per the OHLCV bar specification
(dau-docs-private/OHLCV_BARS.md), consumed by polars-free tests
(`dau_core/tests/test_ohlcv_fixtures.py`) and future HDL benches.

Regenerate after any bar-spec change:

    PYTHONPATH=.../dau-core:.../dau-driver:.../dau:.../dau-polars \
        python -m dau_polars.ohlcv_fixtures dau-core/dau_core/tests/fixtures/ohlcv

| File | Content |
| --- | --- |
| `trades.csv` | raw synthetic TAQ trades (all decision-point cases) |
| `prepared_{regular,full}.csv` | host-prep output (dict codes, fixed point, sorted) |
| `bars_{regular,full}.csv` | expected bars per session mode |
