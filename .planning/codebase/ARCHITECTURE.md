---
last_mapped_commit: 67e699dca6ad9c51903425039a8a2b0f78639465
last_mapped_at: 2026-06-11
---
<!-- refreshed: 2026-06-11 -->
# Architecture

**Analysis Date:** 2026-06-11

## System Overview

```text
HK/US Quant Scripts
├─ Data ingestion
│  ├─ `scripts/expand_hk_us.py`
│  ├─ `scripts/kline_batch.py`
│  └─ `scripts/quantmind_daily_pipeline.py`
├─ Signal generation
│  ├─ `scripts/generate_signals.py`
│  └─ `scripts/signal_engine_v4.py`
├─ Trading operations
│  ├─ `scripts/quantmind_strategy_runner.py`
│  ├─ `scripts/quantmind_sim_trader.py`
│  ├─ `scripts/update_portfolio_prices.py`
│  └─ `scripts/feishu_notify.py`
└─ Research and validation
   ├─ `backtest/*.py`
   ├─ `docs/scoring_logic.md`
   └─ `results/*.json`

External/runtime layer
├─ PostgreSQL: `quantmind-db`
├─ Redis: `quantmind-redis`
├─ Tencent Finance and AkShare
├─ QuantMind API
└─ Feishu Open API
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| Stock universe expansion | Populate/refresh HK and US stock records in `stocks`. | `scripts/expand_hk_us.py` |
| K-line batch updater | Fetch Tencent daily K-lines and insert into `klines`. | `scripts/kline_batch.py` |
| Daily pipeline | Run remote K-line update, feature parquet generation, and model inference trigger. | `scripts/quantmind_daily_pipeline.py` |
| Basic quality updater | Add simple RSI/MA/momentum quality details to existing engine scores. | `scripts/generate_signals.py` |
| Signal engine v4 | Compute technical score, support/resistance, order prices, predictions, and write quality JSONB. | `scripts/signal_engine_v4.py` |
| Strategy runner | Select market session, read BUY signals, place simulated orders, enforce stop/take profit, send Feishu reports, and update Redis heartbeat. | `scripts/quantmind_strategy_runner.py` |
| Sim trader | Older P5 simulation order engine that reads signals remotely and submits API orders. | `scripts/quantmind_sim_trader.py` |
| Price updater | Refresh simulated holding prices through Tencent and Redis/DB helpers. | `scripts/update_portfolio_prices.py` |
| Notification helper | Feishu token caching and text message sending. | `scripts/feishu_notify.py` |
| Backtests | Offline or DB-backed validation of scoring logic and portfolio rules. | `backtest/*.py` |

## Pattern Overview

**Overall:** Operational script pipeline with direct infrastructure coupling.

**Key Characteristics:**
- Scripts are self-contained and duplicate infrastructure helpers such as `db(sql)`, quote fetching, scoring, and logging.
- Persistent state lives outside the repo in PostgreSQL, Redis, remote containers, and `/tmp`.
- There is no shared package layer, dependency injection, or central config loader.
- Backtest implementations are independent copies of strategy logic, not imports from the live signal engine.

## Layers

**Data Layer:**
- Purpose: Maintain stock universe and K-line history.
- Location: `scripts/expand_hk_us.py`, `scripts/kline_batch.py`, and parts of `scripts/quantmind_daily_pipeline.py`.
- Contains: AkShare scraping, Tencent K-line fetching, SQL insert generation, and remote SQL calls.
- Depends on: AkShare, Tencent Finance, Docker, SSH, PostgreSQL.
- Used by: signal generation, strategy runner, and backtests.

**Signal Layer:**
- Purpose: Produce or enrich `engine_signal_scores`.
- Location: `scripts/signal_engine_v4.py` and `scripts/generate_signals.py`.
- Contains: RSI, MACD, moving averages, ATR, Bollinger bands, volume ratios, support/resistance, order prices, predictions, and JSONB quality payloads.
- Depends on: PostgreSQL `klines`, `stocks`, and `engine_signal_scores`.
- Used by: `scripts/quantmind_strategy_runner.py` and simulation tools.

**Trading Layer:**
- Purpose: Convert BUY signals into simulation orders and manage live-like strategy state.
- Location: `scripts/quantmind_strategy_runner.py` and `scripts/quantmind_sim_trader.py`.
- Contains: API login, account reads, current price refresh, stop loss/take profit checks, position sizing, lot sizing, order placement, cash sync, notifications, and heartbeat.
- Depends on: QuantMind API, PostgreSQL, Redis, Tencent quotes, Feishu.
- Used by: cron schedule in `config/crontab.txt`.

**Research Layer:**
- Purpose: Test strategy assumptions over historical CSV or DB data.
- Location: `backtest/*.py`, `docs/scoring_logic.md`, and `results/*.json`.
- Contains: standalone scoring functions, portfolio simulation loops, fixed-position and compounding variants, segmented market analysis, and summary outputs.
- Depends on: `/tmp/*.csv` files or PostgreSQL, depending on script.
- Used by: manual research and README performance claims.

## Data Flow

### Scheduled Live Signal Path

1. Cron triggers K-line update and signal computation through `config/crontab.txt`.
2. `scripts/kline_batch.py` reads active symbols from PostgreSQL and fetches Tencent K-lines.
3. `scripts/kline_batch.py` bulk-inserts daily bars into `klines`.
4. `scripts/signal_engine_v4.py` reads recent `klines` and active `stocks`, computes technical quality, and updates `engine_signal_scores`.
5. `scripts/quantmind_strategy_runner.py` reads latest BUY rows from `engine_signal_scores`.
6. `scripts/quantmind_strategy_runner.py` gets current account state from QuantMind API and current prices from Tencent.
7. `scripts/quantmind_strategy_runner.py` places simulation orders, syncs cash, updates Redis heartbeat, and sends Feishu notifications.

### Daily Remote Inference Path

1. `scripts/quantmind_daily_pipeline.py` SSHes into the remote host.
2. It updates recent K-lines in the remote PostgreSQL container.
3. It runs an inline Python script inside the `quantmind` container to generate `/app/db/feature_snapshots/model_features_2026.parquet`.
4. It logs into the QuantMind API as admin and triggers `/admin/model/run-inference`.

### Backtest Path

1. CSV backtests load `/tmp/all_klines.csv`, `/tmp/hk_klines_v2.csv`, or `/tmp/us_klines.csv`.
2. DB backtests read `klines` through `docker exec quantmind-db psql`.
3. Each script computes its own scoring and stop logic.
4. Results are printed and written to `/tmp/*.json`; selected summaries are committed in `results/*.json`.

**State Management:**
- PostgreSQL is the source of truth for stocks, K-lines, signal scores, trades, portfolios, and positions.
- Redis stores short-lived strategy heartbeat and simulation account/price cache data.
- `/tmp/quantmind_last_state.json` prevents repeated Feishu notifications.
- There is no migration layer or schema definition in this repo.

## Key Abstractions

**Technical score:**
- Purpose: Convert market history into BUY/HOLD/SELL readiness.
- Examples: `scripts/signal_engine_v4.py`, `docs/scoring_logic.md`, and copied variants in `backtest/*.py`.
- Pattern: procedural indicator functions returning numeric scores and reason strings.

**Market data fetcher:**
- Purpose: Read daily quotes/K-lines from Tencent Finance.
- Examples: `scripts/kline_batch.py`, `scripts/update_portfolio_prices.py`, and `scripts/quantmind_strategy_runner.py`.
- Pattern: direct `urllib.request` calls with user-agent headers and silent fallback.

**DB helper:**
- Purpose: Execute SQL against `quantmind-db`.
- Examples: repeated `db(sql)` or `db_query(sql)` functions in most scripts.
- Pattern: subprocess wrapper around `docker exec ... psql`.

**Strategy execution loop:**
- Purpose: Convert latest database signal rows into simulated orders and notifications.
- Example: `scripts/quantmind_strategy_runner.py`.
- Pattern: single script function `run_strategy()` orchestrates the whole flow.

## Entry Points

**K-line update:**
- Location: `scripts/kline_batch.py`
- Triggers: cron and manual `python3 scripts/kline_batch.py`.
- Responsibilities: fetch all active HK/US daily K-lines and write to PostgreSQL.

**Signal engine v4:**
- Location: `scripts/signal_engine_v4.py`
- Triggers: cron and manual execution.
- Responsibilities: compute signal side and quality JSON for active symbols.

**Strategy runner:**
- Location: `scripts/quantmind_strategy_runner.py`
- Triggers: cron every 5 minutes during trading hours.
- Responsibilities: place simulation orders, enforce stops, send reports, and heartbeat.

**Price updater:**
- Location: `scripts/update_portfolio_prices.py`
- Triggers: cron every 15 minutes during trading hours.
- Responsibilities: refresh active position prices.

**Backtests:**
- Location: `backtest/*.py`
- Triggers: manual execution.
- Responsibilities: produce strategy performance summaries and trade exports.

## Architectural Constraints

- **Threading:** Single-process, sequential scripts. No async, queue, worker, or parallel execution model.
- **Global state:** Strategy constants and credentials are module globals in `scripts/quantmind_strategy_runner.py`, `scripts/quantmind_sim_trader.py`, and `scripts/feishu_notify.py`.
- **Circular imports:** None observed. The only local import is `quantmind_strategy_runner.py` importing `feishu_notify`.
- **Runtime coupling:** Many scripts require exact Docker container names and table schemas that are not defined in this repo.
- **Path coupling:** Cron and script state assume Linux paths under `/root` and `/tmp`.
- **Config divergence:** `config/config.template.json` exists, but the scripts mostly use hard-coded constants instead of loading it.

## Anti-Patterns

### Duplicate Strategy Logic

**What happens:** RSI/MACD/MA/ATR/Bollinger scoring logic is copied across `scripts/signal_engine_v4.py`, `scripts/generate_signals.py`, `backtest/backtest_trades.py`, `backtest/segment_backtest.py`, `backtest/portfolio_backtest_combined.py`, and `backtest/portfolio_backtest_realistic.py`.
**Why it's wrong:** Backtests can drift away from the live signal engine, so performance results may not validate the deployed strategy.
**Do this instead:** Extract shared indicator and scoring code into a module such as `scripts/strategy_core.py`, then import it from live and backtest scripts.

### Direct Shell SQL Everywhere

**What happens:** SQL is assembled with f-strings and sent through `docker exec ... psql`.
**Why it's wrong:** It couples code to one deployment layout, makes testing difficult, and increases injection and quoting risks.
**Do this instead:** Centralize database access behind a small adapter that supports parameterized queries and can be mocked in tests.

### Secrets in Code

**What happens:** API passwords, Feishu defaults, and remote operational targets are embedded in scripts.
**Why it's wrong:** Public repos and logs can expose credentials and infrastructure details.
**Do this instead:** Load secrets from environment or ignored config files and fail fast when required values are absent.

## Error Handling

**Strategy:** Best-effort scripting with broad exception handling.

**Patterns:**
- Many quote and parsing failures are silently ignored with `except:` or `except Exception: continue`.
- API failures are sometimes converted into status dictionaries, for example `place_order()` in `scripts/quantmind_strategy_runner.py`.
- Backtests generally skip malformed rows and continue.
- There is no centralized retry, alerting, structured error type, or exit-code policy.

## Cross-Cutting Concerns

**Logging:** Local `log()` helpers print timestamps; cron redirects logs to `/tmp/*.log`.
**Validation:** Minimal. Inputs from SQL, Tencent, API responses, and CSV files are parsed with best-effort conversion.
**Authentication:** Custom API login and Feishu token flow are implemented inline.
**Observability:** Redis heartbeat exists for strategy status, but exceptions and data quality failures are not aggregated.

---

*Architecture analysis: 2026-06-11*
