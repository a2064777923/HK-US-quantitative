# Codebase Structure

**Analysis Date:** 2026-06-19

## Directory Layout

```text
F:/stock/
├── .planning/                 # GSD planning and generated codebase maps
│   └── codebase/              # Architecture, stack, testing, and concern maps
├── backtest/                  # Standalone legacy historical backtest scripts
├── config/                    # Runtime templates, JSON configs, schemas, cron, systemd
├── docs/                      # Human-readable strategy and Hermes integration notes
├── results/                   # Small checked-in backtest summary JSON artifacts
├── scripts/                   # Operational runtime scripts, reports, gates, producers
├── tests/                     # unittest contract tests for scripts and schemas
├── .gitignore                 # Ignore rules for caches, local env, logs, generated data
├── README.md                  # Current system overview and operator guidance
└── requirements.txt           # Python runtime dependency list
```

## Directory Purposes

**`.planning/`:**
- Purpose: GSD workflow state and codebase intelligence.
- Contains: generated planning files and `.planning/codebase/*.md` maps.
- Key files: `.planning/codebase/ARCHITECTURE.md`, `.planning/codebase/STRUCTURE.md`.

**`scripts/`:**
- Purpose: All runtime automation, report generation, signal generation, notification, order-intake, portfolio, and maintenance logic.
- Contains: directly executable Python scripts, one shell heartbeat helper, and one SQL index script.
- Key files: `scripts/rt_signal_engine_v5.py`, `scripts/hermes_review_packet.py`, `scripts/rt_alert_bridge.py`, `scripts/rt_order_intake.py`, `scripts/data_health_report.py`, `scripts/execution_readiness_report.py`, `scripts/portfolio_report.py`, `scripts/kline_batch.py`, `scripts/signal_engine_v4.py`.

**`config/`:**
- Purpose: Track non-secret runtime configuration, schema contracts, and deployment templates.
- Contains: JSON strategy/watchlist/session configs, JSON schemas, crontab templates, systemd service template, config template.
- Key files: `config/rt_signal_strategy_config.json`, `config/rt_signal_watchlist.json`, `config/hermes_trade_judgment.schema.json`, `config/hermes_position_judgment.schema.json`, `config/hermes_v5_crontab.txt`, `config/rt_signal_engine_v5.service`.

**`tests/`:**
- Purpose: Contract tests for script functions, schemas, report payloads, safety gates, and promotion/apply behavior.
- Contains: `unittest` files named after the covered script or feature.
- Key files: `tests/test_rt_signal_engine_v5.py`, `tests/test_rt_order_intake.py`, `tests/test_hermes_review_packet.py`, `tests/test_execution_readiness_report.py`, `tests/test_data_health_report.py`.

**`docs/`:**
- Purpose: Human guidance for scoring rules and v5/Hermes integration.
- Contains: Markdown notes.
- Key files: `docs/HERMES_V5_INTEGRATION.md`, `docs/scoring_logic.md`.

**`backtest/`:**
- Purpose: Legacy historical strategy research scripts.
- Contains: standalone Python scripts that load PostgreSQL or local CSV data and print/write performance summaries.
- Key files: `backtest/portfolio_backtest_realistic.py`, `backtest/portfolio_backtest_combined.py`, `backtest/segment_backtest.py`, `backtest/backtest_trades.py`.

**`results/`:**
- Purpose: Store curated, small backtest summary outputs.
- Contains: JSON summaries only.
- Key files: `results/realistic_backtest_summary.json`, `results/combined_backtest_summary.json`.

## Key File Locations

**Entry Points:**
- `scripts/rt_signal_engine_v5.py`: current realtime signal loop; started by `config/rt_signal_engine_v5.service`.
- `scripts/rt_alert_bridge.py`: operator/Feishu notification bridge and alert-intake launcher.
- `scripts/rt_order_intake.py`: alert-specific dry-run or simulation/paper order-intake gate.
- `scripts/hermes_review_packet.py`: Hermes review packet generator.
- `scripts/readiness_refresh.py`: manual read-only refresh of major evidence reports in dependency order.
- `scripts/kline_batch.py`: completed daily K-line refresh.
- `scripts/signal_engine_v4.py`: legacy daily v4 signal generation.
- `scripts/quantmind_strategy_runner.py`: legacy simulation position manager.
- `scripts/update_portfolio_prices.py`: portfolio valuation snapshot refresh.
- `scripts/trade_update.py`: manual DB user-holding maintenance.
- `backtest/*.py`: legacy backtest entry points.

