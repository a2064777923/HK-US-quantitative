# External Integrations

**Analysis Date:** 2026-06-19

## APIs & External Services

**Market Data:**
- Tencent Finance / QQ Finance - realtime HK/US quotes, daily K-lines, minute snapshot data, HK quote reads, and fallback partial fundamentals.
  - SDK/Client: `urllib.request` in `scripts/rt_signal_engine_v5.py`, `scripts/kline_batch.py`, `scripts/intraday_kline_batch.py`, `scripts/kline_daily_gap_repair.py`, `scripts/kline_integrity_repair.py`, `scripts/update_portfolio_prices.py`, `scripts/hk_realtime.py`, `scripts/fundamentals_context_producer.py`, and `scripts/quantmind_strategy_runner.py`; `requests` in `scripts/local_backtest_dataset.py`.
  - Auth: none visible in code.
- Sina Finance - US realtime quote fallback for user holdings and price refresh.
  - SDK/Client: `urllib.request` in `scripts/us_realtime.py` and `scripts/update_portfolio_prices.py`.
  - Auth: none visible in code.
- Alpaca Market Data - US daily bars for K-line backfill and local backtest datasets.
  - SDK/Client: REST over `urllib.request` in `scripts/kline_batch.py`; REST over `requests` in `scripts/local_backtest_dataset.py`.
  - Auth: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, plus aliases `ALPACA_API_KEY_ID`, `ALPACA_API_KEY`, `ALPACA_KEY_ID`, and `ALPACA_SECRET_KEY`.
- Yahoo Finance - chart data, quoteSummary fundamentals, and alternate-provider probes.
  - SDK/Client: `urllib.request` in `scripts/market_sentiment_producer.py`, `scripts/market_index_context_producer.py`, `scripts/fundamentals_context_producer.py`, and `scripts/kline_gap_alternate_provider_probe.py`.
  - Auth: none visible in code.
- HKEX and Nasdaq public endpoints - universe refresh and source/session references.
  - SDK/Client: `requests` in `scripts/weekly_universe_refresh.py`, `scripts/expand_universe_v2.py`, `scripts/expand_batch.py`, and `scripts/fix_us_insert.py`.
  - Auth: none visible in code.
- Google News RSS, MarketWatch RSS, CNBC RSS - public external market context for Hermes.
  - SDK/Client: `urllib.request` and XML parsing in `scripts/external_market_context_producer.py`.
  - Auth: none visible in code.
- Local InfoHub HTTP bridge - optional local public-context adapter at `EXTERNAL_CONTEXT_INFOHUB_URL`, defaulting to a loopback service.
  - SDK/Client: `urllib.request` in `scripts/external_market_context_producer.py`; TCP probing in `scripts/trusted_source_discovery_report.py`; cron wiring in `config/hermes_v5_crontab.txt`.
  - Auth: none visible in code.

**Trading and Simulation API:**
- QuantMind API - login, simulation account/positions, simulation orders, and model inference trigger.
  - SDK/Client: `urllib.request` in `scripts/rt_order_intake.py`, `scripts/quantmind_sim_trader.py`, `scripts/quantmind_strategy_runner.py`, and `scripts/quantmind_daily_pipeline.py`.
  - Auth: `QM_API_BASE`, `QM_API_USER`, and `QM_API_PASSWORD`; token flow uses `/auth/login` and bearer tokens.
- Alpaca paper trading - optional US paper broker for `alert-sim` order intake.
  - SDK/Client: REST over `urllib.request` in `scripts/rt_order_intake.py`; audit/reconciliation references in `scripts/rt_order_intake_event_store.py` and `docs/HERMES_V5_INTEGRATION.md`.
  - Auth: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `ALPACA_TRADING_BASE_URL`, `ALPACA_BASE_URL`, and compatible Alpaca alias variables.

**Notifications:**
- Feishu Open API - tenant token retrieval and chat messages for operator notifications.
  - SDK/Client: `urllib.request` in `scripts/feishu_notify.py`; invoked by `scripts/rt_alert_bridge.py`, `scripts/alert_quality_report.py`, and `scripts/portfolio_report.py`.
  - Auth: `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_CHAT_ID`, and optional `FEISHU_ENV_FILE`.

**Remote Operations:**
- SSH to remote runtime host - remote alert reads and legacy simulation commands.
  - SDK/Client: `subprocess.run(["ssh", ...])` in `scripts/rt_alert_bridge.py`; shell-based SSH commands in `scripts/quantmind_sim_trader.py`.
  - Auth: OS-level SSH keys/config outside the repo; `RT_ALERT_REMOTE` selects remote vs local alert bridge mode.

## Data Storage

