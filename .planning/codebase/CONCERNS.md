---
last_mapped_commit: 67e699dca6ad9c51903425039a8a2b0f78639465
last_mapped_at: 2026-06-11
---
# Codebase Concerns

**Analysis Date:** 2026-06-11

## Tech Debt

**Duplicated indicator and scoring logic:**
- Issue: RSI, MACD, MA, ATR, Bollinger, Chandelier stop, and scoring rules are copied across live scripts and backtests.
- Files: `scripts/signal_engine_v4.py`, `scripts/generate_signals.py`, `backtest/backtest_trades.py`, `backtest/segment_backtest.py`, `backtest/portfolio_backtest_combined.py`, `backtest/portfolio_backtest_realistic.py`.
- Impact: Backtest behavior can silently diverge from live trading behavior.
- Fix approach: Extract shared strategy logic into a module and make live scripts/backtests import it.

**Config template is not the runtime source of truth:**
- Issue: `config/config.template.json` models database/API/Feishu/portfolio config, but most scripts use hard-coded module constants.
- Files: `config/config.template.json`, `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_sim_trader.py`, `scripts/quantmind_daily_pipeline.py`, `scripts/feishu_notify.py`.
- Impact: Environment changes require code edits, and secret rotation is unsafe.
- Fix approach: Add a config loader with required environment variables and fail-fast validation.

**Direct shell SQL and deployment coupling:**
- Issue: SQL is assembled inline and run through `docker exec quantmind-db psql`.
- Files: most scripts under `scripts/` and DB-backed scripts under `backtest/`.
- Impact: Hard to test, hard to move across environments, and vulnerable to quoting/injection problems.
- Fix approach: Centralize DB access and use parameterized queries through a Python database client.

**Backtests depend on untracked `/tmp` data:**
- Issue: CSV-backed backtests require files such as `/tmp/all_klines.csv`, `/tmp/hk_klines_v2.csv`, and `/tmp/us_klines.csv`.
- Files: `backtest/portfolio_backtest_combined.py`, `backtest/portfolio_backtest_realistic.py`.
- Impact: Results are not reproducible from the repo alone.
- Fix approach: Document dataset generation, add checksums/metadata, or provide a deterministic export command.

## Known Bugs

**Signal engine v4 syntax error:**
- Symptoms: `python -m compileall -q .` fails before `scripts/signal_engine_v4.py` can run.
- Files: `scripts/signal_engine_v4.py`.
- Trigger: Python parses the nested f-string at line 525.
- Workaround: Change the inner dictionary key quotes or assign `preds[3]` values before formatting.

**Missing runtime dependencies for daily pipeline:**
- Symptoms: The remote inline feature generator imports `sqlalchemy` and writes parquet, but `requirements.txt` does not list `sqlalchemy`, `pyarrow`, or `fastparquet`.
- Files: `scripts/quantmind_daily_pipeline.py`, `requirements.txt`.
- Trigger: Running feature generation in an environment without those packages.
- Workaround: Install missing packages in the remote container or add them to dependency management.

**Segment backtest Bollinger lower band calculation appears wrong:**
- Symptoms: `calc_bollinger()` in `backtest/segment_backtest.py` returns `ma-num_std*2` instead of `ma-num_std*std`.
- Files: `backtest/segment_backtest.py`.
- Trigger: Any scoring path using that lower band.
- Workaround: Fix the formula and re-run segment results.

## Security Considerations

**Credentials and sensitive defaults in source:**
- Risk: API passwords, Feishu application defaults, and operational targets can leak from a public repository.
- Files: `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_sim_trader.py`, `scripts/quantmind_daily_pipeline.py`, `scripts/feishu_notify.py`.
- Current mitigation: `.gitignore` excludes common secret files, but scripts still include sensitive fallback values.
- Recommendations: Move secrets to environment or ignored config, rotate exposed credentials, and remove fallback secrets from source.

**SQL injection and shell quoting risk:**
- Risk: Symbols, names, JSON, and dynamic values are interpolated directly into SQL and shell command strings.
- Files: `scripts/expand_hk_us.py`, `scripts/generate_signals.py`, `scripts/signal_engine_v4.py`, `scripts/quantmind_daily_pipeline.py`, `scripts/quantmind_sim_trader.py`, `scripts/quantmind_strategy_runner.py`.
- Current mitigation: Some strings replace single quotes, but this is inconsistent and not equivalent to parameterization.
- Recommendations: Use parameterized DB APIs and avoid `shell=True` for commands with interpolated content.

**Broad exception swallowing hides failures:**
- Risk: Quote fetch, DB update, notification, and parsing failures may go unnoticed while the strategy continues.
- Files: `scripts/kline_batch.py`, `scripts/update_portfolio_prices.py`, `scripts/quantmind_strategy_runner.py`, `scripts/signal_engine_v4.py`, and `backtest/*.py`.
- Current mitigation: Some progress logs exist.
- Recommendations: Log exception details with context, return non-zero for critical failures, and alert on data staleness.

