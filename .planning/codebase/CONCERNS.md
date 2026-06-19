# Codebase Concerns

**Analysis Date:** 2026-06-19

## Tech Debt

**Duplicated strategy math across live engines, reports, and backtests:**
- Issue: RSI, MACD, ATR, Bollinger, moving-average, score, and risk geometry logic exists in multiple independently maintained scripts.
- Files: `scripts/signal_engine_v4.py`, `scripts/rt_signal_engine_v5.py`, `scripts/generate_signals.py`, `backtest/backtest_trades.py`, `backtest/segment_backtest.py`, `backtest/portfolio_backtest_combined.py`, `backtest/portfolio_backtest_realistic.py`, `scripts/v5_local_replay_report.py`.
- Impact: Backtest, v4 daily signals, v5 realtime alerts, and reports can drift even when each script has tests. The existing `scripts/factor_contract_alignment_report.py` detects some alignment, but it does not replace a shared implementation.
- Fix approach: Extract indicator math, score construction, factor contribution normalization, and risk geometry into a shared strategy module. Keep compatibility wrappers in `scripts/signal_engine_v4.py` and `scripts/rt_signal_engine_v5.py`, then test shared behavior directly.

**Database access is repeated and partly shell-driven:**
- Issue: Many scripts define their own `db()` or `psql()` helpers and run PostgreSQL through `docker exec ... psql`; other scripts use `psycopg2` directly with a separate connection path.
- Files: `scripts/data_health_report.py`, `scripts/data_source_inventory_report.py`, `scripts/kline_batch.py`, `scripts/portfolio_report.py`, `scripts/rt_signal_engine_v5.py`, `scripts/signal_engine_v4.py`, `scripts/update_portfolio_prices.py`, `scripts/system_health_check.py`, `backtest/backtest_trades.py`, `backtest/segment_backtest.py`.
- Impact: Error handling, timeout handling, quoting, table-column introspection, and environment overrides vary by script. New DB-backed code is likely to copy an existing helper and add another variant.
- Fix approach: Add a shared DB adapter with parameterized execution, structured row parsing, timeout handling, and Docker/client selection. Migrate high-risk writers first: `scripts/signal_engine_v4.py`, `scripts/update_portfolio_prices.py`, `scripts/rt_alert_event_store.py`, `scripts/rt_order_intake_event_store.py`, and `scripts/hermes_judgment_event_store.py`.

**Runtime configuration is split across tracked config, env vars, cron, systemd, and hard-coded defaults:**
- Issue: `config/config.template.json` is not the runtime source of truth; active scripts read dozens of environment variables, tracked cron files source server-only env files, and some legacy scripts still carry code-level API/user defaults.
- Files: `config/config.template.json`, `config/hermes_v5_crontab.txt`, `config/rt_signal_engine_v5.service`, `config/rt_signal_strategy_config.json`, `scripts/rt_order_intake.py`, `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_daily_pipeline.py`, `scripts/signal_engine_v4.py`.
- Impact: Operators can run different behavior locally, under cron, and under systemd. Secret rotation and environment audits require reading several files and scripts.
- Fix approach: Define one tracked runtime schema for non-secret config and one documented secret contract for env-only values. Fail fast when required secrets are missing, and remove code-level credential defaults from legacy scripts.

**Large script modules carry multiple responsibilities:**
- Issue: Several scripts combine CLI parsing, file I/O, DB queries, business rules, report generation, safety gates, and text formatting in one module.
- Files: `scripts/hermes_review_packet.py` (4268 lines), `scripts/rt_signal_engine_v5.py` (2905 lines), `scripts/rt_signal_outcome_report.py` (2070 lines), `scripts/rt_order_intake.py` (1745 lines), `scripts/hermes_judgment_audit_report.py` (1563 lines), `scripts/portfolio_report.py` (1493 lines), `scripts/operator_action_queue_report.py` (1467 lines), `scripts/data_health_report.py` (1299 lines).
- Impact: Even with tests, changes are expensive to review and easy to couple accidentally across report sections or execution gates.
- Fix approach: Split by stable domains: loaders/adapters, normalization, gate evaluation, report assembly, and CLI. Preserve current top-level script names as thin entry points.

