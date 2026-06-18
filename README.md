# HK-US Quantitative

HK/US equities quantitative research and realtime signal system. The current production path is v5 realtime alerting with Hermes review context, Feishu/operator notifications, user-position risk review, and guarded simulation or Alpaca paper execution.

This repository is not configured for automatic real-money trading. Real broker execution should remain manual until paper/live-signal evidence is reviewed separately.

## Current Status

- `scripts/rt_signal_engine_v5.py` is the intended realtime signal source.
- Current strategy config is `v5.5-buy-realtime-alignment-20260618`.
- v5 scans HK/US watchlists, polls realtime quotes, and writes alerts to `/tmp/rt_signal_alerts.jsonl`.
- Signal scoring uses completed daily OHLCV history plus one realtime quote bar.
- v5.5 keeps the guarded momentum breakout model, downgrades noisy or underperforming BUY triggers to diagnostic `WATCH` unless later evidence supports re-enabling, ranks same-symbol same-scan executable candidates by quality, and blocks BUY execution candidates when the symbol's same-session `change_pct` is below the configured realtime-alignment floor.
- Minute/hour data is read-only context for Hermes and quality reports; it is not the core v5 scoring authority.
- `scripts/rt_alert_bridge.py` defaults to notify-only mode.
- The alert bridge is fail-closed: a BUY/SELL raw trigger is not sent as an operator trade candidate unless v5 marks `execution_candidate=true`, Hermes review marks the matching item `eligible_for_approval=true`, and execution readiness is `READY` with `ready_for_execute=true`.
- `scripts/portfolio_report.py` produces advisory `position_review` items for user and simulation holdings, including large unrealized losses and daily holding moves that need trailing-stop/take-profit/reduction review.
- `scripts/rt_order_intake.py` is the gated paper/simulation intake path.
- HK simulated orders use the QuantMind simulation API.
- US paper orders may use Alpaca paper only when explicitly enabled.

## Safety Model

The safe default is:

```bash
RT_ALERT_REMOTE=local RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 \
python3 scripts/rt_alert_bridge.py
```

Execution modes:

| Mode | Effect |
| --- | --- |
| `notify` | Print/send alert text only. No orders. |
| `alert-dry-run` | Run order-intake checks and report the proposed action or rejection. No orders. |
| `alert-sim` | Submit to simulation/paper only after all gates pass. Requires pilot flags and caps. |
| `legacy-sim` | Compatibility path for the old simulation trader. Keep disabled unless intentionally reviewed. |

Paper/simulation execute mode is fail-closed by default. The production server should currently run `alert-dry-run`, not `alert-sim`, while execution readiness is `BLOCKED` and Hermes trade-review eligibility is absent. To enable the pilot path later, the runtime must explicitly set caps such as:

```bash
RT_ALERT_EXECUTION_MODE=alert-sim
RT_ORDER_EXECUTE_PILOT_ENABLED=1
RT_ORDER_PILOT_MAX_ORDER_NOTIONAL_HKD=5000
RT_ORDER_PILOT_MAX_ORDER_RISK_HKD=500
RT_ORDER_PILOT_MAX_DAILY_SUBMITTED_ORDERS=1
```

The bridge also forces order-intake execute gates on when invoking `alert-dry-run` or `alert-sim`: `RT_ORDER_REQUIRE_EXECUTION_READINESS=1`, `RT_ORDER_REQUIRE_STRATEGY_EVIDENCE=1`, `RT_ORDER_REQUIRE_HERMES_JUDGMENT=1`, `RT_ORDER_REQUIRE_MARKET_CONTEXT=1`, and `RT_ORDER_REQUIRE_NO_SYMBOL_CONFLICT=1`. Do not pass `RT_ORDER_REQUIRE_*=0` through bridge jobs; a raw technical trigger without a matching Hermes review item must remain blocked.

For US Alpaca paper:

```bash
RT_ORDER_US_BROKER=alpaca-paper
APCA_API_KEY_ID=...
APCA_API_SECRET_KEY=...
```

The runtime also accepts existing Alpaca env aliases: `ALPACA_API_KEY`,
`ALPACA_SECRET_KEY`, and `ALPACA_BASE_URL`.

Put runtime secrets in the server environment or `/root/.quantmind_env`; do not commit them.

