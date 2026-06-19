# Technology Stack

**Analysis Date:** 2026-06-19

## Languages

**Primary:**
- Python 3.11-compatible scripts - all runtime, report, signal, data repair, notification, and backtest code lives in `scripts/*.py`, `backtest/*.py`, and `tests/*.py`. Local interpreter observed with `python --version` is Python 3.11.0, but the repo does not pin a Python version in `.python-version`, `pyproject.toml`, or runtime metadata.

**Secondary:**
- Shell/crontab/systemd - production scheduling and service startup are represented by `config/hermes_v5_crontab.txt`, `config/crontab.txt`, `config/rt_signal_engine_v5.service`, and `scripts/heartbeat_refresh.sh`.
- SQL - operational DDL/index helpers and generated SQL execution paths live in `scripts/add_performance_indexes.sql`, `scripts/*_event_store.py`, `scripts/kline_batch.py`, and other `psql`-backed scripts.
- JSON/Markdown - runtime schemas, watchlists, strategy config, session calendars, docs, and checked-in summaries live in `config/*.json`, `config/*.schema.json`, `README.md`, `docs/*.md`, and `results/*.json`.

## Runtime

**Environment:**
- Command-line Python scripts, not a web application. The primary live process is `scripts/rt_signal_engine_v5.py`, run by the systemd template `config/rt_signal_engine_v5.service`.
- Cron-driven batch/report jobs are modeled in `config/hermes_v5_crontab.txt`; older/simple cron wiring also exists in `config/crontab.txt`.
- Most server paths assume Linux-style runtime locations such as `/root/*.py`, `/root/rt_signal_watchlist.json`, `/root/rt_signal_strategy_config.json`, and `/tmp/*.json`; the repository itself can be edited and tested on Windows.
- Runtime state and report artifacts are file-based under `/tmp`, including alert queues, Hermes packets, readiness reports, strategy reports, and local backtest datasets referenced by `README.md` and `scripts/*.py`.

**Package Manager:**
- pip via `requirements.txt`.
- Lockfile: missing. There is no `requirements.lock`, `pip-tools`, Poetry, uv, Pipenv, or Conda environment file.
- Dependency versions are mostly lower bounds or unpinned names in `requirements.txt`; exact reproducible installs are not defined.

## Frameworks

**Core:**
- No web framework. This is a script-oriented quant/realtime alerting system.
- PostgreSQL access uses a mix of Docker `psql` subprocesses and direct `psycopg2` connections. Common Docker `psql` paths appear in `scripts/kline_batch.py`, `scripts/rt_signal_engine_v5.py`, `scripts/data_health_report.py`, `scripts/portfolio_report.py`, `scripts/rt_signal_outcome_report.py`, `scripts/*_event_store.py`, and `backtest/*.py`; direct `psycopg2` appears in `scripts/signal_engine_v4.py`, `scripts/read_positions.py`, and `scripts/trade_update.py`.
- Redis is used as infrastructure state/cache through `docker exec quantmind-redis redis-cli` in `scripts/quantmind_strategy_runner.py` and a database-side `redis_command` helper in `scripts/update_portfolio_prices.py`.
- Concurrency is mostly process-level cron/systemd. `scripts/rt_signal_engine_v5.py` imports `Thread` and `Lock` for realtime polling/scanning, while long-running batch jobs use filesystem locks such as `KLINE_BATCH_LOCK_FILE` in `scripts/kline_batch.py`.

**Testing:**
- Python standard library `unittest` is the active test framework.
- Tests are located under `tests/test_*.py` and are run with `python -m unittest discover -s tests`, as documented in `README.md`.
- There is no pytest, coverage, tox, nox, or CI test configuration file.

**Build/Dev:**
- No build step is required.
- No formatter, linter, or type checker configuration is present. There is no `.prettierrc`, `.eslintrc`, `ruff.toml`, `pyproject.toml`, `mypy.ini`, or `setup.cfg`.
- `python -m unittest discover -s tests` and `git diff --check` are the documented verification commands in `README.md`.
- Deployment/startup configuration is maintained as templates in `config/hermes_v5_crontab.txt` and `config/rt_signal_engine_v5.service`, not as Docker Compose or Kubernetes manifests.

## Key Dependencies