**Databases:**
- PostgreSQL in Docker container `quantmind-db`.
  - Connection: `docker exec quantmind-db psql -U quantmind -d quantmind` in many scripts; environment-supported `QM_DB_CONTAINER`, `QM_DB_USER`, and `QM_DB_NAME` in report/event-store scripts; `DATABASE_URL` in `scripts/read_positions.py` and `scripts/trade_update.py`; direct `psycopg2` in `scripts/signal_engine_v4.py`.
  - Client: Docker `psql`, `psycopg2`, and a limited SQLAlchemy use inside the remote inline feature-generation block in `scripts/quantmind_daily_pipeline.py`.
  - Tables referenced include `stocks`, `klines`, `engine_signal_scores`, `engine_feature_runs`, `positions`, `portfolios`, `sim_trades`, `rt_signal_alert_events`, `rt_signal_outcome_events`, `rt_order_intake_events`, and Hermes judgment event tables.

**File Storage:**
- Local filesystem only for runtime artifacts.
  - Alert and state files: `/tmp/rt_signal_alert.json`, `/tmp/rt_signal_alerts.jsonl`, `/tmp/rt_signal_state.json`, `/tmp/rt_signal_sent.json`, and `/tmp/rt_order_intake_state.json`.
  - Hermes and readiness reports: `/tmp/hermes_signal_review_packet.json`, `/tmp/hermes_trade_judgments.jsonl`, `/tmp/hermes_position_judgments.jsonl`, `/tmp/execution_readiness_report.json`, and many other `/tmp/*_report.json` paths configured in `scripts/*.py`.
  - Runtime configs on server: `/root/rt_signal_watchlist.json` and `/root/rt_signal_strategy_config.json`, sourced from `config/rt_signal_watchlist.json` and `config/rt_signal_strategy_config.json`.
  - Raw research data is intentionally local-only and ignored by `.gitignore`; examples include `data/`, `datasets/`, `raw_data/`, `intraday_data/`, `*.parquet`, `*.csv.gz`, and database files.
  - Checked-in summary artifacts live in `results/combined_backtest_summary.json` and `results/realistic_backtest_summary.json`.

**Caching:**
- Redis in Docker container `quantmind-redis`.
  - Client: `redis-cli` through Docker in `scripts/quantmind_strategy_runner.py`; database-side `redis_command` helper in `scripts/update_portfolio_prices.py`.
  - Keys include heartbeat/state and price-cache style values such as `quantmind:price:*`.
- Local JSON/JSONL state files under `/tmp` are also used as durable queues and deduplication state by `scripts/rt_signal_engine_v5.py`, `scripts/rt_alert_bridge.py`, and `scripts/rt_order_intake.py`.

## Authentication & Identity

**Auth Provider:**
- Custom QuantMind API login flow.
  - Implementation: `scripts/rt_order_intake.py` posts to `/auth/login`, stores the returned access token in memory for account, position, and order calls, and sends bearer tokens to `/simulation/*` endpoints.
- Feishu tenant token flow.
  - Implementation: `scripts/feishu_notify.py` reads `FEISHU_*` values from environment or `FEISHU_ENV_FILE`, requests a tenant access token, caches it in memory, and posts chat messages.
- Alpaca API key flow.
  - Implementation: `scripts/rt_order_intake.py`, `scripts/kline_batch.py`, and `scripts/local_backtest_dataset.py` send Alpaca key/secret headers directly to REST endpoints.
- Database identity.
  - Implementation: Docker `psql` paths mostly rely on container-local PostgreSQL identity; direct tools use `DATABASE_URL` in `scripts/read_positions.py` and `scripts/trade_update.py`.

## Monitoring & Observability

**Error Tracking:**
- None detected. There is no Sentry, OpenTelemetry, Prometheus, or hosted logging SDK.

**Logs:**
- Console/stdout logging through local helper functions in scripts such as `scripts/rt_signal_engine_v5.py`, `scripts/kline_batch.py`, and `scripts/quantmind_strategy_runner.py`.
- Cron redirects stdout/stderr to `/tmp/*.log` in `config/hermes_v5_crontab.txt`.
- Systemd captures the realtime process configured in `config/rt_signal_engine_v5.service`.
- Operational observability is report-file based: `scripts/system_health_check.py`, `scripts/data_health_report.py`, `scripts/source_reliability_report.py`, `scripts/execution_readiness_report.py`, `scripts/cron_audit_report.py`, and `scripts/operator_action_queue_report.py`.

## CI/CD & Deployment

**Hosting:**
- Production assumptions point to a Linux host with systemd, cron, Docker, PostgreSQL, Redis, Python scripts under `/root`, and runtime secrets sourced from `/root/.quantmind_env` or `/root/.env`.
- Service template: `config/rt_signal_engine_v5.service`.
- Cron template: `config/hermes_v5_crontab.txt`.
- There is no Dockerfile, Docker Compose file, Kubernetes manifest, or cloud deployment config in the repo.