## Realtime v5 Contract

Each v5 alert declares its timeframe basis:

- `timeframe_scope=completed_daily_ohlcv_with_realtime_quote`
- `primary_timeframe=1d`
- `realtime_input=single_quote_temporary_bar`
- `intraday_minute_bars_used=false`
- `intraday_evidence_policy=external_read_only_context_only`

Executable alerts must be confirmed directional BUY/SELL candidates with valid entry, stop, take-profit, risk/reward, liquidity, and execution-candidate fields. Diagnostic `WATCH` rows may still carry candidate geometry for review, but order intake rejects them as non-executable.

v5 treats same-day repeated states as one event. For the same market `signal_date`, symbol, emitted side, and trigger, the engine writes one alert and persists a session key in `/tmp/rt_signal_state.json`; cooldown remains a short-term guard, not permission to re-send the same stale condition every 30 minutes. On startup, v5 also backfills these session keys from recent `/tmp/rt_signal_alerts.jsonl` rows, so a service restart or deploy does not re-announce already observed same-day conditions. A later alert is allowed on the next market signal date, or when the emitted side changes, for example a diagnostic `WATCH` later becoming a confirmed `BUY`.

v5.5 does not globally loosen execution gates. A `急漲` row may carry `candidate_signal_type=BUY`, but it remains `WATCH` unless full-score confirmation, factor confluence, risk geometry, liquidity, realtime direction alignment, and execution-candidate checks pass. `布林上軌動量突破` replaces the old unconditional upper-band SELL interpretation only when same-session or recent momentum supports a breakout context; weak or overbought upper-band touches still remain SELL/WATCH diagnostics.
When a single symbol emits multiple executable BUY or SELL candidates in the same scan, v5.5 keeps the higher-quality candidate and marks lower-ranked same-direction rows as `same_scan_directional_duplicate` diagnostics. Candidates already blocked by the same-session or cooldown dedup state are skipped instead of being re-emitted as WATCH rows.

The live v5.5 config uses `trigger_overrides` to reduce weak-market BUY noise:

- `BUY:布林下軌突破`, `BUY:RSI超賣`, `BUY:MA金叉`, and `BUY:站上MA5` are `disabled_pending_rework`, so they remain diagnostic `WATCH` rows and cannot become execution candidates.
- `BUY:布林上軌動量突破` is `shadow_only_pending_sample`, so strong breakout contexts are observed but not sent to execution review.
- `BUY:急漲` is tightened with a higher score threshold and longer cooldown while evidence is retested.
- `realtime_alignment.block_buy_when_change_pct_below=0.0` downgrades BUY candidates to `WATCH` when same-session price change is negative, with `execution_blocked_reasons=["buy_realtime_direction_misaligned"]`.

SELL and position-risk reviews are not globally disabled. In weak markets they may still appear frequently, but trade-signal delivery still requires Hermes item eligibility plus execution readiness `READY`.

## Hermes Review Layer

Hermes consumes compact review packets from:

```bash
python3 scripts/hermes_review_packet.py --output /tmp/hermes_signal_review_packet.json
```

The packet combines:

- v5 alert and order-intake context;
- market regime and breadth;
- intraday confirmation/contradiction context;
- external news/event/sentiment/fundamentals summaries;
- source reliability and data-health state;
- simulation performance and postmortem coverage;
- execution readiness blockers;
- user/simulation `position_review` items.

Hermes judgments are advisory artifacts. Position review decisions must stay advisory and must not be confused with user-broker order instructions.
By default, trade `review_items` are selected only from fresh confirmed directional alerts within the order-intake freshness window. Stale alerts remain in `/tmp/rt_signal_alerts.jsonl`, outcome reports, and non-trade diagnostics, but they are not re-fed to Hermes as current trade-approval work. Operators can run `hermes_review_packet.py --include-stale` for debugging old alerts.

The Feishu/operator bridge consumes this layer, but does not bypass it. With default settings, technical BUY/SELL alerts blocked by Hermes or execution readiness are marked sent locally so they do not repeat-spam Feishu, and remain available in JSONL/event-store data for learning and postmortem reports. If an operator wants diagnostic noise during debugging, set `RT_ALERT_NOTIFY_INELIGIBLE_SIGNALS=1`; those messages are titled as safety-gate-blocked candidates and do not run intake.