**Configuration:**
- `config/rt_signal_strategy_config.json`: live v5.5 strategy/risk/trigger policy template.
- `config/rt_signal_watchlist.json`: tracked watchlist seed used by v5 runtime copies.
- `config/hermes_trade_judgment.schema.json`: trade judgment schema and context-review checklist.
- `config/hermes_position_judgment.schema.json`: position judgment schema.
- `config/hermes_v5_crontab.txt`: reviewed v5/Hermes cron template.
- `config/crontab.txt`: legacy v4 cron template.
- `config/rt_signal_engine_v5.service`: systemd service template for v5 loop.
- `config/intraday_market_sessions.json`: intraday session override config.
- `config/config.template.json`: non-secret example config structure.
- `requirements.txt`: Python dependency declaration.

**Core Logic:**
- `scripts/rt_signal_engine_v5.py`: v5 watchlist overlay, quote normalization, scoring, trigger policy, alert de-duplication, alert writes.
- `scripts/rt_order_intake.py`: alert validation, order planning, gate evaluation, pilot caps, QuantMind/Alpaca paper backend integration.
- `scripts/hermes_review_packet.py`: evidence aggregation, context digests, packet compaction, judgment templates.
- `scripts/rt_alert_bridge.py`: actionability filtering, packet eligibility checks, Feishu delivery, intake invocation.
- `scripts/portfolio_report.py`: portfolio risk and advisory position review generation.
- `scripts/data_health_report.py`: K-line/signal/source-quality and trade-relevant data-health checks.
- `scripts/execution_readiness_report.py`: cross-report execute-readiness gate.
- `scripts/source_reliability_report.py`: source freshness/provenance/reliability matrix.
- `scripts/kline_batch.py`: daily K-line ingestion and provider safety logic.
- `scripts/signal_engine_v4.py`: legacy daily signal scoring.

**Reports and Evidence:**
- `scripts/system_health_check.py`: system health report.
- `scripts/cron_audit_report.py`: cron wiring audit.
- `scripts/data_source_inventory_report.py`: data source visibility and coverage inventory.
- `scripts/kline_source_granularity_report.py`: K-line provenance report and hash-gated backfill.
- `scripts/intraday_context_report.py`: read-only intraday context.
- `scripts/intraday_timeframe_quality_report.py`: 5m/15m/30m/60m quality gate.
- `scripts/market_context_report.py`: market breadth/regime context.
- `scripts/external_market_context_report.py`: external news/macro/context report.
- `scripts/event_catalyst_report.py`: event catalyst context.
- `scripts/event_catalyst_signal_report.py`: event support/challenge for alerts.
- `scripts/market_sentiment_report.py`: sentiment context.
- `scripts/fundamentals_context_report.py`: fundamentals context.
- `scripts/rt_signal_outcome_report.py`: forward outcome evidence from alert events.
- `scripts/alert_quality_report.py`: alert quality report.
- `scripts/simulation_performance_report.py`: simulation performance and postmortem context.
- `scripts/operator_action_queue_report.py`: remediation priority queue.

**Producers and Ingesters:**
- `scripts/external_market_context_producer.py`: public RSS/InfoHub input production.
- `scripts/external_market_context_ingest.py`: append-only external context ingestion.
- `scripts/market_sentiment_producer.py`: market sentiment input production.
- `scripts/market_sentiment_ingest.py`: market sentiment input ingestion.
- `scripts/market_index_context_producer.py`: benchmark/index context input production.
- `scripts/fundamentals_context_producer.py`: fundamentals input production.
- `scripts/fundamentals_context_ingest.py`: fundamentals input ingestion.
- `scripts/minute_collector.py`: Tencent minute snapshot collection.