## Performance Bottlenecks

**Per-symbol subprocess SQL:**
- Problem: Many loops call `docker exec ... psql` once per symbol or per update.
- Files: `scripts/signal_engine_v4.py`, `scripts/generate_signals.py`, `scripts/quantmind_strategy_runner.py`, DB-backed `backtest/*.py`.
- Cause: No persistent DB connection or bulk query abstraction.
- Improvement path: Fetch needed rows in bulk and update in batches using a Python DB client.

**Repeated remote commands:**
- Problem: Daily and sim-trader flows execute remote SSH commands with embedded SQL.
- Files: `scripts/quantmind_daily_pipeline.py`, `scripts/quantmind_sim_trader.py`.
- Cause: Remote orchestration is embedded in scripts rather than exposed through a stable service or job.
- Improvement path: Move remote jobs into deployable scripts on the server and trigger them with minimal parameters.

**Backtest lookup inefficiency:**
- Problem: Some backtests use repeated list scans or `all_d.index(...)` inside loops.
- Files: `backtest/segment_backtest.py`, `backtest/portfolio_backtest_combined.py`, `backtest/portfolio_backtest_realistic.py`.
- Cause: Prototype-style loops prioritize readability/speed of writing over algorithmic efficiency.
- Improvement path: Precompute date indexes and reuse shared data structures.

## Fragile Areas

**Trading runner:**
- Files: `scripts/quantmind_strategy_runner.py`.
- Why fragile: It combines session detection, API login, quote refresh, stop/take-profit logic, signal selection, order placement, cash sync, notification dedupe, and heartbeat in one long function.
- Safe modification: Add tests around extracted pure functions first, then split I/O adapters from decision logic.
- Test coverage: None automated.

**Signal engine v4:**
- Files: `scripts/signal_engine_v4.py`.
- Why fragile: It is the core signal writer, currently has a syntax error, and writes JSONB directly through formatted SQL.
- Safe modification: Fix syntax first, then add unit tests for indicator math and generated quality JSON.
- Test coverage: None automated.

**Backtest credibility:**
- Files: `backtest/*.py`, `results/*.json`, `README.md`.
- Why fragile: Multiple strategy variants and assumptions exist, and output generation depends on external/untracked data.
- Safe modification: Version the data snapshot metadata and align scoring with the live engine.
- Test coverage: Manual only.

## Scaling Limits

**Stock universe size:**
- Current capacity: README references about 242 HK stocks and 32 US stocks in current summaries.
- Limit: Per-symbol HTTP calls and per-symbol subprocess DB calls become slow as universe expands.
- Scaling path: Add batched DB operations, rate-limit aware quote fetching, and resumable update state.

**Operational reliability:**
- Current capacity: cron jobs run scripts directly.
- Limit: No job queue, retry policy, health checks beyond Redis heartbeat, or centralized logs.
- Scaling path: Add a scheduler/job runner, structured logs, and alerting for stale data/signals.

## Dependencies at Risk

**Tencent Finance endpoints:**
- Risk: Unofficial endpoints can change or throttle.
- Impact: K-line updates and price refresh fail.
- Migration plan: Add provider abstraction and fallback providers.

**AkShare:**
- Risk: Data schemas and endpoint behavior can change.
- Impact: `scripts/expand_hk_us.py` can break or write wrong exchange/name data.
- Migration plan: Pin versions and validate expected columns.

**Docker container names:**
- Risk: Code assumes `quantmind-db`, `quantmind-redis`, and `quantmind` names.
- Impact: Scripts fail in any differently named environment.
- Migration plan: Move container/host names into config.

## Missing Critical Features

**Automated tests:**
- Problem: No unit, integration, or smoke tests protect strategy behavior.
- Blocks: Safe refactoring and credible performance iteration.

**Schema/migration documentation:**
- Problem: The database schema is referenced but not defined in the repo.
- Blocks: Reproducing the system locally or onboarding new environments.

**Dry-run mode:**
- Problem: Trading scripts call real simulation API and DB/Redis systems directly.
- Blocks: Safe CI validation and local development.

## Test Coverage Gaps

**Indicator/scoring math:**
- What's not tested: RSI, MACD, ATR, Bollinger, support/resistance, hard rules, and score thresholds.
- Files: `scripts/signal_engine_v4.py`, `backtest/*.py`.
- Risk: Small changes alter trade behavior or invalidate backtest claims.
- Priority: High.

**Order and risk management:**
- What's not tested: position sizing, lot sizing, stop loss, take profit, market-mode gating, and notification dedupe.
- Files: `scripts/quantmind_strategy_runner.py`.
- Risk: Bad quantity/order decisions in simulation or production-like runs.
- Priority: High.

**External integration failure handling:**
- What's not tested: Tencent failures, QuantMind API errors, Feishu failures, SSH failures, and DB command failures.
- Files: `scripts/*.py`.
- Risk: Silent data staleness or missing alerts.
- Priority: Medium.

---

*Concerns audit: 2026-06-11*
