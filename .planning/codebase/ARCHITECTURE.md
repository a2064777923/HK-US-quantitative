<!-- refreshed: 2026-06-19 -->
# Architecture

**Analysis Date:** 2026-06-19

## System Overview

```text
┌─────────────────────────────────────────────────────────────┐
│                 Runtime Schedulers / Operators              │
│ `config/rt_signal_engine_v5.service`                        │
│ `config/hermes_v5_crontab.txt`                              │
│ `config/crontab.txt`                                        │
└───────────────┬─────────────────────────────┬───────────────┘
                │                             │
                ▼                             ▼
┌─────────────────────────────────┐ ┌─────────────────────────┐
│ Realtime v5 Signal Runtime       │ │ Daily v4 Compatibility  │
│ `scripts/rt_signal_engine_v5.py` │ │ `scripts/kline_batch.py`│
│ `config/rt_signal_watchlist.json`│ │ `scripts/signal_engine_`│
│ `config/rt_signal_strategy_`     │ │ `v4.py`                 │
│ `config.json`                    │ │ `scripts/quantmind_`    │
│                                  │ │ `strategy_runner.py`    │
└───────────────┬─────────────────┘ └────────────┬────────────┘
                │                                │
                ▼                                ▼
┌─────────────────────────────────────────────────────────────┐
│                File-Based Evidence / Event Bus               │
│ `/tmp/rt_signal_alerts.jsonl`, `/tmp/rt_signal_alert.json`   │
│ `/tmp/*_report.json`, `/tmp/*_inputs.json`, `/tmp/*.jsonl`   │
└───────────────┬─────────────────────────────┬───────────────┘
                │                             │
                ▼                             ▼
┌─────────────────────────────────┐ ┌─────────────────────────┐
│ Context, Health, Learning        │ │ Hermes Review Packet    │
│ `scripts/*_report.py`            │ │ `scripts/hermes_review_`│
│ `scripts/*_producer.py`          │ │ `packet.py`             │
│ `scripts/*_event_store.py`       │ │ `config/hermes_*`       │
└───────────────┬─────────────────┘ │ `.schema.json`           │
                │                   └────────────┬────────────┘
                ▼                                ▼
┌─────────────────────────────────────────────────────────────┐
│              Operator Notification / Gated Intake            │
│ `scripts/rt_alert_bridge.py`                                │
│ `scripts/rt_order_intake.py`                                │
│ `scripts/feishu_notify.py`                                  │
└───────────────┬─────────────────────────────────────────────┘
                ▼
┌─────────────────────────────────────────────────────────────┐
│               External Storage and Services                  │
│ PostgreSQL container `quantmind-db`                          │
│ Redis container `quantmind-redis`                            │
│ Tencent Finance, Alpaca paper, QuantMind API, Feishu         │
└─────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| Realtime v5 signal engine | Poll HK/US quotes, merge static watchlists with DB user-holdings overlay, score completed-daily history plus one realtime quote, de-duplicate session alerts, and write alert files. | `scripts/rt_signal_engine_v5.py` |
| v5 runtime config | Configure v5.5 thresholds, trigger overrides, market-breadth gating, realtime BUY alignment, and risk model values. | `config/rt_signal_strategy_config.json` |
| v5 watchlist config | Seed HK/US scan lists before DB user-holdings overlay. | `config/rt_signal_watchlist.json` |
| Hermes packet builder | Combine alerts, dry-run intake results, portfolio risk, health/readiness, source reliability, market/external/fundamental context, learning reports, and position review items into a compact review artifact. | `scripts/hermes_review_packet.py` |
| Hermes trade judgment schema | Define required advisory trade judgment fields and required context-review checklist for approve/reduce decisions. | `config/hermes_trade_judgment.schema.json` |
| Hermes position judgment schema | Define advisory position review judgment contract. | `config/hermes_position_judgment.schema.json` |
| Alert bridge | Read v5 alerts and Hermes packets, filter actionable notifications, optionally send Feishu messages, and run intake in notify/dry-run/simulation modes. | `scripts/rt_alert_bridge.py` |
| Order intake | Validate one alert or queue item, build a simulation/paper order plan, enforce readiness/Hermes/strategy/market/conflict/pilot gates, and submit only when execute mode and caps pass. | `scripts/rt_order_intake.py` |
| Portfolio and position context | Read simulation and user holdings, produce portfolio-risk and position-review context, and preserve DB `positions` as user-holdings source of truth. | `scripts/portfolio_report.py` |
| User holding tools | Read, quote, and manually maintain DB user holdings without submitting broker orders. | `scripts/read_positions.py`, `scripts/hk_realtime.py`, `scripts/us_realtime.py`, `scripts/trade_update.py` |
| Price snapshot updater | Refresh portfolio position valuation fields for simulation or user portfolios without changing quantities. | `scripts/update_portfolio_prices.py` |
| Daily K-line updater | Fetch completed daily HK/US K-lines and write `klines` rows. | `scripts/kline_batch.py` |
| Daily v4 signal engine | Compute `signal_v4` / `v4_full` scores and feature run metadata from completed daily K-lines. | `scripts/signal_engine_v4.py` |
| Legacy strategy runner | Manage legacy simulation positions and exits; new openings are disabled unless explicitly enabled. | `scripts/quantmind_strategy_runner.py` |
| Health/readiness reports | Convert report freshness, data quality, outcome evidence, cron wiring, simulation performance, and judgment audits into machine-readable gates. | `scripts/system_health_check.py`, `scripts/data_health_report.py`, `scripts/execution_readiness_report.py`, `scripts/source_reliability_report.py` |
| Event stores | Convert alert, judgment, intake, and outcome JSONL/state into dry-run or hash-confirmed DB persistence reports. | `scripts/rt_alert_event_store.py`, `scripts/hermes_judgment_event_store.py`, `scripts/rt_order_intake_event_store.py`, `scripts/rt_signal_outcome_event_store.py` |
| Strategy learning and promotion | Analyze outcomes and propose config/watchlist changes without applying them automatically. | `scripts/strategy_learning_report.py`, `scripts/strategy_review_report.py`, `scripts/strategy_config_proposal.py`, `scripts/strategy_config_promote.py`, `scripts/watchlist_diff_report.py`, `scripts/watchlist_promote.py` |
| Market and external context | Produce read-only macro, news, event, sentiment, fundamentals, market-regime, intraday, and source-trust reports for Hermes. | `scripts/external_market_context_producer.py`, `scripts/external_market_context_report.py`, `scripts/market_context_report.py`, `scripts/intraday_context_report.py`, `scripts/fundamentals_context_report.py` |
| Research and replay | Run historical backtests, local dataset builds, and v5 replay evidence. | `backtest/*.py`, `scripts/local_backtest_dataset.py`, `scripts/v5_local_replay_report.py` |
| Contract tests | Unit-test script functions, schemas, gates, report payloads, and safety behavior. | `tests/test_*.py` |

## Pattern Overview

**Overall:** Script-oriented event/report pipeline with fail-closed execution gates and file-based JSON contracts.

**Key Characteristics:**
- Use executable Python scripts in `scripts/` as both runtime entry points and importable modules for `tests/`.
- Treat v5 realtime alerting (`scripts/rt_signal_engine_v5.py`) plus Hermes packet review (`scripts/hermes_review_packet.py`) plus gated intake (`scripts/rt_order_intake.py`) as the current primary path.
- Keep the v4 daily path (`scripts/kline_batch.py`, `scripts/signal_engine_v4.py`, `scripts/quantmind_strategy_runner.py`) as compatibility data generation and legacy position management.
- Use `/tmp/*.json` and `/tmp/*.jsonl` as the local event/report bus between independent cron jobs.
- Use PostgreSQL as the durable trading/data store, usually through `docker exec quantmind-db psql`; use Redis only for legacy/cache-style runtime state.
- Prefer read-only reports and hash-confirmed apply/promote scripts for operational changes.
- Keep secrets out of tracked files; runtime credentials are expected through environment variables or server-local env files.

## Layers

**Scheduling and Deployment:**
- Purpose: Start long-running v5 polling and schedule report/maintenance jobs.
- Location: `config/rt_signal_engine_v5.service`, `config/hermes_v5_crontab.txt`, `config/crontab.txt`.
- Contains: systemd service definition, v5/Hermes cron template, legacy v4 cron template.
- Depends on: Linux cron/systemd, `/root` deployment paths, `/tmp` output paths, runtime env files.
- Used by: all production/server workflows.

**Market Data and Universe:**
- Purpose: Build stock universe, fetch daily/minute/realtime market data, and diagnose/repair K-line coverage.
- Location: `scripts/expand_hk_us.py`, `scripts/expand_universe_v2.py`, `scripts/kline_batch.py`, `scripts/intraday_kline_batch.py`, `scripts/minute_collector.py`, `scripts/kline_*`.
- Contains: Tencent Finance fetchers, Alpaca data fetch paths, AkShare universe expansion, source-granularity reports, dry-run repair plans.
- Depends on: public data endpoints, optional Alpaca data env names, PostgreSQL.
- Used by: v4 daily signals, v5 completed-daily context, health/source reports, backtests.

**Realtime Signal Layer:**
- Purpose: Convert watchlists, user-holdings overlay, completed daily history, and realtime quotes into alert events.
- Location: `scripts/rt_signal_engine_v5.py`.
- Contains: watchlist/config loading, user portfolio overlay, quote normalization/freshness checks, technical scoring, market-breadth gating, trigger policy, de-duplication, alert writing.
- Depends on: `config/rt_signal_watchlist.json`, `config/rt_signal_strategy_config.json`, PostgreSQL `positions` and `klines`, Tencent Finance quote endpoints.
- Used by: alert bridge, Hermes packet, outcome/learning reports, event stores.

**Evidence and Report Layer:**
- Purpose: Produce compact JSON evidence for Hermes and readiness gates.
- Location: `scripts/*_report.py`, `scripts/*_producer.py`, `scripts/*_ingest.py`.
- Contains: health, data quality, source reliability, portfolio risk, market context, event catalysts, sentiment, fundamentals, outcomes, alert quality, simulation performance, operator queue, cron audit.
- Depends on: `/tmp` report/input files, PostgreSQL, external public feeds, runtime env variables.
- Used by: `scripts/hermes_review_packet.py`, `scripts/execution_readiness_report.py`, `scripts/source_reliability_report.py`, operator review.

**Hermes Review Layer:**
- Purpose: Package evidence into advisory trade and position review work items, then audit Hermes judgments.
- Location: `scripts/hermes_review_packet.py`, `scripts/hermes_judgment_audit_report.py`, `scripts/hermes_position_judgment_audit_report.py`, `config/hermes_trade_judgment.schema.json`, `config/hermes_position_judgment.schema.json`.
- Contains: review item selection, dry-run intake context, compact source summaries, context digests, judgment templates, schema contracts, audit reports.
- Depends on: v5 alert queue, all relevant `/tmp/*_report.json` files, judgment JSONL files.
- Used by: `scripts/rt_alert_bridge.py`, `scripts/rt_order_intake.py`, readiness and learning reports.

**Notification and Execution-Gate Layer:**
- Purpose: Deliver operator notifications and guard simulation/paper order submission.
- Location: `scripts/rt_alert_bridge.py`, `scripts/rt_order_intake.py`, `scripts/feishu_notify.py`.
- Contains: alert filtering, packet eligibility checks, Feishu sending, alert dry-run, order plan sizing, broker/portfolio context, readiness/Hermes/strategy/pilot gates.
- Depends on: alert queue, Hermes packet, QuantMind simulation API env names, optional Alpaca paper env names, Feishu env names.
- Used by: cron notification jobs and manual dry-run/execute commands.

**Portfolio and Holdings Layer:**
- Purpose: Maintain user and simulation holdings context separate from broker execution.
- Location: `scripts/portfolio_report.py`, `scripts/read_positions.py`, `scripts/trade_update.py`, `scripts/update_portfolio_prices.py`, `scripts/sim_position_reconcile.py`.
- Contains: DB position reads, quote refresh, valuation updates, advisory position review, manual DB holding maintenance, simulation reconciliation plans.
- Depends on: PostgreSQL `positions`, `portfolios`, `sim_trades`, Tencent/US quotes, user/simulation portfolio env names.
- Used by: v5 monitoring overlay, Hermes packet, readiness gates, operator workflows.

**Persistence and Event Store Layer:**
- Purpose: Move file/state events toward durable DB records only after dry-run or hash-confirmed review.
- Location: `scripts/rt_alert_event_store.py`, `scripts/hermes_judgment_event_store.py`, `scripts/rt_order_intake_event_store.py`, `scripts/rt_signal_outcome_event_store.py`, `scripts/add_performance_indexes.sql`.
- Contains: schema-hash reports, apply modes, DB insert/update statements, index SQL.
- Depends on: PostgreSQL, JSONL/state/report files.
- Used by: learning, audit, durability, and performance workflows.

**Research and Test Layer:**
- Purpose: Validate strategy assumptions and protect script contracts.
- Location: `backtest/*.py`, `scripts/local_backtest_dataset.py`, `scripts/local_backtest_reliability_report.py`, `scripts/v5_local_replay_report.py`, `tests/test_*.py`.
- Contains: legacy portfolio backtests, local replay, report contract tests, gate tests, schema tests.
- Depends on: local CSVs under `/tmp`, PostgreSQL for some backtests, unittest.
- Used by: development verification and strategy evidence.

## Data Flow

### Primary Realtime v5 Alert Path

1. systemd starts the v5 loop from `config/rt_signal_engine_v5.service`, executing `scripts/rt_signal_engine_v5.py`.
2. v5 loads strategy policy from `config/rt_signal_strategy_config.json` through `load_strategy_config()` (`scripts/rt_signal_engine_v5.py:789`).
3. v5 loads static watchlists and overlays open DB user holdings through `load_watchlists()` and `user_holding_symbols()` (`scripts/rt_signal_engine_v5.py:863`, `scripts/rt_signal_engine_v5.py:395`).
4. v5 reads completed daily history from PostgreSQL through `db()` and fetches current quotes through market-specific quote functions (`scripts/rt_signal_engine_v5.py:1031`, `scripts/rt_signal_engine_v5.py:1041`, `scripts/rt_signal_engine_v5.py:1075`).
5. `TriggerEngine` scores triggers, applies confirmation/risk/market-breadth/realtime-alignment policy, and tags diagnostics vs execution candidates (`scripts/rt_signal_engine_v5.py:2167`).
6. `send_alert()` appends alert events to `/tmp/rt_signal_alerts.jsonl` and updates `/tmp/rt_signal_alert.json` (`scripts/rt_signal_engine_v5.py:2987`).
7. Report cron jobs in `config/hermes_v5_crontab.txt` refresh health, market, source, portfolio, outcome, learning, and readiness JSON files under `/tmp`.
8. `scripts/hermes_review_packet.py` builds `/tmp/hermes_signal_review_packet.json` from alert and report inputs (`scripts/hermes_review_packet.py:3982`, `scripts/hermes_review_packet.py:4421`).
9. `scripts/rt_alert_bridge.py` reads alert and packet files, filters actionable items, formats notification text, optionally sends Feishu, and can call alert-specific intake (`scripts/rt_alert_bridge.py:118`, `scripts/rt_alert_bridge.py:349`, `scripts/rt_alert_bridge.py:289`).
10. `scripts/rt_order_intake.py` validates the alert, builds an order plan, applies Hermes/readiness/strategy/market/conflict/pilot gates, and records dry-run or processed state (`scripts/rt_order_intake.py:1373`, `scripts/rt_order_intake.py:1431`, `scripts/rt_order_intake.py:1649`, `scripts/rt_order_intake.py:1914`).

### Hermes Evidence and Readiness Path

1. Read-only producer/report scripts write source JSON files, for example `scripts/external_market_context_producer.py`, `scripts/market_sentiment_producer.py`, and `scripts/fundamentals_context_producer.py`.
2. Report scripts normalize those inputs into schema-tagged reports such as `/tmp/external_market_context_report.json`, `/tmp/market_sentiment_report.json`, and `/tmp/fundamentals_context_report.json`.
3. System and data reports evaluate infrastructure and market-data quality through `scripts/system_health_check.py` and `scripts/data_health_report.py`.
4. `scripts/source_reliability_report.py` classifies report freshness, source coverage, and provenance weaknesses across the context set.
5. `scripts/execution_readiness_report.py` combines system/data health, outcomes, portfolio risk, watchlist diff, alert quality, Hermes audits, simulation performance, cron wiring, and event-store status into `ready_for_execute`.
6. `scripts/hermes_review_packet.py` compacts all evidence into trade `review_items[]`, position review context, blocking reasons, and judgment templates.
7. `scripts/hermes_judgment_audit_report.py` and `scripts/hermes_position_judgment_audit_report.py` audit JSONL judgments against schemas, freshness, required context review flags, and packet/review identity.

### Legacy v4 Daily Path

1. Cron entries in `config/crontab.txt` and `config/hermes_v5_crontab.txt` run `scripts/kline_batch.py` for HK/US market windows.
2. `scripts/kline_batch.py` fetches completed daily K-lines, applies provider/window safeguards, and writes PostgreSQL `klines` rows.
3. `scripts/signal_engine_v4.py` computes `signal_v4` and `v4_full` rows into `engine_signal_scores` and `engine_feature_runs`.
4. `scripts/quantmind_strategy_runner.py` can manage legacy simulation exits and existing positions; new openings are gated off unless `QM_STRATEGY_ALLOW_NEW_POSITIONS` enables them.
5. v5 and Hermes reports still consume v4-derived data quality, daily history, and feature-run state as evidence.

### Holdings and Position Review Path

1. User holdings are DB `positions` rows for configured user portfolio IDs, read by `scripts/read_positions.py`, `scripts/portfolio_report.py`, `scripts/update_portfolio_prices.py`, and `scripts/rt_signal_engine_v5.py`.
2. `scripts/update_portfolio_prices.py` refreshes valuation fields only for the selected portfolio.
3. `scripts/portfolio_report.py` builds portfolio risk and advisory position-review items for Hermes.
4. `scripts/hermes_review_packet.py` adds position review items, context digests, and judgment templates.
5. `scripts/rt_alert_bridge.py` can notify operator position-review items without submitting orders.
6. Manual changes to user holdings go through `scripts/trade_update.py`, which mutates DB rows but does not submit broker orders.

### Research, Replay, and Promotion Path

1. Historical scripts in `backtest/` and replay scripts such as `scripts/v5_local_replay_report.py` produce research or replay reports.
2. `scripts/rt_signal_outcome_report.py`, `scripts/strategy_review_report.py`, and `scripts/strategy_learning_report.py` turn live/replay outcomes into policy evidence.
3. `scripts/strategy_config_proposal.py` and `scripts/watchlist_diff_report.py` produce proposed strategy/watchlist changes.
4. `scripts/strategy_config_promote.py`, `scripts/watchlist_promote.py`, and `scripts/stock_universe_hygiene_promote.py` apply only through explicit operator confirmation and proposal/hash checks.

**State Management:**
- Durable market/trade/account state lives in PostgreSQL tables accessed from scripts such as `scripts/rt_signal_engine_v5.py`, `scripts/portfolio_report.py`, and `scripts/trade_update.py`.
- Runtime alert and report exchange uses `/tmp/rt_signal_alerts.jsonl`, `/tmp/rt_signal_alert.json`, `/tmp/hermes_signal_review_packet.json`, `/tmp/*_report.json`, and `/tmp/*_inputs.json`.
- Idempotency and sent/reminder state use files such as `/tmp/rt_signal_state.json`, `/tmp/rt_order_intake_state.json`, `/tmp/rt_signal_sent.json`, and `/tmp/rt_position_review_sent.json`.
- Configured source files live under `config/`; deployed runtime copies are expected under `/root`.
- There is no central application server, message broker, ORM model layer, or migrations directory in this repo.

## Key Abstractions

**Realtime Alert:**
- Purpose: Represent one v5 signal, diagnostic WATCH row, or executable candidate with technical, timing, risk, and provenance fields.
- Examples: `scripts/rt_signal_engine_v5.py`, `scripts/rt_alert_bridge.py`, `scripts/rt_signal_outcome_report.py`.
- Pattern: JSON/JSONL event with schema-like field conventions rather than a Python class shared across modules.

**Hermes Review Packet:**
- Purpose: Provide a compact, reviewable evidence bundle for trade and position decisions.
- Examples: `scripts/hermes_review_packet.py`, `docs/HERMES_V5_INTEGRATION.md`.
- Pattern: report aggregator that reads many `/tmp` files, compacts them, and adds context digests and judgment templates.

**Execution Gate Contract:**
- Purpose: Fail closed before simulation/paper submission unless alert, strategy evidence, readiness, market context, Hermes judgment, broker context, symbol conflict, and pilot caps pass.
- Examples: `scripts/rt_order_intake.py`, `scripts/execution_readiness_report.py`, `config/hermes_trade_judgment.schema.json`.
- Pattern: ordered pure-ish gate functions returning payloads and blocker reasons, with dry-run as default.

**Report Contract:**
- Purpose: Let independent cron jobs exchange evidence through JSON payloads with `schema`, status, timestamps, recommendations, and text output.
- Examples: `scripts/data_health_report.py`, `scripts/source_reliability_report.py`, `scripts/execution_readiness_report.py`, `scripts/operator_action_queue_report.py`.
- Pattern: module-level constants for input/output paths, `build_report()`/`build_text_report()` functions, and a CLI `main()`.

**Hash-Gated Apply/Promote Contract:**
- Purpose: Keep repairs/promotions read-only by default and require operator confirmation for writes.
- Examples: `scripts/kline_daily_gap_repair.py`, `scripts/kline_source_granularity_report.py`, `scripts/watchlist_promote.py`, `scripts/strategy_config_promote.py`, `scripts/rt_alert_event_store.py`.
- Pattern: dry-run report contains a proposal/schema hash; apply mode requires matching confirmation.

**DB Helper:**
- Purpose: Execute SQL against the QuantMind PostgreSQL container.
- Examples: `db()` in `scripts/rt_signal_engine_v5.py`, `psql()` in `scripts/data_health_report.py`, direct `psycopg2` connection in `scripts/trade_update.py`.
- Pattern: local helper per script; no shared DB adapter or migration layer.

**Watchlist and Strategy Config:**
- Purpose: Externalize v5 scan universe and trigger/risk policy.
- Examples: `config/rt_signal_watchlist.json`, `config/rt_signal_strategy_config.json`, `scripts/strategy_config_proposal.py`, `scripts/strategy_config_promote.py`.
- Pattern: tracked template/current config copied to `/root` for runtime.

## Entry Points

**Realtime v5 service:**
- Location: `config/rt_signal_engine_v5.service`
- Triggers: systemd service.
- Responsibilities: run `scripts/rt_signal_engine_v5.py` continuously with runtime env loaded.

**Realtime v5 script:**
- Location: `scripts/rt_signal_engine_v5.py`
- Triggers: systemd or manual `python scripts/rt_signal_engine_v5.py`.
- Responsibilities: scan watchlists/holdings, fetch quotes, score triggers, write alert events.

**Hermes cron template:**
- Location: `config/hermes_v5_crontab.txt`
- Triggers: server crontab.
- Responsibilities: refresh read-only reports, build packets, notify operators, maintain price snapshots, and run dry-run event-store/report jobs.

**Alert bridge:**
- Location: `scripts/rt_alert_bridge.py`
- Triggers: cron or manual execution.
- Responsibilities: filter v5 alerts, attach packet context, notify Feishu/operator, and optionally run intake modes.

**Order intake:**
- Location: `scripts/rt_order_intake.py`
- Triggers: alert bridge or manual CLI with `--alert-json` / `--queue-file`.
- Responsibilities: validate, size, gate, dry-run, or submit simulation/paper orders.

**Hermes packet builder:**
- Location: `scripts/hermes_review_packet.py`
- Triggers: cron or manual CLI.
- Responsibilities: build compact trade/position review packet from alerts and report files.

**Health/readiness refresh:**
- Location: `scripts/readiness_refresh.py`
- Triggers: manual refresh or cron-adjacent operation.
- Responsibilities: run read-only evidence reports in dependency order.

**Daily data and v4 signal jobs:**
- Location: `scripts/kline_batch.py`, `scripts/signal_engine_v4.py`
- Triggers: cron, manual CLI.
- Responsibilities: refresh completed daily K-lines and daily signal rows.

**Portfolio/holding tools:**
- Location: `scripts/read_positions.py`, `scripts/trade_update.py`, `scripts/update_portfolio_prices.py`, `scripts/portfolio_report.py`
- Triggers: manual CLI and cron.
- Responsibilities: read holdings, update DB user holdings manually, refresh valuations, and generate Hermes portfolio context.

**Tests:**
- Location: `tests/test_*.py`
- Triggers: `python -m unittest discover -s tests`.
- Responsibilities: verify script contracts, schemas, gates, and report behavior.

## Architectural Constraints

- **Threading:** Most jobs are single-process scripts. `scripts/rt_signal_engine_v5.py` is a long-running polling loop; cron runs independent report jobs. There is no central async worker pool or queue service in the repo.
- **Global state:** Module-level env-derived constants and caches are common, for example `scripts/rt_order_intake.py`, `scripts/rt_alert_bridge.py`, `scripts/data_health_report.py`, `scripts/portfolio_report.py`, and `scripts/rt_signal_engine_v5.py`.
- **Circular imports:** No circular import chain is evident. Local imports are narrow and commonly guarded with fallback forms, for example `scripts/hermes_review_packet.py` importing `rt_order_intake`.
- **Namespace packaging:** `scripts/` has no `__init__.py`; tests rely on Python namespace-package behavior with imports such as `from scripts import rt_signal_engine_v5`.
- **Deployment paths:** Runtime cron/service files assume scripts are copied into `/root` and state/report files live under `/tmp`.
- **Database coupling:** Many scripts assume Docker container name `quantmind-db`, DB user/name defaults, and table schemas that are not defined as migrations in this repo.
- **File bus coupling:** Freshness and ordering depend on cron cadence and atomic writes to `/tmp` files, not on a broker or transaction coordinator.
- **Execution safety:** Notify/dry-run are default. Paper/simulation submission requires explicit mode, credentials, Hermes/reports, and pilot env gates.

## Anti-Patterns

### Monolithic Runtime Scripts

**What happens:** Large operational modules combine config parsing, IO, SQL, domain logic, report formatting, and CLI handling in one file, especially `scripts/rt_signal_engine_v5.py`, `scripts/hermes_review_packet.py`, `scripts/rt_order_intake.py`, `scripts/portfolio_report.py`, and `scripts/data_health_report.py`.
**Why it's wrong:** Changes to one rule can accidentally affect parsing, persistence, or output contracts, and small features require reading broad files.
**Do this instead:** Keep new behavior behind focused functions in the owning script and add matching tests in `tests/test_<module>.py`; extract shared helpers only when two or more modules need the same tested behavior.

### Repeated DB and JSON Helpers

**What happens:** `db()`, `psql()`, `run_cmd()`, `load_json_file()`, and `save_json_atomic()` patterns are copied across `scripts/*.py`.
**Why it's wrong:** Timeout behavior, error handling, SQL quoting, and atomic-write semantics drift between jobs.
**Do this instead:** Follow the local helper style in the target file for small changes; for cross-cutting changes, introduce a shared helper module under `scripts/` and migrate tests incrementally.

### Direct Shell SQL

**What happens:** Most database access is SQL strings sent through `docker exec quantmind-db psql`, for example `scripts/data_health_report.py`, `scripts/portfolio_report.py`, `scripts/kline_batch.py`, and `scripts/rt_signal_engine_v5.py`.
**Why it's wrong:** The code is tightly coupled to one deployment layout and makes parameterization/testing harder.
**Do this instead:** Use existing `sql_quote()` helpers where present, keep inputs constrained before interpolation, and add tests that assert generated SQL for write paths.

### Server Path Coupling

**What happens:** Runtime templates and scripts default to `/root` and `/tmp` paths.
**Why it's wrong:** Local development and alternate deployments need environment overrides or copied files.
**Do this instead:** Add env-overridable path constants near existing constants, update `config/*.txt` templates, and test the override path.

### Report Bus Without Dependency Enforcement

**What happens:** Reports read each other from `/tmp`, and stale or missing upstream files become warning/blocking payloads rather than scheduler-level dependencies.
**Why it's wrong:** Operators can see inconsistent evidence when cron timings drift.
**Do this instead:** Preserve report freshness fields and make downstream gates fail closed or explicitly degrade, following `scripts/source_reliability_report.py` and `scripts/execution_readiness_report.py`.

## Error Handling

**Strategy:** Operational scripts prefer fail-closed gates for execution paths and status-bearing JSON reports for evidence paths.

**Patterns:**
- Alert/order execution rejects unsafe conditions rather than assuming defaults, as in `scripts/rt_order_intake.py`.
- Report scripts convert missing/stale/invalid inputs into `WARN`, `FAIL`, blocker lists, or recommendations, as in `scripts/source_reliability_report.py` and `scripts/execution_readiness_report.py`.
- Many data fetchers and report readers continue with partial evidence and record warnings.
- Apply/promote paths require explicit hash/proposal confirmation in files such as `scripts/watchlist_promote.py` and `scripts/rt_alert_event_store.py`.
- Legacy and data scripts still contain broad exception handling and best-effort continuation; tests should pin behavior before tightening it.

## Cross-Cutting Concerns

**Logging:** Scripts print text reports or timestamped messages; cron templates redirect output to `/tmp/*.log`.
**Validation:** JSON payloads carry `schema` fields; Hermes judgment schemas live in `config/hermes_trade_judgment.schema.json` and `config/hermes_position_judgment.schema.json`; tests assert many report contracts under `tests/`.
**Authentication:** Feishu, QuantMind API, Alpaca, and portfolio credentials are loaded from environment variables or server-local env files. Tracked files list env var names only.
**Observability:** Health, readiness, source reliability, data inventory, cron audit, event-store, outcome, learning, and operator queue reports provide the main operational visibility.
**Configuration:** Runtime policy is split across tracked `config/*.json`, env variables, and deployment copies under `/root`.
**Testing:** Use `python -m unittest discover -s tests`; add targeted tests for any changed script contract or safety gate.

---

*Architecture analysis: 2026-06-19*
