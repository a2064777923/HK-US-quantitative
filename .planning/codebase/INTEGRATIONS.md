---
last_mapped_commit: 67e699dca6ad9c51903425039a8a2b0f78639465
last_mapped_at: 2026-06-11
---
# External Integrations

**Analysis Date:** 2026-06-11

## APIs & External Services

**Market Data:**
- Tencent Finance / QQ Finance - K-line and latest price retrieval.
  - SDK/Client: `urllib.request` in `scripts/kline_batch.py`, `scripts/signal_engine_v4.py`, `scripts/update_portfolio_prices.py`, and `scripts/quantmind_strategy_runner.py`.
  - Auth: none visible in code.
  - Usage: HK symbols use `hk{symbol}`; US symbols try Tencent suffixes such as `.OQ` and `.N`.
- AkShare - broad HK/US stock universe discovery.
  - SDK/Client: `akshare` in `scripts/expand_hk_us.py`.
  - Auth: none visible in code.

**Trading and Simulation API:**
- QuantMind API - authentication, simulation account reads, simulation order placement, and model inference trigger.
  - SDK/Client: `urllib.request`.
  - Configured in: `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_sim_trader.py`, and `scripts/quantmind_daily_pipeline.py`.
  - Auth: username/password login returning bearer token.

**Notifications:**
- Feishu Open API - tenant token and chat message delivery.
  - SDK/Client: `urllib.request` in `scripts/feishu_notify.py`.
  - Auth: environment variables are supported, but fallback defaults are embedded in code.
  - Used by: `scripts/quantmind_strategy_runner.py` via `send_feishu_message()`.

**Remote Operations:**
- SSH to a remote QuantMind host - used to run SQL and in-container feature generation.
  - SDK/Client: shell commands via `subprocess.run(..., shell=True)`.
  - Files: `scripts/quantmind_daily_pipeline.py` and `scripts/quantmind_sim_trader.py`.

## Data Storage

**Databases:**
- PostgreSQL in Docker container `quantmind-db`.
  - Connection: mostly implicit through `docker exec quantmind-db psql -U quantmind -d quantmind`.
  - Client: direct `psql` subprocess calls, not `psycopg2`, for most scripts.
  - Tables referenced include `stocks`, `klines`, `engine_signal_scores`, `positions`, `sim_trades`, and `portfolios`.

**File Storage:**
- Local filesystem and `/tmp` are used for state and backtest artifacts.
  - Runner state: `/tmp/quantmind_last_state.json` in `scripts/quantmind_strategy_runner.py`.
  - Backtest inputs: `/tmp/all_klines.csv`, `/tmp/hk_klines_v2.csv`, and `/tmp/us_klines.csv`.
  - Backtest outputs: `/tmp/portfolio_bt_v4.json`, `/tmp/portfolio_bt_realistic.json`, and `/tmp/segment_backtest_results.json`.
  - Committed summaries: `results/combined_backtest_summary.json` and `results/realistic_backtest_summary.json`.

**Caching:**
- Redis in Docker container `quantmind-redis`.
  - Updated via `redis-cli SET ... EX 300` in `scripts/quantmind_strategy_runner.py`.
  - Price keys are also written through a `redis_command` SQL helper in `scripts/update_portfolio_prices.py`.

## Authentication & Identity

**Auth Provider:**
- Custom API login flow for QuantMind simulation and admin endpoints.
  - Implementation: POST `/auth/login`, then use bearer token for `/simulation/*` and `/admin/model/run-inference`.
- Feishu tenant access token flow.
  - Implementation: POST `open-apis/auth/v3/tenant_access_token/internal`, then send `im/v1/messages`.

## Monitoring & Observability

**Error Tracking:**
- None. Errors are printed to stdout/stderr and often swallowed with broad `except` blocks.

**Logs:**
- Console logging through local `log()` helpers with timestamp prefixes.
- Cron redirects stdout/stderr into `/tmp/*.log` in `config/crontab.txt`.
- Strategy heartbeat is written into Redis keys by `scripts/quantmind_strategy_runner.py`.

## CI/CD & Deployment

**Hosting:**
- No deployment manifests are included.
- Operational assumptions point to a Linux host with cron, Docker, SSH, PostgreSQL, Redis, and Python scripts copied under `/root`.

**CI Pipeline:**
- None. There is no `.github/workflows`, test runner config, or release automation.

## Environment Configuration

**Required env vars:**
- `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, and `FEISHU_CHAT_ID` are supported by `scripts/feishu_notify.py`.
- Database, Redis, API, and portfolio values are modeled in `config/config.template.json`, but most scripts do not load this file.

**Secrets location:**
- Intended: `config/secrets.json` or `config/.env` based on `.gitignore`.
- Actual current state: several operational credentials and sensitive endpoints are embedded directly in Python scripts. Do not copy them into planning docs.

## Webhooks & Callbacks

**Incoming:**
- None. This repo contains scripts, not an HTTP server.

**Outgoing:**
- Feishu chat messages.
- QuantMind API login, account, order, and inference requests.
- Tencent Finance/QQ Finance quote requests.
- SSH commands to the remote host.
- Docker CLI calls into PostgreSQL and Redis containers.

---

*Integration audit: 2026-06-11*