## Feishu Notifications

`scripts/feishu_notify.py` reads credentials from env or `FEISHU_ENV_FILE`, defaulting to `/root/.quantmind_env`.

Expected secret file shape:

```bash
export QM_USER_PORTFOLIO_IDS="3"
export QM_SIM_PORTFOLIO_ID="8"
export FEISHU_APP_ID="..."
export FEISHU_APP_SECRET="..."
export FEISHU_CHAT_ID="..."
```

Enable Feishu delivery explicitly:

```bash
RT_ALERT_REMOTE=local RT_ALERT_SEND_FEISHU=1 RT_ALERT_EXECUTION_MODE=notify \
python3 scripts/rt_alert_bridge.py
```

The bridge marks emitted alerts or position reviews as sent only after Feishu delivery succeeds. Safety-gate-blocked technical triggers are suppressed by default and marked locally so they do not repeat-spam Feishu.
For trade-signal notifications, the default also requires `RT_ALERT_REQUIRE_PACKET_ELIGIBLE=1`: the matching Hermes packet item must be eligible and execution readiness must be READY. This prevents raw technical triggers from being presented as operation signals during blocked or risk-off system states.
Position-review notifications default to `RT_POSITION_REVIEW_ROLES=user`, include `high,medium` urgency, and cap at 20 items. Simulation holdings still remain in the Hermes packet and reports, but they are not mixed into the operator Feishu stream unless `RT_POSITION_REVIEW_ROLES=simulation` or `all` is set explicitly. These notifications are titled as `Hermes持倉審核待辦（不下單）`, include `order_submission=false`, and remain advisory-only.
The position-review message summary counts only the items that passed the active role and urgency filters; packet-level global counts may include filtered simulation or diagnostic context and must not be read as submitted orders.

## Recommended Server Jobs

Use `config/hermes_v5_crontab.txt` as the reviewed cron template. The active default lines are read-only or notify-only. Lines that submit simulation/paper orders, apply DB repairs, promote watchlists, or promote strategy config are commented and hash/pilot gated.

Core runtime:

```bash
cp config/rt_signal_watchlist.json /root/rt_signal_watchlist.json
cp config/rt_signal_strategy_config.json /root/rt_signal_strategy_config.json
cp config/rt_signal_engine_v5.service /etc/systemd/system/rt_signal_engine_v5.service
systemctl daemon-reload
systemctl enable --now rt_signal_engine_v5
```

Cron jobs that need runtime portfolio IDs or Feishu credentials should source env files with export semantics:

```cron
* * * * * /bin/bash -lc "cd /root && set -a; [ -f /root/.quantmind_env ] && . /root/.quantmind_env; [ -f /root/.env ] && . /root/.env; set +a; RT_ALERT_REMOTE=local RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 RT_POSITION_REVIEW_ROLES=user /usr/bin/python3 /root/rt_alert_bridge.py >> /tmp/rt_alert_bridge.log 2>&1"
*/15 * * * * /bin/bash -lc "cd /root && set -a; [ -f /root/.quantmind_env ] && . /root/.quantmind_env; [ -f /root/.env ] && . /root/.env; set +a; /usr/bin/python3 /root/portfolio_report.py --output /tmp/portfolio_report.json --text >> /tmp/portfolio_report.log 2>&1"
* * * * * /bin/bash -lc "cd /root && set -a; [ -f /root/.quantmind_env ] && . /root/.quantmind_env; [ -f /root/.env ] && . /root/.env; set +a; /usr/bin/python3 /root/hermes_review_packet.py --output /tmp/hermes_signal_review_packet.json >> /tmp/hermes_review_packet.log 2>&1"
```

Refresh position price snapshots for both the simulation and user portfolio. These jobs update only `current_price`, `market_value`, unrealized PnL fields, and portfolio totals; they do not change quantities, submit orders, or write Hermes judgments:

```cron
*/15 9-16 * * 1-5 /bin/bash -lc "cd /root && set -a; [ -f /root/.quantmind_env ] && . /root/.quantmind_env; [ -f /root/.env ] && . /root/.env; set +a; QM_PRICE_UPDATE_PORTFOLIO_ID=8 /usr/bin/python3 /root/update_portfolio_prices.py >> /tmp/portfolio_update.log 2>&1"
*/15 9-16 * * 1-5 /bin/bash -lc "cd /root && set -a; [ -f /root/.quantmind_env ] && . /root/.quantmind_env; [ -f /root/.env ] && . /root/.env; set +a; QM_PRICE_UPDATE_PORTFOLIO_ID=3 /usr/bin/python3 /root/update_portfolio_prices.py >> /tmp/portfolio_update_user.log 2>&1"
```

## User Holdings Source Of Truth

User holdings are sourced from DB `positions` rows for portfolio `3`, status `holding`. The helper scripts under `scripts/` are the reviewed interface for that path:

```bash
python3 scripts/read_positions.py --summary
python3 scripts/read_positions.py --us --format json
python3 scripts/read_positions.py --hk --format csv

python3 scripts/hk_realtime.py
python3 scripts/us_realtime.py

python3 scripts/trade_update.py list
python3 scripts/trade_update.py buy --symbol 09988 --exchange HKEX --qty 200 --cost 85.50 --name "Alibaba-W"
python3 scripts/trade_update.py buy --symbol PDD --exchange NASDAQ --qty 10 --cost 82.48
python3 scripts/trade_update.py add --symbol PDD --qty 5 --cost 80.00
python3 scripts/trade_update.py sell --symbol PDD --qty 5 --price 90.00
python3 scripts/trade_update.py sell --symbol PDD --price 90.00
```

`read_positions.py`, `hk_realtime.py`, and `us_realtime.py` read DB holdings and quote data only. `trade_update.py` mutates user holding rows, adjusts the internal `available_cash` estimate by trade notional by default, and recomputes portfolio total value from cash plus open holding value. Use `--no-cash-adjust` only when syncing positions that have already been settled in an external brokerage ledger. The tool does not submit broker orders, append Hermes judgments, write v5 alert state, or change the portfolio `8` simulation ledger. Write commands refuse to mutate any portfolio id not present in `QM_HOLDINGS_PORTFOLIO_ID`, `QM_USER_PORTFOLIO_ID`, or `QM_USER_PORTFOLIO_IDS`, and always refuse configured simulation ids such as `QM_SIM_PORTFOLIO_ID=8`, so `--portfolio-id` is not a bypass for simulation/paper state. The default portfolio id can be overridden with those same user-portfolio environment variables or `--portfolio-id`.

When adding to an existing position, `trade_update.py add` keeps valuation fields consistent: if the existing row has no usable `current_price`, the add cost becomes the refreshed quote snapshot and is written together with market value and unrealized P&L.

`trade_update.py delete --symbol ...` is a maintenance escape hatch for stale or invalid rows. By default it soft-closes a `holding` row and clears open quantity/valuation fields; normal exits should use `sell` so cash and realized P&L are adjusted. Physical deletion requires both `QM_ALLOW_HARD_DELETE_POSITIONS=1` and `--hard --confirm-symbol SYMBOL`.

The `positions` table stores HKD-valued `market_value`/`unrealized_pnl` snapshots, while `avg_cost`, `total_cost`, and `current_price` remain quote-currency prices. `trade_update.py` preserves that convention, including USD-to-HKD valuation for US holdings.

Hermes portfolio context and user-portfolio price refreshes intentionally read/update only `status='holding'` user rows. A portfolio is treated as user-owned when its id appears in `QM_HOLDINGS_PORTFOLIO_ID`, `QM_USER_PORTFOLIO_ID`, or `QM_USER_PORTFOLIO_IDS`; if none are configured, portfolio `3` is the user-holding default. User-portfolio price refresh follows the same source-of-truth rule: if `positions` has no open user holdings, it skips valuation and never rebuilds from `sim_trades`. Full sells mark the row `closed` and clear open quantity/valuation fields, so historical closed rows cannot be mistaken for current exposure. The simulation portfolio `8` path remains compatible with both `active` and `holding` rows, and may still rebuild from `sim_trades` when positions are empty because older simulation jobs used both states.

Readiness refresh, useful after deploy or missing cron:

```bash
python3 scripts/readiness_refresh.py --output /tmp/readiness_refresh_report.json --text
python3 scripts/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text
```