**Legacy execution paths still exist beside the v5/Hermes path:**
- Issue: Older simulation and strategy scripts remain available, some with hard-coded remote/server assumptions. Current docs mark many of these as legacy or compatibility paths, but the files still look runnable.
- Files: `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_sim_trader.py`, `scripts/quantmind_daily_pipeline.py`, `scripts/sim_trade.py`, `docs/HERMES_V5_INTEGRATION.md`, `README.md`.
- Impact: An operator can accidentally revive behavior that bypasses the reviewed v5 alert, Hermes judgment, readiness, and intake gates.
- Fix approach: Keep `scripts/sim_trade.py` fail-closed, and add similar explicit fail-closed guards or deprecation banners to `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_sim_trader.py`, and `scripts/quantmind_daily_pipeline.py` unless intentionally maintained.

## Known Bugs

**Segment backtest lower Bollinger band formula is wrong:**
- Symptoms: `calc_bollinger()` returns the lower band as `ma - num_std * 2` instead of `ma - num_std * std`.
- Files: `backtest/segment_backtest.py`.
- Trigger: Any path using `score_stock()` in `backtest/segment_backtest.py`.
- Workaround: Use `scripts/v5_local_replay_report.py` for v5 replay research and do not treat `backtest/segment_backtest.py` output as authoritative until the formula is fixed and results regenerated.

**Daily pipeline depends on packages and remote container state not declared in repo requirements:**
- Symptoms: The inline remote feature generator imports SQLAlchemy and writes parquet, while `requirements.txt` lists pandas/numpy/requests/akshare/psycopg2/redis only.
- Files: `scripts/quantmind_daily_pipeline.py`, `requirements.txt`.
- Trigger: Running feature generation in a clean environment or a remote container without SQLAlchemy and a parquet backend.
- Workaround: Treat `scripts/quantmind_daily_pipeline.py` as a server-specific legacy helper. Add explicit dependency checks or move the remote code into a tracked, tested script with declared requirements.

**Root-level `NUL` file is present:**
- Symptoms: `rg --files` reports `NUL` at the repository root. `NUL` is a reserved device name on Windows and can cause tooling or archive portability issues.
- Files: `NUL`.
- Trigger: Windows filesystem tools, packaging, or sync tools that special-case reserved DOS device names.
- Workaround: Remove or rename the file after confirming it is not intentionally required.

## Security Considerations

