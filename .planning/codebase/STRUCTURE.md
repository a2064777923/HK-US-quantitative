---
last_mapped_commit: 67e699dca6ad9c51903425039a8a2b0f78639465
last_mapped_at: 2026-06-11
---
# Codebase Structure

**Analysis Date:** 2026-06-11

## Directory Layout

```text
F:/stock/
├── backtest/                 # Standalone portfolio and trade backtest scripts
├── config/                   # Config template and cron schedule
├── docs/                     # Strategy/scoring documentation
├── results/                  # Committed summary JSON from backtests
├── scripts/                  # Operational data, signal, trading, notification scripts
├── .gitignore                # Python, IDE, log, and secret ignore rules
├── README.md                 # Project overview and performance summary
└── requirements.txt          # pip dependencies
```

## Directory Purposes

**`scripts/`:**
- Purpose: Runtime automation for market data, signal generation, simulation trading, notifications, and price refresh.
- Contains: Python scripts designed to be run directly.
- Key files: `scripts/signal_engine_v4.py`, `scripts/kline_batch.py`, `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_daily_pipeline.py`, `scripts/feishu_notify.py`.

**`backtest/`:**
- Purpose: Historical strategy validation and performance research.
- Contains: Standalone Python scripts that load either `/tmp/*.csv` files or PostgreSQL data.
- Key files: `backtest/portfolio_backtest_realistic.py`, `backtest/portfolio_backtest_combined.py`, `backtest/segment_backtest.py`, `backtest/backtest_trades.py`.

**`config/`:**
- Purpose: Operational configuration examples and scheduling.
- Contains: `config/config.template.json` and `config/crontab.txt`.
- Key files: `config/config.template.json` models database, Redis, API, Feishu, and portfolio settings.

**`docs/`:**
- Purpose: Human-readable strategy documentation.
- Contains: `docs/scoring_logic.md`.
- Key files: `docs/scoring_logic.md` describes factor weights, thresholds, hard rules, order prices, support/resistance, and hourly prediction.

**`results/`:**
- Purpose: Committed summary outputs from historical backtests.
- Contains: JSON summaries only, not full trade/equity logs.
- Key files: `results/realistic_backtest_summary.json` and `results/combined_backtest_summary.json`.

## Key File Locations

**Entry Points:**
- `scripts/kline_batch.py`: update K-line data from Tencent into PostgreSQL.
- `scripts/generate_signals.py`: update simpler quality fields on current `engine_signal_scores`.
- `scripts/signal_engine_v4.py`: compute full v4 technical signal quality and side.
- `scripts/quantmind_strategy_runner.py`: production-like simulation strategy runner.
- `scripts/quantmind_sim_trader.py`: older P5 simulation order engine.
- `scripts/quantmind_daily_pipeline.py`: remote daily pipeline for K-lines, features, and inference.
- `scripts/update_portfolio_prices.py`: refresh active holding prices.
- `scripts/expand_hk_us.py`: expand stock universe using AkShare.
- `scripts/feishu_notify.py`: Feishu notification helper.
- `backtest/*.py`: manual backtest entry points.

**Configuration:**
- `requirements.txt`: declared Python dependencies.
- `config/config.template.json`: example runtime config.
- `config/crontab.txt`: cron schedule for heartbeat, K-line update, signal engine, strategy runner, and price updater.
- `.gitignore`: excludes virtualenvs, caches, logs, and secret files.

**Core Logic:**
- `scripts/signal_engine_v4.py`: most complete live scoring implementation.
- `docs/scoring_logic.md`: intended strategy specification.
- `scripts/quantmind_strategy_runner.py`: position sizing, market-session gating, stop/take-profit execution, lot sizes, and notification report assembly.
- `backtest/portfolio_backtest_realistic.py`: fixed position-size backtest used by README headline performance.
- `backtest/portfolio_backtest_combined.py`: long-history combined backtest with dynamic allocation.

**Testing:**
- No `tests/` directory exists.
- No pytest/unittest files exist.
- Backtest scripts are the only validation-style artifacts.

## Naming Conventions

**Files:**
- Operational scripts use lower snake case: `signal_engine_v4.py`, `quantmind_strategy_runner.py`, `update_portfolio_prices.py`.
- Backtest scripts use descriptive snake case: `portfolio_backtest_realistic.py`, `segment_backtest.py`.
- Documentation uses Markdown: `README.md`, `docs/scoring_logic.md`.
- Result summaries use lower snake case JSON: `realistic_backtest_summary.json`.

**Directories:**
- Top-level directories are purpose-based and lower case: `scripts`, `backtest`, `config`, `docs`, `results`.
- There is no Python package directory and no `__init__.py`.

## Where to Add New Code

**New operational script:**
- Primary code: `scripts/<purpose>.py`.
- Configuration: add non-secret defaults to `config/config.template.json`; do not add real secrets.
- Schedule: add cron entries to `config/crontab.txt` only after paths are deployment-correct.

**New strategy or indicator logic:**
- Current convention: place in the operational script that uses it.
- Better target for future refactor: create a shared module such as `scripts/strategy_core.py` and import it from `scripts/signal_engine_v4.py` and `backtest/*.py`.

**New backtest:**
- Add under `backtest/<scenario>_backtest.py`.
- Store large generated outputs outside git or commit only curated summaries under `results/`.

**New documentation:**
- Strategy details: `docs/*.md`.
- GSD planning/codebase context: `.planning/codebase/*.md`.

**New tests:**
- Recommended new location: `tests/`.
- Start with unit tests around extracted scoring functions and smoke tests for script syntax/importability.

## Special Directories

**`results/`:**
- Purpose: Persist selected performance summaries.
- Generated: Yes, manually copied/curated from `/tmp` outputs.
- Committed: Yes.

**`.planning/codebase/`:**
- Purpose: GSD codebase mapping generated on 2026-06-11.
- Generated: Yes.
- Committed: Intended by GSD workflow.

**`/tmp` external runtime path:**
- Purpose: State, logs, CSV inputs, and generated backtest outputs.
- Generated: Yes.
- Committed: No. It is outside this repo.

---

*Structure analysis: 2026-06-11*