`readiness_refresh.py` keeps the daily-gap repair planner in the same read-only report flow, but gives that full-universe network step a bounded worker pool and a longer per-step timeout. Tune with `KLINE_DAILY_GAP_FETCH_WORKERS` and `READINESS_REFRESH_KLINE_DAILY_GAP_REPAIR_TIMEOUT_SECONDS` rather than skipping the report.

`execution_readiness_report.py` treats sparse 1-day target/stop hit-rate imbalance as material only when the stop-minus-target gap reaches `EXECUTION_READINESS_MIN_STOP_TARGET_IMBALANCE_BLOCK_PCT` (default `5`). Average return, win rate, maximum stop-hit rate, and favorable/adverse ratio remain hard forward-evidence gates.

## Data Policy

Raw market data should stay local by default. Do not commit raw CSV/parquet/DB files or secret-bearing runtime state.

Recommended local research flow:

```bash
APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... \
python3 scripts/local_backtest_dataset.py --output-dir /tmp --start-date 2021-01-01 --end-date 2026-06-14

python3 scripts/local_backtest_reliability_report.py \
  --metadata-file /tmp/hk_us_dataset_metadata.json \
  --realistic-result-file /tmp/portfolio_bt_realistic.json \
  --combined-result-file /tmp/portfolio_bt_v4.json \
  --output /tmp/local_backtest_reliability_report.json --text

python3 scripts/v5_local_replay_report.py \
  --hk-csv /tmp/hk_klines_v2.csv \
  --us-csv /tmp/us_klines.csv \
  --output /tmp/v5_local_replay_report.json --text
```

Use raw minute/hour data for research, replay, and feature engineering, but promote only compact metadata, coverage, freshness, gap, source-quality, and replay reports into Hermes/server workflows.

Minute rows from Tencent public endpoints are persisted as snapshot-like data, not broker-grade minute OHLCV. `scripts/minute_collector.py` writes `data_source='tencent_min'` and, when the DB schema supports it, `source_granularity='minute_snapshot_price'`. `scripts/kline_source_granularity_report.py` can hash-gate and batch-backfill this provenance without changing OHLCV prices, volumes, positions, alerts, or strategy. Hermes may use these rows to cap/challenge confidence and diagnose path risk, but not to claim full intraday execution-quality evidence.

## Backtesting

Backtest scripts remain under `backtest/`, with summary JSON under `results/`. These are research evidence, not a guarantee of live performance.

Important limitations:

- historical backtests can contain survivorship, liquidity, slippage, and universe-selection bias;
- v5 replay is closer to current trigger semantics than legacy portfolio backtests, but it is not a true intraday PnL path reconstruction;
- no report should be treated as fully eliminating overfitting or lookahead risk until walk-forward/out-of-sample validation is complete.

## Repository Layout

```text
backtest/   Historical research backtests.
config/     Runtime templates, schemas, watchlists, crontab template.
docs/       Integration notes, especially docs/HERMES_V5_INTEGRATION.md.
results/    Small checked-in summary artifacts only.
scripts/    Realtime engine, reports, readiness gates, notifications, intake.
tests/      Unit tests for contracts and safety gates.
```

## Verification

Run before release:

```bash
python -m unittest discover -s tests
git diff --check
```

Also scan for secrets before pushing. Placeholder names such as `FEISHU_APP_SECRET` or `APCA_API_SECRET_KEY` are expected in docs/tests; real values are not.

## Known Gaps

- As of the 2026-06-18 server audit, the live stack is running but not execution-ready: execution readiness remains `BLOCKED`, Hermes eligible trade items are `0`, simulation performance is failing, and Alpaca paper intake must remain blocked by gates until those reports recover.
- Dynamic target adjustment, trailing-stop movement, add/reduce, and T-trading are advisory review items, not an automated position-management engine.
- v5 core scoring is daily-history plus realtime quote; intraday data is contextual evidence.
- Full visual backtest dashboards are not the primary artifact yet.
- Strategy promotion should remain blocked unless execution readiness, forward outcomes, Hermes audit-pass learning, and simulation performance support it.

## Production Fixes 2026-06-18