**Event Stores and Apply/Promote Tools:**
- `scripts/rt_alert_event_store.py`: alert event durability dry-run/apply.
- `scripts/hermes_judgment_event_store.py`: judgment durability dry-run/apply.
- `scripts/rt_order_intake_event_store.py`: order-intake decision durability dry-run/apply.
- `scripts/rt_signal_outcome_event_store.py`: outcome durability dry-run/apply.
- `scripts/watchlist_promote.py`: hash-confirmed watchlist promotion.
- `scripts/strategy_config_promote.py`: hash-confirmed strategy config promotion.
- `scripts/stock_universe_hygiene_promote.py`: hash-confirmed active-stock hygiene promotion.

**Portfolio and Holdings:**
- `scripts/read_positions.py`: read DB holdings.
- `scripts/hk_realtime.py`: quote HK holdings.
- `scripts/us_realtime.py`: quote US holdings.
- `scripts/trade_update.py`: manually maintain DB user holdings.
- `scripts/update_portfolio_prices.py`: update valuation snapshots.
- `scripts/sim_position_reconcile.py`: read-only simulation reconciliation plan.
- `scripts/sim_trade.py`: fail-closed compatibility guard.

**Testing:**
- `tests/test_<script>.py`: unit tests for one script or feature module.
- `tests/test_hermes_trade_judgment_schema.py`: schema validation for `config/hermes_trade_judgment.schema.json`.
- `tests/test_hermes_position_judgment_schema.py`: schema validation for `config/hermes_position_judgment.schema.json`.
- `tests/test_position_price_cron.py`: cron template coverage for portfolio price refresh.

## Naming Conventions

**Files:**
- Runtime scripts use lower snake case: `rt_signal_engine_v5.py`, `rt_order_intake.py`, `portfolio_report.py`.
- Read-only report scripts end with `_report.py`: `data_health_report.py`, `source_reliability_report.py`.
- Input-producing scripts end with `_producer.py`: `external_market_context_producer.py`, `fundamentals_context_producer.py`.
- Append-only input scripts end with `_ingest.py`: `market_sentiment_ingest.py`, `fundamentals_context_ingest.py`.
- Event durability scripts end with `_event_store.py`: `rt_alert_event_store.py`, `rt_signal_outcome_event_store.py`.
- Hash-confirmed promotion scripts end with `_promote.py`: `watchlist_promote.py`, `strategy_config_promote.py`.
- Repair/proposal scripts include the domain and action: `kline_daily_gap_repair.py`, `kline_gap_alternate_provider_repair_plan.py`.
- Tests mirror the target script as `tests/test_<module>.py`: `tests/test_rt_order_intake.py`.
- JSON schemas use `<domain>.schema.json`: `config/hermes_trade_judgment.schema.json`.

**Directories:**
- Top-level directories are lower-case and purpose-based: `scripts`, `tests`, `config`, `docs`, `backtest`, `results`.
- There is no `src/` directory and no package-specific subdirectories under `scripts/`.
- `scripts/` has no `__init__.py`; do not depend on package-only initialization side effects.

## Where to Add New Code

**New realtime signal rule or trigger policy:**
- Primary code: `scripts/rt_signal_engine_v5.py`.
- Config defaults or live policy: `config/rt_signal_strategy_config.json`.
- Tests: `tests/test_rt_signal_engine_v5.py`.
- Docs: update `docs/HERMES_V5_INTEGRATION.md` when the alert contract, evidence basis, or execution eligibility semantics change.

**New Hermes packet context:**
- Produce the source report in `scripts/<domain>_report.py` or `scripts/<domain>_producer.py`.
- Add compact packet integration in `scripts/hermes_review_packet.py`.
- Tests: `tests/test_hermes_review_packet.py` plus `tests/test_<domain>_report.py`.
- Docs: update `docs/HERMES_V5_INTEGRATION.md` if Hermes instructions or packet fields change.

**New execution gate or order-intake behavior:**
- Primary code: `scripts/rt_order_intake.py`.
- Bridge invocation changes: `scripts/rt_alert_bridge.py`.
- Tests: `tests/test_rt_order_intake.py` and `tests/test_rt_alert_bridge.py`.
- Config/cron changes: `config/hermes_v5_crontab.txt`.

**New read-only operational report:**
- Primary code: `scripts/<domain>_report.py`.
- CLI pattern: accept `--output` and optional `--text`, write JSON atomically, include a top-level `schema` and status.
- Tests: `tests/test_<domain>_report.py`.
- Cron: add only reviewed read-only scheduling to `config/hermes_v5_crontab.txt`.