**Credential-like defaults remain in source code:**
- Risk: Legacy scripts include code-level default usernames/passwords or DB credentials instead of requiring env-only values. No secret values should be copied from these files into documentation or logs.
- Files: `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_daily_pipeline.py`, `scripts/signal_engine_v4.py`, `scripts/rt_order_intake.py`, `scripts/quantmind_sim_trader.py`, `config/config.template.json`.
- Current mitigation: Current v5/Hermes docs direct operators to use `/root/.quantmind_env` and `/root/.env`; `.gitignore` excludes common local secret and key files; `scripts/feishu_notify.py` requires `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, and `FEISHU_CHAT_ID`.
- Recommendations: Remove fallback secret values, require env values for all API and DB authentication, rotate any credentials that were ever real, and keep `README.md` examples to env variable names only.

**SQL and shell quoting are not equivalent to parameterization:**
- Risk: SQL strings are assembled with f-strings and manual quote replacement; some scripts also execute constructed shell commands with `shell=True`.
- Files: `scripts/quantmind_daily_pipeline.py`, `scripts/quantmind_sim_trader.py`, `scripts/quantmind_strategy_runner.py`, `scripts/expand_hk_us.py`, `scripts/expand_universe_v2.py`, `scripts/update_portfolio_prices.py`, `scripts/rt_alert_bridge.py`, `backtest/backtest_trades.py`, `backtest/segment_backtest.py`.
- Current mitigation: Many DB-backed report scripts use `sql_quote()` and symbol normalization, and several writers use controlled generated values.
- Recommendations: Use parameterized DB APIs for values and identifier allowlists for table/column names. Avoid `shell=True`; pass SSH and command arguments as arrays or move remote work into tracked server scripts.

**Remote host and root execution assumptions are embedded:**
- Risk: Several scripts and cron templates assume root-owned paths, a root SSH target, and fixed Docker container names. This increases blast radius if commands are misconfigured or copied into another environment.
- Files: `scripts/quantmind_daily_pipeline.py`, `scripts/quantmind_sim_trader.py`, `scripts/rt_alert_bridge.py`, `config/hermes_v5_crontab.txt`, `config/rt_signal_engine_v5.service`, `README.md`.
- Current mitigation: `RT_ALERT_REMOTE=local` is the reviewed cron mode for the alert bridge, and many scripts expose env overrides such as `QM_DB_CONTAINER`.
- Recommendations: Keep remote/root execution disabled by default, centralize host/container names in non-secret config, and document the minimum privilege needed for each job.

**External notification and broker credentials are passed through process environments:**
- Risk: Feishu and Alpaca keys are read from environment variables and passed through bridge/intake paths; careless command logging or systemd/cron configuration can expose key names or values.
- Files: `scripts/rt_alert_bridge.py`, `scripts/rt_order_intake.py`, `scripts/rt_order_intake_event_store.py`, `scripts/feishu_notify.py`, `config/hermes_v5_crontab.txt`, `config/rt_signal_engine_v5.service`.
- Current mitigation: Feishu delivery is disabled by default in tracked cron; `scripts/cron_audit_report.py` checks missing Feishu config without printing values; `scripts/trusted_source_discovery_report.py` reports secret key presence with values redacted.
- Recommendations: Keep secret values out of command lines, avoid printing env files, and ensure cron/systemd logs cannot include secret payloads.

## Performance Bottlenecks

**Per-script and per-symbol DB subprocess calls:**
- Problem: Many loops call `docker exec ... psql` once per query or update. Some scripts write SQL scripts in bulk, but the pattern is not consistent.
- Files: `scripts/kline_batch.py`, `scripts/update_portfolio_prices.py`, `scripts/rt_signal_engine_v5.py`, `scripts/generate_signals.py`, `scripts/quantmind_strategy_runner.py`, `backtest/backtest_trades.py`, `backtest/segment_backtest.py`.
- Cause: There is no shared persistent DB client or batch query/update abstraction across the repo.
- Improvement path: Use persistent connections for Python DB access, batch reads by market/date/symbol set, and write bulk upserts through one shared helper.

**Backtests contain avoidable O(n^2) date lookups and scans:**
- Problem: Backtest loops repeatedly call `all_d.index(...)` or scan each symbol's rows for each date.
- Files: `backtest/segment_backtest.py`, `backtest/portfolio_backtest_combined.py`, `backtest/portfolio_backtest_realistic.py`.
- Cause: Prototype-style list scans are used instead of precomputed date indexes and symbol/date maps.
- Improvement path: Precompute `date -> index` and `(symbol, date) -> row` maps before the simulation loop. Prefer the structured replay approach in `scripts/v5_local_replay_report.py` for current v5 research.

**JSONL tail loaders read whole files:**
- Problem: Several scripts use `readlines()[-limit:]`, which reads entire alert/order/event files before slicing.
- Files: `scripts/alert_quality_report.py`, `scripts/event_catalyst_signal_report.py`, `scripts/rt_alert_bridge.py`, `scripts/rt_order_intake.py`, `scripts/rt_signal_outcome_report.py`, `scripts/strategy_learning_report.py`, `scripts/system_health_check.py`.
- Cause: Simplicity is favored over bounded tail reading.
- Improvement path: Add a shared bounded JSONL tail reader using `collections.deque(maxlen=limit)` or seek-based tailing for large files.

**Remote inline pipeline is heavyweight and serial:**
- Problem: `scripts/quantmind_daily_pipeline.py` performs remote SQL, per-symbol Tencent fetches, inline feature generation inside a Docker container, and remote inference from one script.
- Files: `scripts/quantmind_daily_pipeline.py`.
- Cause: Operational orchestration is embedded as shell strings rather than deployed as separate idempotent jobs.
- Improvement path: Split fetch, feature generation, and inference into tracked server-side scripts with explicit inputs/outputs and health reports.

## Fragile Areas

**Realtime signal engine:**
- Files: `scripts/rt_signal_engine_v5.py`, `tests/test_rt_signal_engine_v5.py`, `config/rt_signal_strategy_config.json`, `config/rt_signal_watchlist.json`.
- Why fragile: This is a long-running process that owns watchlist loading, user-holdings overlay, quote freshness, trigger detection, factor scoring, candidate downgrades, state persistence, and alert emission.
- Safe modification: Add tests in `tests/test_rt_signal_engine_v5.py` before changing trigger, score, risk geometry, or emission behavior. Preserve strict JSON output and completed-daily history contracts documented in `docs/HERMES_V5_INTEGRATION.md`.
- Test coverage: Strong unit coverage exists for many v5 cases, but module size still makes regression scope hard to audit.

**Order intake and alert bridge gates:**
- Files: `scripts/rt_order_intake.py`, `scripts/rt_alert_bridge.py`, `tests/test_rt_order_intake.py`, `tests/test_rt_alert_bridge.py`, `config/hermes_v5_crontab.txt`.
- Why fragile: These scripts combine safety gates, state locking, duplicate detection, broker context, Hermes judgment requirements, simulation/paper routing, and notification behavior.
- Safe modification: Keep default modes dry-run/notify-only. Any change to execution mode, gate defaults, sent-state behavior, or broker routing needs direct tests plus a `python -m unittest discover -s tests` run.
- Test coverage: Good unit coverage exists, but live safety also depends on cron env, `/tmp` state files, and fresh report inputs.

**Hermes review packet and judgment audits:**
- Files: `scripts/hermes_review_packet.py`, `scripts/hermes_judgment_audit_report.py`, `scripts/hermes_position_judgment_audit_report.py`, `tests/test_hermes_review_packet.py`, `tests/test_hermes_judgment_audit_report.py`, `tests/test_hermes_position_judgment_audit_report.py`.
- Why fragile: The packet/audit contract is broad and schema-like but implemented in Python dictionaries across large files.
- Safe modification: Treat report payload fields as public contracts. Add focused tests for any new field, acknowledgement requirement, recommendation, or status transition.
- Test coverage: Broad, but changes can still break downstream consumers in `scripts/operator_action_queue_report.py`, `scripts/strategy_learning_report.py`, and `scripts/execution_readiness_report.py`.

**Backtest and replay credibility:**
- Files: `backtest/*.py`, `scripts/local_backtest_dataset.py`, `scripts/local_backtest_reliability_report.py`, `scripts/v5_local_replay_report.py`, `scripts/v5_replay_strategy_review_report.py`, `results/*.json`.
- Why fragile: Legacy backtests, local CSV datasets, DB replay, and v5 trigger replay answer different questions. `scripts/v5_local_replay_report.py` explicitly reports research context rather than true intraday PnL reconstruction.
- Safe modification: Do not compare headline returns across these tools without dataset metadata, checksum validation, and strategy-contract notes.
- Test coverage: Unit tests cover local replay/report behavior, but the real research data remains local/untracked by design.

**Cron/systemd operational graph:**
- Files: `config/hermes_v5_crontab.txt`, `config/crontab.txt`, `config/rt_signal_engine_v5.service`, `scripts/cron_audit_report.py`, `scripts/cron_install_promote.py`, `scripts/system_health_check.py`.
- Why fragile: Many jobs communicate through `/tmp` JSON reports, JSONL queues, and state files. Missing or stale files can degrade downstream decisions without a type-level contract.
- Safe modification: Update `scripts/cron_audit_report.py` and tests whenever adding, renaming, or retiring a cron-produced report. Keep apply/promote scripts dry-run by default with explicit hash confirmation.
- Test coverage: Cron audit and promotion tests exist, but live correctness depends on actual crontab contents and file freshness.

## Scaling Limits

**Stock universe and quote polling:**
- Current capacity: Default v5 watchlist contains tracked HK and US symbols in `config/rt_signal_watchlist.json`, plus a DB user-holdings overlay from `scripts/rt_signal_engine_v5.py`.
- Limit: Quote fetching, alert evaluation, and report generation are mostly process-local and batch-size limited by public/unofficial endpoints.
- Scaling path: Add provider abstractions, quote batch limits/retries, and per-provider health metrics before materially increasing universe size.

**Report-file bus under `/tmp`:**
- Current capacity: The system passes many reports through `/tmp/*.json` and `/tmp/*.jsonl` files referenced by `config/hermes_v5_crontab.txt` and `scripts/source_reliability_report.py`.
- Limit: File growth, stale files, partial writes outside atomic helpers, and local disk cleanup can affect downstream gates.
- Scaling path: Keep atomic writes, add shared JSON/JSONL readers, rotate append-only queues, and consider a small event/report store for high-volume signals.

**Single-host Docker assumptions:**
- Current capacity: DB and service names default to `quantmind-db`, `quantmind-redis`, and `rt_signal_engine_v5`.
- Limit: Multi-host, non-root, or differently named deployments require many env overrides and cron edits.
- Scaling path: Centralize deployment configuration and provide an environment validation command that checks containers, service names, paths, and required reports.

## Dependencies at Risk

**Tencent Finance public endpoints:**
- Risk: HK/US K-line and realtime quote flows depend on public/unofficial Tencent endpoints and response layouts.
- Impact: Quote polling, daily K-line refresh, intraday context, local dataset building, and replay inputs can degrade or fail.
- Migration plan: Keep existing source reliability reports, add provider adapters with fallback ordering, and pin response-contract tests around parsers.

**Alpaca market data and paper trading APIs:**
- Risk: US dataset building and optional US paper routing require external API credentials and provider availability.
- Impact: `scripts/local_backtest_dataset.py`, `scripts/kline_batch.py`, `scripts/rt_order_intake.py`, and `scripts/rt_order_intake_event_store.py` can fail or downgrade when keys or endpoints are unavailable.
- Migration plan: Keep broker/data provider usage optional and fail-closed. Add explicit provider capability reports before enabling paper routing.

**AkShare data schemas:**
- Risk: Universe expansion scripts consume AkShare output columns that can change.
- Impact: `scripts/expand_hk_us.py` and related universe scripts can misclassify or fail to write stocks.
- Migration plan: Pin versions, validate required columns, and prefer `scripts/universe_hygiene_report.py` / `scripts/universe_rank_report.py` gates before promotion.

**Python dependency declarations:**
- Risk: `requirements.txt` does not cover every import used by server-inline code.
- Impact: Clean setup and CI-like environments can miss SQLAlchemy/parquet backends required by `scripts/quantmind_daily_pipeline.py`.
- Migration plan: Move inline remote code into tracked modules and declare all dependencies, or mark legacy scripts as server-only with explicit preflight checks.

## Missing Critical Features

**Shared application package:**
- Problem: Most business logic lives in executable scripts under `scripts/` with ad hoc imports.
- Blocks: Reuse, typed interfaces, narrower tests, and safe refactors across reports/engines.

**Schema and migration source of truth:**
- Problem: Scripts introspect DB columns and adapt to schema variants, but the repo does not contain a complete migration history for tables such as `klines`, `engine_signal_scores`, `engine_feature_runs`, `positions`, and event stores.
- Blocks: Reproducible local setup and reliable onboarding for new environments.

**Central env/config validation:**
- Problem: Required env keys are spread across scripts and docs.
- Blocks: Fast detection of missing `QM_*`, `RT_*`, Feishu, Alpaca, database, and report-file settings before cron/systemd execution.

**Bounded operational log/event retention:**
- Problem: Alert, order, judgment, and outcome JSONL files are append-only or tail-read by convention.
- Blocks: Predictable long-running performance and disk usage.

## Test Coverage Gaps

**Legacy scripts and old backtests:**
- What's not tested: `scripts/quantmind_daily_pipeline.py`, `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_sim_trader.py`, and most `backtest/*.py` simulation loops.
- Files: `scripts/quantmind_daily_pipeline.py`, `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_sim_trader.py`, `backtest/backtest_trades.py`, `backtest/segment_backtest.py`, `backtest/portfolio_backtest_combined.py`, `backtest/portfolio_backtest_realistic.py`.
- Risk: Legacy paths can break silently or produce misleading research if reused.
- Priority: High if these paths remain runnable; Low if they are formally fail-closed or deprecated.

**Shared DB and SQL behavior:**
- What's not tested: A single contract for DB quoting, parameterization, Docker/client selection, timeout behavior, and typed row parsing.
- Files: `scripts/*_event_store.py`, `scripts/data_health_report.py`, `scripts/portfolio_report.py`, `scripts/update_portfolio_prices.py`, `scripts/signal_engine_v4.py`, `scripts/rt_signal_engine_v5.py`.
- Risk: Each script can handle query failures differently, and SQL bugs can appear only under live DB state.
- Priority: High.

**End-to-end cron/systemd freshness:**
- What's not tested: Live execution order and freshness of the full `/tmp` report graph under real cron/systemd timing.
- Files: `config/hermes_v5_crontab.txt`, `config/rt_signal_engine_v5.service`, `scripts/cron_audit_report.py`, `scripts/system_health_check.py`, `scripts/source_reliability_report.py`, `scripts/execution_readiness_report.py`.
- Risk: Unit tests can pass while a deployed job graph is stale, missing env, or writing unexpected paths.
- Priority: Medium.

**Security regression checks:**
- What's not tested: Automated scanning for new code-level credentials, unsafe `shell=True` command construction, or accidental secret printing.
- Files: `scripts/*.py`, `config/*.txt`, `config/*.json`, `README.md`.
- Risk: A future change can reintroduce secret defaults or command-line secret exposure.
- Priority: Medium.

**Verification Snapshot:**
- Syntax check: `python -m compileall -q scripts backtest tests` passes.
- Unit suite: `python -m unittest discover -s tests` passes with 1023 tests.
- Scope note: No local `.env` files or obvious credential/key files were read during this mapping; secret-like values found in source were not copied into this document.

---

*Concerns audit: 2026-06-19*