- Deployed `v5.5-buy-realtime-alignment-20260618`: BUY candidates now require non-negative same-session `change_pct` by default before remaining executable.
- Hardened `portfolio_report.py` against stale/wrong DB prices by falling back to latest daily K-line when `positions.current_price` differs from the latest K-line by more than `QM_MAX_DB_PRICE_TO_KLINE_RATIO` (default `3.0`) and flagging the fallback for Hermes.
- Made `update_portfolio_prices.py` portfolio-id configurable via `QM_PRICE_UPDATE_PORTFOLIO_ID`, then refreshed portfolio `3` DB price snapshots so user holdings such as PDD/ARAY/NOK/BABA are no longer valued from stale or mis-scaled prices. Portfolio `3` now refreshes `status='holding'` rows only; portfolio `8` keeps `active/holding` compatibility for simulation state.
- Added reviewed cron lines for portfolio `3` and portfolio `8` price snapshots; these update valuation fields only and do not mutate holdings or submit orders.
- After finding one Alpaca paper order created from a raw `NO_MATCH` technical signal while Hermes judgment was disabled, the server bridge cron was changed from `alert-sim` to `alert-dry-run`, `RT_ORDER_EXECUTE_PILOT_ENABLED=0`, `RT_ALERT_REQUIRE_PACKET_ELIGIBLE=1`, and `RT_ALERT_NOTIFY_INELIGIBLE_SIGNALS=0`.
- Hardened `rt_alert_bridge.py` so bridge-launched intake always forces readiness, strategy evidence, Hermes judgment, market context, and symbol-conflict gates on for `alert-dry-run` and `alert-sim`.
- Changed Feishu position-review delivery to default to `role=user` only, keeping simulation holdings available to Hermes in packet/report context without mixing them into operator-facing holding reminders.
- Added v5 session-level alert de-duplication so persistent conditions such as `跌破MA5`, `RSI超賣`, or Bollinger-band breaches do not re-enter the alert queue every cooldown bucket on the same market signal date.
- Updated cron audit logic so a fail-closed `alert-dry-run` bridge with packet eligibility required, ineligible signal notifications suppressed, and pilot execution disabled satisfies operator delivery wiring; loose dry-run lines still do not count.
- Deployed `v5.4-quality-ranked-same-scan-candidates-20260618` to reduce weak-market BUY noise through existing `trigger_overrides` and replace same-scan first-trigger wins with quality-ranked candidate selection; no execution gates were loosened.
- Converted poor BUY reversal triggers (`布林下軌突破`, `RSI超賣`, `MA金叉`, `站上MA5`) into diagnostic WATCH-only rows.
- Shadowed `布林上軌動量突破` and tightened `急漲` while evidence is retested.
- Clarified Feishu position-review copy so holding risk reminders cannot be mistaken for approved operation signals.
- Updated the v5 systemd template to source `/root/.quantmind_env` and `/root/.env` with shell semantics instead of systemd `EnvironmentFile`, avoiding ignored `export KEY=...` lines and secret-bearing journal warnings.

## Production Fixes 2026-06-16

- Deployed v5.1 momentum-breakout config to the live server.
- Fixed upper Bollinger-band breakout classification so strong momentum contexts are not forced into SELL.
- Fixed large positive moves so confirmed cases can become BUY candidates while unconfirmed cases remain WATCH.
- Fixed position-review loss override: holdings below `-20%` now produce `exit_review`, not `hold`.
- Fixed user portfolio coverage by sourcing `/root/.env`/`/root/.quantmind_env` with `set -a`; portfolio `3` holdings, including `SPCX`, now enter `/tmp/portfolio_report.json` and Hermes packets.
- Installed notify-only `rt_alert_bridge.py` cron. No `alert-sim`, `legacy-sim`, or real broker execution was enabled.

## Performance Optimizations (2026-06-15)

### Database Indexes

Run  on the production database:



This adds two targeted indexes:
-  — partial index for daily klines (symbol + timestamp DESC), speeds up signal_engine_v4 queries by ~11x
-  — partial index for active stocks by exchange

### signal_engine_v4 Query Optimization

Replaced two slow  subqueries with direct  + :

| Query | Before | After | Speedup |
|-------|--------|-------|---------|
|  | 14s (Seq Scan 1M rows) | 1.2s | 11x |
|  | 14s | 1.5s | 9x |

### Cron Concurrency Guards

Added  locks to prevent process pile-up:



### rt_signal_engine_v5 Quote Freshness

Increased  from 15min to 25min to accommodate Tencent free API delay (~15min).