**New producer/ingest input path:**
- Producer: `scripts/<domain>_producer.py`.
- Ingest append-only path: `scripts/<domain>_ingest.py`.
- Consumer report: `scripts/<domain>_report.py`.
- Runtime file defaults: use `/tmp/<domain>_inputs.json` or `/tmp/<domain>_inputs.jsonl` with env overrides.

**New DB repair or promotion workflow:**
- Dry-run/report code: `scripts/<domain>_report.py` or `scripts/<domain>_repair.py`.
- Apply/promote code: include proposal hash or schema hash confirmation, following `scripts/watchlist_promote.py`, `scripts/strategy_config_promote.py`, or `scripts/kline_source_granularity_report.py`.
- Tests: assert dry-run default and confirmation behavior in `tests/test_<domain>.py`.
- Cron: do not schedule write/apply modes by default in `config/hermes_v5_crontab.txt`.

**New portfolio/user-holding feature:**
- Read-only views: `scripts/read_positions.py`, `scripts/portfolio_report.py`.
- Manual DB mutation: `scripts/trade_update.py`.
- Valuation refresh: `scripts/update_portfolio_prices.py`.
- Tests: `tests/test_holdings_tools.py`, `tests/test_portfolio_report.py`, `tests/test_update_portfolio_prices.py`.

**New strategy learning or promotion logic:**
- Evidence reports: `scripts/strategy_learning_report.py`, `scripts/strategy_review_report.py`, `scripts/trigger_evidence_convergence_report.py`.
- Proposal generation: `scripts/strategy_config_proposal.py` or `scripts/watchlist_diff_report.py`.
- Promotion: `scripts/strategy_config_promote.py` or `scripts/watchlist_promote.py`.
- Tests: matching `tests/test_strategy_*.py` or `tests/test_watchlist_*.py`.

**New legacy daily data logic:**
- Daily K-line ingestion: `scripts/kline_batch.py`.
- v4 signal output: `scripts/signal_engine_v4.py`.
- Data-health checks: `scripts/data_health_report.py`.
- Tests: `tests/test_kline_*.py`, `tests/test_signal_engine_v4_schema.py`, `tests/test_data_health_report.py`.

**New backtest or replay:**
- Legacy standalone backtest: `backtest/<scenario>_backtest.py`.
- v5/current replay report: prefer `scripts/<scenario>_replay_report.py` if it feeds Hermes/readiness.
- Curated small summaries: `results/<name>_summary.json`.
- Generated raw datasets: keep outside git, typically under `/tmp`.

**New shared utility:**
- Current convention: keep helpers local to the script unless reuse is proven.
- When reuse is necessary: create `scripts/<domain>_utils.py` or `scripts/<domain>_core.py`, then import it with fallback-compatible patterns used in `scripts/hermes_review_packet.py` and `scripts/strategy_config_proposal.py`.
- Tests: add direct unit tests for the shared module and keep existing script-level tests.

## Special Directories

**`.planning/codebase/`:**
- Purpose: Generated GSD codebase maps.
- Generated: Yes.
- Committed: Intended by GSD workflow.

**`results/`:**
- Purpose: Curated summary artifacts from research runs.
- Generated: Yes, by manual/research workflows.
- Committed: Yes, small JSON summaries only.

**`backtest/__pycache__/`:**
- Purpose: Python bytecode cache.
- Generated: Yes.
- Committed: No.

**External `/tmp` runtime paths:**
- Purpose: Alert queues, report files, state files, logs, local datasets, and generated outputs.
- Generated: Yes.
- Committed: No; paths are outside this repo.

**External `/root` deployment paths:**
- Purpose: Server runtime copies of scripts/configs and env-file sourced cron/service jobs.
- Generated: Deployment-specific.
- Committed: No; tracked templates live in `config/` and scripts live in `scripts/`.

**Secret-bearing env files:**
- Purpose: Runtime credentials and portfolio IDs.
- Generated: Operator-managed outside git.
- Committed: No; tracked files may name env variables but must not include secret values.

---

*Structure analysis: 2026-06-19*