**Critical:**
- `akshare>=1.18.0` - HK/US stock universe expansion in `scripts/expand_hk_us.py`.
- `pandas>=1.5.0` - feature engineering and Parquet snapshot creation inside the remote feature-generation block in `scripts/quantmind_daily_pipeline.py`.
- `numpy>=1.21.0` - vectorized feature generation with pandas in `scripts/quantmind_daily_pipeline.py`.
- `psycopg2-binary` - direct PostgreSQL access in `scripts/signal_engine_v4.py`, `scripts/read_positions.py`, and `scripts/trade_update.py`.
- `requests` - HTTP client for universe/backtest data paths in `scripts/expand_batch.py`, `scripts/expand_universe_v2.py`, `scripts/weekly_universe_refresh.py`, and `scripts/local_backtest_dataset.py`.
- Python stdlib `urllib.request` - primary HTTP client for live quote feeds, QuantMind API calls, Feishu notifications, Yahoo data, and several producers in `scripts/*.py`.

**Infrastructure:**
- `redis` - listed in `requirements.txt` as an optional Redis client, though current Redis writes are primarily through `redis-cli` or database helper calls.
- Docker CLI - required by many scripts that shell into `quantmind-db` and `quantmind-redis`; examples include `scripts/kline_batch.py`, `scripts/rt_signal_engine_v5.py`, `scripts/system_health_check.py`, and `backtest/segment_backtest.py`.
- PostgreSQL client tools - `psql` is invoked inside the `quantmind-db` container by report, event-store, backtest, and repair scripts.
- `sqlalchemy` - imported only inside the remote inline feature-generation code in `scripts/quantmind_daily_pipeline.py`; it is not listed in `requirements.txt`.
- Parquet engine such as `pyarrow` or `fastparquet` - implicitly required by `DataFrame.to_parquet()` in `scripts/quantmind_daily_pipeline.py`; no Parquet engine is listed in `requirements.txt`.
- `openpyxl` - imported dynamically by `scripts/expand_universe_v2.py` for HKEX spreadsheet handling; it is not listed in `requirements.txt`.

## Configuration

**Environment:**
- Runtime template config lives at `config/config.template.json`; it models database, Redis, QuantMind API, Feishu, and portfolio settings with placeholders.
- Watchlist and strategy config templates live at `config/rt_signal_watchlist.json` and `config/rt_signal_strategy_config.json`.
- Market-session override config lives at `config/intraday_market_sessions.json` with an example in `config/intraday_market_sessions.example.json`.
- Hermes judgment schemas live at `config/hermes_trade_judgment.schema.json` and `config/hermes_position_judgment.schema.json`.
- Service startup sources `/root/.quantmind_env` and `/root/.env` in `config/rt_signal_engine_v5.service`; cron templates source the same files before jobs that need secrets or portfolio IDs in `config/hermes_v5_crontab.txt`.
- Repo-local secret targets are intentionally excluded by `.gitignore`: `config/secrets.json`, `config/.env`, `*.pem`, and `*.key`.
- No root `.env` or `.env.*` file was present in the repository during this scan.

**Build:**
- Build config files: Not detected.
- Test config files: Not detected.
- Runtime/deploy config files: `config/hermes_v5_crontab.txt`, `config/crontab.txt`, `config/rt_signal_engine_v5.service`, `config/config.template.json`, `config/rt_signal_strategy_config.json`, and `config/rt_signal_watchlist.json`.

## Platform Requirements

**Development:**
- Python 3.11-compatible interpreter and pip.
- Install dependencies from `requirements.txt`, plus missing runtime-only packages when working on affected scripts: `sqlalchemy`, a Parquet engine, and `openpyxl`.
- Docker CLI access is required for scripts that call `docker exec quantmind-db psql` or `docker exec quantmind-redis redis-cli`.
- PostgreSQL connection via `DATABASE_URL` is required for direct holding tools such as `scripts/read_positions.py` and `scripts/trade_update.py`.
- Network access is required for Tencent/QQ Finance, Sina Finance, Yahoo Finance, Alpaca, Feishu, QuantMind API, Google News/MarketWatch/CNBC RSS, HKEX, Nasdaq, and optional local InfoHub endpoints.

**Production:**
- Linux host with systemd and cron.
- Docker containers named `quantmind-db` and `quantmind-redis`, or equivalent environment overrides where supported through `QM_DB_CONTAINER`, `QM_DB_USER`, `QM_DB_NAME`, and Redis-related runtime wiring.
- `scripts/rt_signal_engine_v5.py` runs as `rt_signal_engine_v5.service` from `config/rt_signal_engine_v5.service`.
- Cron jobs should be reviewed from `config/hermes_v5_crontab.txt`; active lines are mostly read-only or notify-only, while write/apply/simulation paths are commented or hash/pilot gated.
- Runtime secrets should be provided by the server environment, `/root/.quantmind_env`, `/root/.env`, or gitignored config files; do not commit secret-bearing values.

---

*Stack analysis: 2026-06-19*
