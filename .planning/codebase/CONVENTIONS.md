---
last_mapped_commit: 67e699dca6ad9c51903425039a8a2b0f78639465
last_mapped_at: 2026-06-11
---
# Coding Conventions

**Analysis Date:** 2026-06-11

## Naming Patterns

**Files:**
- Python scripts use lower snake case, often with domain nouns: `kline_batch.py`, `generate_signals.py`, `portfolio_backtest_realistic.py`.
- Versioned strategy files use suffixes such as `_v4`, for example `scripts/signal_engine_v4.py`.

**Functions:**
- Functions use lower snake case: `calc_rsi`, `calc_macd`, `fetch_kline`, `run_strategy`, `place_order`.
- Entry functions are usually `main()`, `run()`, `generate()`, `update_redis_prices()`, or `run_strategy()`.

**Variables:**
- Local variables are short lower-case names in numeric/backtest loops: `c`, `h`, `l`, `v`, `sc`, `ep`, `sl`, `pos`.
- Constants are upper snake case: `BUY_THRESHOLD`, `SELL_THRESHOLD`, `MAX_POSITIONS`, `PORTFOLIO_ID`, `STATE_FILE`.

**Types:**
- No custom classes or dataclasses are used.
- Dictionaries represent domain objects such as positions, trades, signals, quality payloads, and predictions.

## Code Style

**Formatting:**
- No formatter is configured.
- Style is compact procedural Python with many one-line guards such as `if len(closes) < 30: return None`.
- Several backtest scripts intentionally compress calculations to keep loops short.

**Linting:**
- No linting tool is configured.
- No `pyproject.toml`, `.flake8`, `ruff.toml`, or `mypy.ini` exists.

## Import Organization

**Order:**
1. Standard library imports first, usually combined on one line: `import subprocess, json, time`.
2. Local import for Feishu helper in `scripts/quantmind_strategy_runner.py`.
3. Third-party imports are sometimes delayed inside functions, for example `akshare` in `scripts/expand_hk_us.py` and `pandas`/`numpy` inside `scripts/quantmind_daily_pipeline.py`.

**Path Aliases:**
- None. There is no package import path or alias configuration.

## Error Handling

**Patterns:**
- Best-effort network fetchers often use broad `except:` and return `None` or `[]`.
- Parsing loops skip malformed rows with `try/except` and `continue`.
- API order placement returns a status dictionary on failure in `scripts/quantmind_strategy_runner.py`.
- Missing local state files are treated as empty state in `load_state()`.
- There is no project-level exception hierarchy or retry helper.

## Logging

**Framework:** console output.

**Patterns:**
- Several scripts define a local `log(msg)` helper using `datetime.now().strftime('%H:%M:%S')`.
- Backtest scripts print progress every N days or rows.
- Cron redirects script output to `/tmp/*.log` in `config/crontab.txt`.

## Comments

**When to Comment:**
- Comments are used heavily to explain trading rules, market assumptions, and operational steps.
- Script headers often summarize purpose and version changes.
- Inline comments document parameters such as slippage, fixed trade size, and stop-loss behavior.

**Docstrings:**
- Module-level docstrings are common.
- Function docstrings appear for larger functions such as `analyze_stock`, `fetch_tencent_kline`, and notification helpers.

## Function Design

**Size:**
- Indicator functions are small and focused.
- Orchestration functions are large: `run_strategy()` in `scripts/quantmind_strategy_runner.py` and `run()` in `scripts/signal_engine_v4.py` own many responsibilities.

**Parameters:**
- Indicator functions accept primitive lists: `closes`, `highs`, `lows`, `volumes`.
- Runtime functions mostly read globals rather than accepting config objects.
- SQL helpers accept raw SQL strings.

**Return Values:**
- Indicator helpers return numbers, tuples, or dictionaries.
- Fetch helpers return dictionaries, lists, scalar prices, or `None`.
- Backtest functions return lists/dictionaries of trades, NAV, and summary metrics.

## Module Design

**Exports:**
- There is no explicit public API. Files are intended to be executed as scripts.
- Standard entry guard is used: `if __name__ == '__main__':`.

**Barrel Files:**
- None. There is no package-level `__init__.py`.

## Domain Conventions

**Signal side:**
- Common signal states are `BUY`, `SELL`, and `HOLD`.
- Live v4 thresholds in `scripts/signal_engine_v4.py` are approximately `BUY >= 0.62` and `SELL <= 0.38`.
- Backtest thresholds are often `BUY = 0.65` and `SELL = 0.35`.

**Position sizing:**
- Live runner uses `POSITION_SIZE_PCT = 0.10` and `MAX_POSITIONS = 10`.
- Realistic backtest uses fixed trade size and maximum concurrent positions.
- HK lots are handled with symbol-specific or default lot sizes.

**Market detection:**
- `scripts/quantmind_strategy_runner.py` uses HKT time windows to select HK, US, or CLOSED.

---

*Convention analysis: 2026-06-11*
