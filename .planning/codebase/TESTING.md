---
last_mapped_commit: 67e699dca6ad9c51903425039a8a2b0f78639465
last_mapped_at: 2026-06-11
---
# Testing Patterns

**Analysis Date:** 2026-06-11

## Test Framework

**Runner:**
- None configured.
- There is no `tests/` directory and no pytest/unittest configuration.

**Assertion Library:**
- None configured.

**Run Commands:**
```bash
python -m compileall -q .              # Current syntax/import smoke check
python backtest/portfolio_backtest_realistic.py   # Manual realistic portfolio backtest, requires /tmp CSV inputs
python backtest/portfolio_backtest_combined.py    # Manual long-history combined backtest, requires /tmp CSV input
python backtest/segment_backtest.py               # Manual DB-backed segmented backtest
python backtest/backtest_trades.py                # Manual DB-backed trade extraction
```

## Test File Organization

**Location:**
- No automated tests exist.
- Validation-style scripts live in `backtest/`.

**Naming:**
- Backtest files use descriptive names ending in `_backtest.py` or `backtest_*.py`.

**Structure:**
```text
backtest/
├── backtest_trades.py
├── portfolio_backtest_combined.py
├── portfolio_backtest_realistic.py
└── segment_backtest.py
```

## Test Structure

**Suite Organization:**
```python
# Observed pattern in backtest scripts:
# 1. Load CSV or DB data at module import time.
# 2. Define local indicator/scoring helpers.
# 3. Run a full simulation loop immediately.
# 4. Print summary and write /tmp output.
```

**Patterns:**
- Setup is implicit and environment-dependent.
- Assertions are not used.
- Outputs are human-readable prints plus JSON/CSV artifacts.
- Performance claims are captured manually in `README.md` and `results/*.json`.

## Mocking

**Framework:** none.

**Patterns:**
```python
# No mocking pattern exists.
# External systems are called directly:
# - docker exec quantmind-db psql
# - Tencent Finance HTTP endpoints
# - QuantMind API HTTP endpoints
# - Feishu Open API
# - SSH into the remote host
```

**What to Mock:**
- Quote fetchers in `scripts/kline_batch.py`, `scripts/update_portfolio_prices.py`, and `scripts/quantmind_strategy_runner.py`.
- Database access wrappers such as `db(sql)` and `db_query(sql)`.
- QuantMind API calls in `scripts/quantmind_strategy_runner.py` and `scripts/quantmind_sim_trader.py`.
- Feishu notification sending in `scripts/feishu_notify.py`.

**What NOT to Mock:**
- Pure indicator math once extracted into a shared module.
- Deterministic scoring rules over small in-memory OHLCV fixtures.

## Fixtures and Factories

**Test Data:**
```python
# Recommended future pattern:
sample_ohlcv = {
    "close": [10.0, 10.2, 10.1, 10.5],
    "high": [10.1, 10.3, 10.2, 10.6],
    "low": [9.9, 10.0, 9.95, 10.3],
    "volume": [1000, 1200, 900, 1500],
}
```

**Location:**
- No fixtures exist today.
- Recommended location for future fixtures: `tests/fixtures/`.

## Coverage

**Requirements:** none enforced.

**View Coverage:**
```bash
# No coverage command exists.
```

## Test Types

**Unit Tests:**
- Missing.
- Highest value starting point: extracted indicator functions (`calc_rsi`, `calc_macd`, `calc_atr`, `calc_bollinger`, support/resistance, score composition).

**Integration Tests:**
- Missing.
- Current manual equivalent is running scripts against live Docker/remote/API systems.

**E2E Tests:**
- Not used.
- A future dry-run strategy test could exercise: K-line fixture -> signal score -> candidate selection -> simulated order payload without calling external APIs.

## Common Patterns

**Async Testing:**
```python
# Not applicable. Code is synchronous.
```

**Error Testing:**
```python
# No automated error tests exist.
# Error handling is mainly broad exception swallowing and log output.
```

## Current Verification Findings

**Compile check:**
- Command run during mapping: `python -m compileall -q .`.
- Result: failed on `scripts/signal_engine_v4.py`.
- Error: unmatched nested f-string quotes at `scripts/signal_engine_v4.py:525`.

**Implication:**
- The v4 signal engine cannot currently be imported or executed by Python until that syntax error is fixed.
- Other files may have compiled before the run stopped, but the whole-repo check is not passing.

---

*Testing analysis: 2026-06-11*
