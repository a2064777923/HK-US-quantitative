# HK-US Quantitative

HK/US equities quantitative research and realtime signal system. The current production path is v5 realtime alerting with Hermes review context, Feishu/operator notifications, user-position risk review, and guarded simulation or Alpaca paper execution.

This repository is not configured for automatic real-money trading. Real broker execution should remain manual until paper/live-signal evidence is reviewed separately.

## Current Status

- `scripts/rt_signal_engine_v5.py` is the intended realtime signal source.
- Current strategy config is `v5.1-momentum-breakout-20260616`.
- v5 scans HK/US watchlists, polls realtime quotes, and writes alerts to `/tmp/rt_signal_alerts.jsonl`.
- Signal scoring uses completed daily OHLCV history plus one realtime quote bar.
- v5.1 adds guarded short-term momentum breakout handling: large positive same-session moves can become BUY candidates, and upper Bollinger-band breaks are BUY candidates only when momentum context supports breakout continuation.
- Minute/hour data is read-only context for Hermes and quality reports; it is not the core v5 scoring authority.
- `scripts/rt_alert_bridge.py` defaults to notify-only mode.
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

Paper/simulation execute mode is fail-closed by default. To enable the pilot path, the runtime must explicitly set caps such as:

```bash
RT_ALERT_EXECUTION_MODE=alert-sim
RT_ORDER_EXECUTE_PILOT_ENABLED=1
RT_ORDER_PILOT_MAX_ORDER_NOTIONAL_HKD=5000
RT_ORDER_PILOT_MAX_ORDER_RISK_HKD=500
RT_ORDER_PILOT_MAX_DAILY_SUBMITTED_ORDERS=1
```

For US Alpaca paper:

```bash
RT_ORDER_US_BROKER=alpaca-paper
APCA_API_KEY_ID=...
APCA_API_SECRET_KEY=...
```

Put runtime secrets in the server environment or `/root/.quantmind_env`; do not commit them.

## Realtime v5 Contract

Each v5 alert declares its timeframe basis:

- `timeframe_scope=completed_daily_ohlcv_with_realtime_quote`
- `primary_timeframe=1d`
- `realtime_input=single_quote_temporary_bar`
- `intraday_minute_bars_used=false`
- `intraday_evidence_policy=external_read_only_context_only`

Executable alerts must be confirmed directional BUY/SELL candidates with valid entry, stop, take-profit, risk/reward, liquidity, and execution-candidate fields. Diagnostic `WATCH` rows may still carry candidate geometry for review, but order intake rejects them as non-executable.

v5.1 does not globally loosen execution gates. A `急漲` row may now carry `candidate_signal_type=BUY`, but it remains `WATCH` unless full-score confirmation, factor confluence, risk geometry, liquidity, and execution-candidate checks pass. `布林上軌動量突破` replaces the old unconditional upper-band SELL interpretation only when same-session or recent momentum supports a breakout context; weak or overbought upper-band touches still remain SELL/WATCH diagnostics.

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

The bridge marks alerts or position reviews as sent only after Feishu delivery succeeds.
Position-review notifications include `high,medium` urgency by default and cap at 20 items, so user holdings such as `SPCX` are not hidden behind simulation-only or high-risk rows. These notifications remain advisory-only and do not submit orders.

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
* * * * * /bin/bash -lc "cd /root && set -a; [ -f /root/.quantmind_env ] && . /root/.quantmind_env; [ -f /root/.env ] && . /root/.env; set +a; RT_ALERT_REMOTE=local RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py >> /tmp/rt_alert_bridge.log 2>&1"
*/15 * * * * /bin/bash -lc "cd /root && set -a; [ -f /root/.quantmind_env ] && . /root/.quantmind_env; [ -f /root/.env ] && . /root/.env; set +a; /usr/bin/python3 /root/portfolio_report.py --output /tmp/portfolio_report.json --text >> /tmp/portfolio_report.log 2>&1"
* * * * * /bin/bash -lc "cd /root && set -a; [ -f /root/.quantmind_env ] && . /root/.quantmind_env; [ -f /root/.env ] && . /root/.env; set +a; /usr/bin/python3 /root/hermes_review_packet.py --output /tmp/hermes_signal_review_packet.json >> /tmp/hermes_review_packet.log 2>&1"
```

Readiness refresh, useful after deploy or missing cron:

```bash
python3 scripts/readiness_refresh.py --output /tmp/readiness_refresh_report.json --text
python3 scripts/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text
```

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

- Dynamic target adjustment, trailing-stop movement, add/reduce, and T-trading are advisory review items, not an automated position-management engine.
- v5 core scoring is daily-history plus realtime quote; intraday data is contextual evidence.
- Full visual backtest dashboards are not the primary artifact yet.
- Strategy promotion should remain blocked unless execution readiness, forward outcomes, Hermes audit-pass learning, and simulation performance support it.

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