**CI Pipeline:**
- None detected. There is no `.github/workflows`, GitLab CI, Azure Pipelines, or equivalent CI configuration.

## Environment Configuration

**Required env vars:**
- Runtime config files: `RT_SIGNAL_WATCHLIST_FILE`, `RT_SIGNAL_STRATEGY_CONFIG_FILE`, `RT_SIGNAL_INCLUDE_USER_HOLDINGS`, and `RT_SIGNAL_USER_HOLDINGS_REFRESH_SECONDS`.
- Database: `QM_DB_CONTAINER`, `QM_DB_USER`, `QM_DB_NAME`, `DATABASE_URL`, `QM_TENANT_ID`, and `QM_USER_ID`.
- Portfolio/simulation: `QM_PORTFOLIO_ID`, `QM_SIM_PORTFOLIO_ID`, `QM_PRICE_UPDATE_PORTFOLIO_ID`, `QM_HOLDINGS_PORTFOLIO_ID`, `QM_USER_PORTFOLIO_ID`, `QM_USER_PORTFOLIO_IDS`, and `USD_TO_HKD`.
- QuantMind API: `QM_API_BASE`, `QM_API_USER`, and `QM_API_PASSWORD`.
- Feishu: `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_CHAT_ID`, and `FEISHU_ENV_FILE`.
- Alpaca: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `ALPACA_API_KEY_ID`, `ALPACA_API_KEY`, `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `ALPACA_TRADING_BASE_URL`, `ALPACA_BASE_URL`, `ALPACA_DATA_BASE_URL`, `ALPACA_DATA_FEED`, and `ALPACA_DATA_ADJUSTMENT`.
- Alert bridge/order intake: `RT_ALERT_REMOTE`, `RT_ALERT_EXECUTION_MODE`, `RT_ALERT_SEND_FEISHU`, `RT_ALERT_REQUIRE_PACKET_ELIGIBLE`, `RT_ORDER_EXECUTION_MODE`, `RT_ORDER_REQUIRE_EXECUTION_READINESS`, `RT_ORDER_REQUIRE_STRATEGY_EVIDENCE`, `RT_ORDER_REQUIRE_HERMES_JUDGMENT`, `RT_ORDER_REQUIRE_MARKET_CONTEXT`, `RT_ORDER_REQUIRE_NO_SYMBOL_CONFLICT`, `RT_ORDER_EXECUTE_PILOT_ENABLED`, `RT_ORDER_US_BROKER`, and pilot cap variables.
- Context/report producers: `EXTERNAL_CONTEXT_INFOHUB_URL`, `EXTERNAL_MARKET_CONTEXT_INPUT_FILE`, `MARKET_SENTIMENT_INPUT_FILE`, `FUNDAMENTALS_CONTEXT_INPUT_FILE`, `MARKET_INDEX_YAHOO_CHART_URL`, `MARKET_SENTIMENT_YAHOO_CHART_URL`, and report file overrides used by `scripts/*_report.py`.

**Secrets location:**
- Server environment, `/root/.quantmind_env`, and `/root/.env` are referenced by `README.md`, `config/hermes_v5_crontab.txt`, and `config/rt_signal_engine_v5.service`.
- Repo-local secret placeholders are represented by `config/config.template.json`; real secret files such as `config/secrets.json` and `config/.env` are excluded by `.gitignore`.
- No secret file contents were read during this scan.

## Webhooks & Callbacks

**Incoming:**
- None detected. The repository contains command-line scripts and scheduled jobs, not an HTTP server or webhook receiver.

**Outgoing:**
- Feishu chat messages from `scripts/feishu_notify.py`.
- QuantMind API login, simulation account/position/order calls, and model inference calls from `scripts/rt_order_intake.py`, `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_sim_trader.py`, and `scripts/quantmind_daily_pipeline.py`.
- Alpaca market data and optional paper-trading REST calls from `scripts/kline_batch.py`, `scripts/local_backtest_dataset.py`, and `scripts/rt_order_intake.py`.
- Public market/news/fundamentals endpoints from Tencent/QQ Finance, Sina Finance, Yahoo Finance, Google News RSS, MarketWatch RSS, CNBC RSS, HKEX, and Nasdaq in `scripts/*.py`.
- Local InfoHub HTTP reads from `scripts/external_market_context_producer.py` and discovery probes from `scripts/trusted_source_discovery_report.py`.
- SSH commands in `scripts/rt_alert_bridge.py` and `scripts/quantmind_sim_trader.py`.
- Docker CLI calls into `quantmind-db` and `quantmind-redis` from `scripts/*.py` and `backtest/*.py`.
- Systemd and crontab commands from `scripts/system_health_check.py`, `scripts/strategy_config_promote.py`, `scripts/cron_audit_report.py`, and `scripts/cron_install_promote.py`.

---

*Integration audit: 2026-06-19*
