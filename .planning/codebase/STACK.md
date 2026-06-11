---
last_mapped_commit: 67e699dca6ad9c51903425039a8a2b0f78639465
last_mapped_at: 2026-06-11
---
# Technology Stack

**Analysis Date:** 2026-06-11

## Languages

**Primary:**
- Python 3 - all executable strategy, data update, notification, and backtest code lives in `scripts/*.py` and `backtest/*.py`.

**Secondary:**
- Shell/crontab - scheduling and heartbeat glue are represented by `scripts/heartbeat_refresh.sh` and `config/crontab.txt`.
- Markdown/JSON - project documentation and saved backtest summaries live in `README.md`, `docs/scoring_logic.md`, and `results/*.json`.

## Runtime

**Environment:**
- Python 3 command-line scripts, generally intended to run on a host with Docker access to `quantmind-db` and `quantmind-redis`.
- Some scripts assume Linux/container paths such as `/root/*.py` and `/tmp/*.json`, even though this repo can be cloned on Windows.

**Package Manager:**
- pip via `requirements.txt`.
- Lockfile: missing. There is no `requirements.lock`, `pip-tools`, Poetry, or uv lockfile.

## Frameworks

**Core:**
- No application framework. This is a script-oriented quant system.
- PostgreSQL is accessed through `docker exec quantmind-db psql` subprocess calls in `scripts/signal_engine_v4.py`, `scripts/kline_batch.py`, `scripts/generate_signals.py`, `scripts/quantmind_strategy_runner.py`, and `backtest/*.py`.
- Redis is accessed either through `redis-cli` subprocesses or the `redis` Python package in `scripts/update_portfolio_prices.py`.

**Testing:**
- No dedicated test framework is configured.
- Backtest scripts act as research/validation tools rather than automated tests: `backtest/backtest_trades.py`, `backtest/segment_backtest.py`, `backtest/portfolio_backtest_combined.py`, and `backtest/portfolio_backtest_realistic.py`.

**Build/Dev:**
- No build system, formatter, linter, type checker, or CI config is present.
- `python -m compileall -q .` is currently the simplest whole-repo syntax check.

## Key Dependencies

**Critical:**
- `pandas>=1.5.0` - feature generation and tabular processing in `scripts/quantmind_daily_pipeline.py`.
- `numpy>=1.21.0` - vectorized feature calculations in `scripts/quantmind_daily_pipeline.py`.
- `akshare>=1.18.0` - stock universe expansion in `scripts/expand_hk_us.py`.
- `requests` - listed for HTTP/Feishu use, although most current HTTP calls use `urllib.request`.

**Infrastructure:**
- `psycopg2-binary` - listed for PostgreSQL direct access, but the current repo primarily shells out to `psql`.
- `redis` - optional client used in generated inline Redis code in `scripts/update_portfolio_prices.py`.
- `sqlalchemy` - used inside the remote feature generation command in `scripts/quantmind_daily_pipeline.py`, but not listed in `requirements.txt`.
- Parquet engine dependency such as `pyarrow` or `fastparquet` is implicitly required by `DataFrame.to_parquet()` in `scripts/quantmind_daily_pipeline.py`, but not listed in `requirements.txt`.

## Configuration

**Environment:**
- Template config lives at `config/config.template.json`.
- `.gitignore` excludes `config/secrets.json`, `config/.env`, `*.pem`, and `*.key`.
- Several scripts bypass the template and hard-code API endpoints, portfolio IDs, usernames, and operational constants.

**Build:**
- No build config files.
- No Dockerfile or compose file is included, even though runtime scripts depend on Docker container names.

## Platform Requirements

**Development:**
- Python 3.
- Docker CLI access to containers named `quantmind-db` and `quantmind-redis` for most live scripts.
- Network access to Tencent Finance, Feishu Open API, and the QuantMind API host.
- SSH access is required by `scripts/quantmind_daily_pipeline.py` and `scripts/quantmind_sim_trader.py`.

**Production:**
- Cron-style execution is implied by `config/crontab.txt`.
- Production host paths are expected to match `/root/*.py` unless crontab entries are adapted.
- PostgreSQL and Redis containers must be reachable by their hard-coded container names.

---

*Stack analysis: 2026-06-11*
