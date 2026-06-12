# Hermes v5 Integration

**Updated:** 2026-06-12

This document explains how to connect the realtime v5 signal path without breaking the existing QuantMind jobs or the separate simulation trading system.

## What Changed

### Reliability hardening

The daily v4 path now has a stricter data contract:

- `signal_engine_v4.py` uses the latest active HK/US K-line date as `trade_date`.
- It creates/updates one `engine_feature_runs` row with `run_id=signal_v4_YYYYMMDD`.
- It upserts `engine_signal_scores` rows for `model_version=signal_v4` and `feature_version=v4_full`.
- It only analyzes symbols whose latest daily K-line matches the selected `trade_date`.
- `quantmind_strategy_runner.py` still manages existing position stop-loss/take-profit checks, but it will not open new positions when the relevant market's v4 signal date is stale.
- `quantmind_strategy_runner.py` also filters new BUY candidates by `quality.order_prices.rr_ratio` and blocking risk flags. Default `QM_STRATEGY_MIN_RR_RATIO=1.5`; existing position exits still run.
- `quantmind_strategy_runner.py` is now legacy position management by default. New BUY openings are disabled unless `QM_STRATEGY_ALLOW_NEW_POSITIONS=1` is set explicitly. This keeps accidental legacy cron deployments from bypassing the v5 Hermes/intake gates.
- `kline_batch.py` and `quantmind_daily_pipeline.py` now update an existing same-day K-line row instead of leaving the first intraday snapshot frozen.
- `update_portfolio_prices.py` reads both `active` and `holding` positions for portfolio 8.

These changes are intended to preserve existing jobs while preventing stale signals and stale prices from silently driving new orders.

### Realtime signal engine

`scripts/rt_signal_engine_v5.py` is now the intended realtime signal source.

Improvements:
- It writes every alert as an event with `signal_id`, `source`, `market`, `confirmed`, `full_score`, `entry_price`, `stop_loss`, and `take_profit` when the alert is a confirmed directional candidate.
- It still writes the legacy latest-alert file at `/tmp/rt_signal_alert.json`.
- It also appends every alert to `/tmp/rt_signal_alerts.jsonl`, so Hermes can consume alerts without losing events when multiple alerts happen between cron runs.
- Realtime quotes are handled as one temporary intraday bar. They no longer get appended to historical daily arrays every scan, which avoids RSI/MA/MACD drift during the day.
- BUY/SELL candidates carry a full-score confirmation flag. Unconfirmed directional candidates are downgraded to `WATCH` by default, with `candidate_signal_type` and candidate risk fields retained for diagnostics.
- Volume anomaly WATCH alerts compare cumulative intraday volume with expected cumulative daily volume based on elapsed HK/US session minutes. This avoids the old false spike where cumulative volume was divided by one minute of average volume.

### Hermes bridge

`scripts/rt_alert_bridge.py` now supports explicit execution modes:

- `RT_ALERT_EXECUTION_MODE=notify` - default, prints Hermes notification text only.
- `RT_ALERT_EXECUTION_MODE=alert-dry-run` - runs `rt_order_intake.py` per alert in dry-run mode and reports the proposed order/rejection reason.
- `RT_ALERT_EXECUTION_MODE=alert-sim` - runs `rt_order_intake.py` per alert in execute mode.
- `RT_ALERT_EXECUTION_MODE=legacy-sim` - preserves old behavior by running `quantmind_sim_trader.py` after actionable alerts.

Important: `legacy-sim` does not place orders for each v5 alert directly. It runs the existing simulation trader, which still reads `engine_signal_scores` from the database. Use `notify` first unless Hermes intentionally wants that legacy behavior.

### Alert-specific order intake

`scripts/rt_order_intake.py` is the new safe interface between v5 alerts and the 100k HKD simulation portfolio.

It accepts one v5 alert or a JSONL queue and applies:

- `signal_id` idempotency through `/tmp/rt_order_intake_state.json`;
- separate dry-run and execute ledgers, so shadow runs do not consume the signal for later execution;
- `confirmed`, `full_score`, risk/reward, alert age, and price-geometry validation;
- execute-only strategy evidence gate using `/tmp/rt_signal_outcome_report.json`;
- execute-only symbol conflict gate using the current-scope v5 alert queue;
- portfolio cash, max positions, existing holdings, lot-size, and per-trade risk sizing;
- fail-closed health gate before execute mode;
- fail-closed Hermes judgment gate before execute mode;
- dry-run by default.

Dry-run examples:

```bash
/usr/bin/python3 /root/rt_order_intake.py --alert-json '{"signal_id":"demo","symbol":"00700","signal_type":"BUY","confirmed":true,"full_score":0.6,"entry_price":300,"stop_loss":288,"take_profit":330,"rr_ratio":2.5,"generated_at":"2026-06-12T10:00:00"}'
/usr/bin/python3 /root/rt_order_intake.py --queue-file /tmp/rt_signal_alerts.jsonl --limit 20
```

Execute mode requires explicit opt-in and API credentials in the environment:

```bash
RT_ORDER_EXECUTION_MODE=execute /usr/bin/python3 /root/rt_order_intake.py --alert-json "$ALERT_JSON"
```

Execute mode also requires strategy evidence by default. The gate reads `/tmp/rt_signal_outcome_report.json` and rejects execution unless the configured horizon has enough resolved forward outcomes with positive average signed return and acceptable win rate. Defaults:

- `RT_ORDER_REQUIRE_STRATEGY_EVIDENCE=1`
- `RT_ORDER_STRATEGY_EVIDENCE_HORIZON=1d`
- `RT_ORDER_MIN_OUTCOME_SAMPLE=30`
- `RT_ORDER_MIN_TRIGGER_OUTCOME_SAMPLE=5`
- `RT_ORDER_MIN_OUTCOME_WIN_RATE_PCT=45`
- `RT_ORDER_MIN_OUTCOME_AVG_RETURN_PCT=0`
- `RT_ORDER_MAX_OUTCOME_REPORT_AGE_HOURS=72`

Dry-run mode does not block on this gate, but includes the gate result in `strategy_evidence` so Hermes can see whether execution would be blocked.

Execute mode also requires no same-symbol directional conflict in the current v5 alert queue by default. The gate reads `/tmp/rt_signal_alerts.jsonl`, scans the recent tail, and rejects execution when the same symbol has an opposite BUY/SELL alert in the same `strategy_config_id + watchlist_id` scope. Defaults:

- `RT_ORDER_REQUIRE_NO_SYMBOL_CONFLICT=1`
- `RT_ORDER_CONFLICT_QUEUE_SCAN_LIMIT=1000`

Dry-run mode does not block on this gate, but includes `symbol_conflict.would_block_execute=true` and `symbol_conflict.reasons` so Hermes can see whether execution would be blocked. Missing queue context is fail-closed in execute mode because the system cannot prove there is no current same-symbol conflict.

Execute mode also requires market context by default for new BUY orders. The gate reads `/tmp/market_context_report.json` and rejects new BUY execution in `risk_off` regimes unless Hermes explicitly documents a market-regime exception. Defaults:

- `RT_ORDER_REQUIRE_MARKET_CONTEXT=1`
- `RT_ORDER_MAX_MARKET_CONTEXT_AGE_HOURS=72`
- `RT_ORDER_MIN_MARKET_EXCEPTION_CONFIDENCE=0.80`

Risk-off exception fields in the Hermes judgment:

```json
{
  "market_regime_exception": true,
  "market_regime_exception_reason": "Specific company-level catalyst or hedge context that justifies a reduced probe despite weak breadth."
}
```

This exception only allows the market-context gate to pass. The trade must still pass health, alert validation, strategy evidence, position sizing, and Hermes judgment freshness/confidence checks.

When `rt_alert_bridge.py` calls intake over SSH, it sources `/root/.quantmind_env` on the server before running `rt_order_intake.py`. Put secrets there, not in crontab:

```bash
export QM_API_USER=kaitosim
export QM_API_PASSWORD=...
```

Do not clear `/tmp/rt_order_intake_state.json` casually. It is the local idempotency ledger preventing repeated processing of the same `signal_id`.

### Hermes judgment gate

Execute mode requires a matching Hermes judgment unless `RT_ORDER_REQUIRE_HERMES_JUDGMENT=0` is set. The default judgment file is:

```bash
/tmp/hermes_trade_judgments.jsonl
```

Schema is provided at `config/hermes_trade_judgment.schema.json`.

Hermes should append one JSON object per reviewed signal:

```json
{
  "schema": "hermes_trade_judgment_v1",
  "packet_id": "copy from hermes_signal_review_packet_v1.packet_id",
  "signal_id": "20260612:00700:站上MA5:BUY:123456",
  "decision": "approve",
  "confidence": 0.74,
  "reviewed_at": "2026-06-12T10:06:00",
  "reviewer": "hermes",
  "supporting_factors": ["v5 signal is confirmed", "risk/reward is acceptable"],
  "opposing_factors": ["market index is weak"],
  "risk_notes": ["keep default 1% equity risk cap"]
}
```

Valid decisions:

- `approve` - intake may execute the planned quantity.
- `reduce` - intake may execute no more than `max_quantity`.
- `reject` or `hold` - intake rejects the trade.

`reduce.max_quantity` is still rounded down to the market lot size. If the reduced quantity is below one lot, intake rejects the trade instead of forcing an odd-lot order.

The execute gate also checks confidence and judgment freshness. Defaults:

- `RT_ORDER_MIN_HERMES_CONFIDENCE=0.60`
- `RT_ORDER_MAX_JUDGMENT_AGE_MINUTES=240`

Dry-run output includes a `hermes.request` object containing the proposed plan and the context Hermes should evaluate. That is the contract Hermes can use to write the judgment artifact.

Hermes must copy the current packet's `packet_id` into every judgment. This lets audit tools resolve the exact packet version Hermes reviewed instead of comparing an old judgment with the latest rolling packet.

### Hermes review packet

`scripts/hermes_review_packet.py` builds one standardized JSON packet for Hermes before it writes any judgment.

It combines:

- latest v5 alerts from JSON/JSONL;
- `system_health_check.py --json` equivalent health state;
- `portfolio_report.py` user/simulation context, portfolio-level risk, position review items, and recent simulation review;
- latest `market_context_report.py` breadth/regime context when `/tmp/market_context_report.json` exists;
- latest `universe_rank_report.py` ranked scan-universe context when `/tmp/universe_rank_report.json` exists;
- latest `rt_signal_outcome_report.py` strategy evidence when `/tmp/rt_signal_outcome_report.json` exists;
- latest `strategy_review_report.py` trigger policy context when `/tmp/strategy_review_report.json` exists;
- latest `hermes_judgment_audit_report.py` audit context when `/tmp/hermes_judgment_audit_report.json` exists;
- `rt_order_intake.py` dry-run result for each alert;
- the required Hermes judgment contract.

Example:

```bash
/usr/bin/python3 /root/hermes_review_packet.py --output /tmp/hermes_signal_review_packet.json
cat /tmp/hermes_signal_review_packet.json
```

For readiness freshness gates, keep a machine-readable system health snapshot at the default path:

```bash
/usr/bin/python3 /root/system_health_check.py --output /tmp/quantmind_system_health.json
```

By default each generated packet is also archived by `packet_id` under:

```bash
/tmp/hermes_review_packet_archive/
```

This archive is read by the judgment audit so historical Hermes judgments are checked against the exact packet version they reviewed.

By default the packet scans the latest 500 raw JSONL alerts, scopes them to the latest `strategy_config_id + watchlist_id`, and selects up to 20 confirmed directional `BUY`/`SELL` alerts (`confirmed=true`) for trade review. `WATCH` alerts, unconfirmed candidates, and older config/watchlist rows are counted in `alert_selection.sample_scope`, but are not sent through order-intake dry-run unless `--include-watch`, `--include-unconfirmed`, or `--sample-scope all` is explicitly used for debugging or historical research.

Confirmed directional alerts rejected by order intake with `sell_without_position` or `alert_too_old` are not trade candidates. `sell_without_position` means the simulation portfolio has no position to reduce or exit; `alert_too_old` means the signal missed the configured freshness window. They are moved out of `review_items` into top-level `non_actionable_observations` with `recommended_use=observation_only_no_trade_judgment_required`. This is an additive, lossless packet change:

- source alert counts stay in `alert_selection`;
- the alert and compact intake rejection remain visible for diagnostics and strategy learning;
- Hermes should not write trade judgments for `non_actionable_observations`;
- existing consumers that only read `review_items` continue to work, but see fewer non-executable SELL rows.

Testing without writing the intake dry-run ledger:

```bash
/usr/bin/python3 /root/hermes_review_packet.py --ephemeral-state --stdout --output ''
```

The packet is review-only. It does not submit simulation orders. Hermes should use `review_items[].eligible_for_approval` as a hard gate:

- `true` means Hermes may still approve or reduce only after independent LLM review;
- `false` means Hermes must write `reject` or `hold`, or write no judgment.

The packet also exposes `portfolio_risk` and `position_review` at the top level, copied from `portfolio_context`. If the simulation portfolio risk level is `critical`, all review items receive portfolio risk blocking reasons and are not eligible for approval. If portfolio risk shows `exit_pressure_above_30pct`, new `BUY` approvals are blocked until high-urgency position reviews are handled; `SELL`/reduction review is not blocked by that reason. This is intentionally fail-closed for adding exposure while still allowing exit review.

Execute mode still happens only through `rt_order_intake.py --mode execute`, and still requires a matching fresh Hermes judgment for the same `signal_id`.

### Portfolio context and simulation review

`scripts/portfolio_report.py` provides the read-only context Hermes needs before writing judgments or daily reports.

It separates:

- user portfolios from `QM_USER_PORTFOLIO_ID` or `QM_USER_PORTFOLIO_IDS`;
- the 100k HKD simulation portfolio from `QM_SIM_PORTFOLIO_ID`, default `8`.

It reads:

- `positions` and `portfolios` for cash, exposure, unrealized P&L, and high-priority holdings;
- latest `signal_v4/v4_full` rows for each holding;
- latest K-line close as a price fallback;
- recent `sim_trades` for FIFO-style closed trade review;
- all available simulation `sim_trades` for a read-only position reconciliation check.

Examples:

```bash
/usr/bin/python3 /root/portfolio_report.py --output /tmp/portfolio_report.json --text
/usr/bin/python3 /root/portfolio_report.py --json
/usr/bin/python3 /root/portfolio_report.py --text
QM_USER_PORTFOLIO_ID=7 QM_SIM_PORTFOLIO_ID=8 /usr/bin/python3 /root/portfolio_report.py --send-feishu --text
```

The JSON output is intended as Hermes/readiness context. The canonical file path is `/tmp/portfolio_report.json`, matching `execution_readiness_report.py` defaults. The text output is suitable for Feishu. The report is read-only and does not submit orders.

Important behavior:

- User portfolio recommendations are advice-only.
- Simulation portfolio recommendations can inform Hermes judgment, but execution still goes through `rt_order_intake.py`.
- Trade review P&L is FIFO-estimated from `sim_trades`; it is a diagnostic, not a tax/accounting ledger.
- Each portfolio report includes `risk_summary`; the payload also includes top-level `portfolio_risk` with `schema=portfolio_risk_report_v1`.
- `portfolio_risk` covers cash/exposure, market and quote-currency exposure, max and top-3 concentration, unrealized P&L, latest v4 signal pressure, stop-loss distance, stale price/K-line flags, and backend valuation discrepancy.
- The payload also includes `position_review` with `schema=portfolio_position_review_v1`. These items ask Hermes to review existing holdings for exit, reduction, trailing stop, or hold/watch decisions.
- `position_review` is advisory and `submits_orders=false`. It is not a SELL order and does not call the simulation API.
- If DB position prices are zero or missing, the report can still value positions from latest daily K-lines, but it flags `fallback_valuation_used` and keeps Hermes confidence reduced.
- If trade-ledger open positions differ from the `positions` table, the simulation report is marked `critical` with `positions_table_conflicts_with_trade_ledger`.
- Portfolio risk is read-only. It does not repair `positions`, change cash, write judgments, or submit orders.

## Current Job Topology

Existing jobs in `config/crontab.txt` remain valid and are not removed by this change:

- `/root/heartbeat_refresh.sh` every 2 minutes.
- `/root/kline_batch.py` plus `/root/signal_engine_v4.py` every 30 minutes during HK hours.
- Optional `/root/quantmind_strategy_runner.py` every 5 minutes during HK hours as a legacy position monitor. Keep `QM_STRATEGY_ALLOW_NEW_POSITIONS=0` unless intentionally testing the old DB-signal simulation path.
- `/root/update_portfolio_prices.py` every 15 minutes during HK hours.
- optional `/root/portfolio_report.py` can run as a read-only Hermes/Feishu context job.

New v5 path:

- `rt_signal_engine_v5.py` should run as one long-running process.
- Hermes should run `rt_alert_bridge.py` every minute.

Do not start `rt_signal_engine_v5.py` from cron every minute. It is an infinite loop and should be supervised by systemd, Hermes worker management, pm2, supervisord, or one equivalent long-running process manager.

## No-Loss Rollout

### Phase 0: Local verification

Before copying files to the server, run the local verification command from the repository root:

```bash
python scripts/local_verify.py
```

It runs:

- Python compile checks for `scripts/` and `tests/`;
- `unittest` discovery for the v5 alert, order intake, and portfolio report contracts;
- `git diff --check`.

This does not connect to the live server, database, Feishu, or simulation trading API.

### Phase 1: Deploy side-by-side

Copy these files to the server path Hermes uses, usually `/root`:

- `scripts/rt_signal_engine_v5.py`
- `scripts/rt_alert_bridge.py`
- `scripts/rt_order_intake.py`
- `scripts/hermes_review_packet.py`
- `scripts/rt_alert_event_store.py`
- `scripts/hermes_judgment_event_store.py`
- `scripts/rt_order_intake_event_store.py`
- `scripts/system_health_check.py`
- `scripts/portfolio_report.py`
- `scripts/sim_position_reconcile.py`
- `scripts/market_context_report.py`
- `scripts/universe_rank_report.py`
- `scripts/rt_signal_outcome_report.py`
- `scripts/rt_signal_outcome_event_store.py`
- `scripts/strategy_review_report.py`
- `scripts/strategy_learning_report.py`
- `scripts/simulation_performance_report.py`
- `scripts/strategy_config_proposal.py`
- `scripts/strategy_config_promote.py`
- `scripts/hermes_judgment_audit_report.py`
- `scripts/hermes_position_judgment_audit_report.py`

Keep these config/docs files available for Hermes/operator reference:

- `config/hermes_trade_judgment.schema.json`
- `config/hermes_position_judgment.schema.json`
- `config/hermes_v5_crontab.txt`
- `config/rt_signal_engine_v5.service`
- `config/rt_signal_watchlist.json`
- `config/rt_signal_strategy_config.json`
- `docs/HERMES_V5_INTEGRATION.md`

Do not remove or edit existing crontab jobs yet.

### Phase 2: Start v5 as a daemon

Systemd example is provided at `config/rt_signal_engine_v5.service`.

Example:

```bash
sudo cp config/rt_signal_engine_v5.service /etc/systemd/system/rt_signal_engine_v5.service
sudo systemctl daemon-reload
sudo systemctl enable --now rt_signal_engine_v5
sudo systemctl status rt_signal_engine_v5
```

Expected outputs on the server:

- `/tmp/rt_signal_alert.json` exists after the first actionable or watch alert.
- `/tmp/rt_signal_alerts.jsonl` grows append-only with one JSON alert per line.
- `/tmp/rt_signal_state.json` stores cooldown state.

### Phase 3: Add Hermes bridge in notify-only mode

Use the safe cron example in `config/hermes_v5_crontab.txt`:

```bash
* * * * * RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py
```

This mode does not touch the simulation trading system. It only emits formatted Hermes text.

### Phase 4: Shadow-run alert intake

After notify-only is stable, switch to alert dry-run:

```bash
* * * * * RT_ALERT_EXECUTION_MODE=alert-dry-run RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py
```

This still does not place simulation orders. It lets Hermes see whether each alert would be accepted, rejected, or sized, with the reason.

### Phase 5: Compare before execution

Run notify-only mode for at least one full HK session and one US session.

Compare:

- v5 alerts from `/tmp/rt_signal_alerts.jsonl`.
- `rt_order_intake.py` dry-run decisions from Hermes/cron output.
- Existing `engine_signal_scores` rows used by `quantmind_strategy_runner.py`.
- Simulation orders created by the existing jobs.
- Quote freshness from Tencent.
- Whether alerts arrive during the intended market sessions.

### Phase 6: Optional alert-specific simulation execution

Only after the dry-run decisions are credible, switch to:

```bash
RT_ALERT_EXECUTION_MODE=alert-sim RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py
```

`alert-sim` calls `rt_order_intake.py` in execute mode, so it consumes the v5 alert fields directly. It is the intended v5 simulation path, but it should not be enabled until the health checks and dry-run output are stable.

### Legacy simulation trigger

Only if Hermes explicitly wants old behavior, switch bridge mode:

```bash
RT_ALERT_EXECUTION_MODE=legacy-sim RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py
```

This is compatible with the previous bridge behavior, but it is not alert-specific execution. It triggers the existing `quantmind_sim_trader.py`, which reads DB signals independently. Prefer `alert-dry-run` and then `alert-sim` for v5.

## Rollback

To roll back v5 without touching existing jobs:

```bash
sudo systemctl stop rt_signal_engine_v5
sudo systemctl disable rt_signal_engine_v5
```

Remove or comment the Hermes bridge cron line.

Existing v4 and strategy runner cron jobs continue to work as before.

## Data Visibility

From this repository we can see the data source definitions and code paths, but not necessarily the live data.

Visible in code:

- PostgreSQL container name: `quantmind-db`.
- Redis container name: `quantmind-redis`.
- Tencent quote endpoints.
- Alert files under `/tmp`.
- Remote server access pattern used by Hermes bridge.

Not visible unless running on the production server or with SSH/database access:

- Actual `stocks`, `klines`, `engine_signal_scores`, `positions`, `sim_trades`, and `portfolios` data.
- Redis heartbeat and simulation cache values.
- `/tmp/rt_signal_alert*.json*` files on the server.
- Whether Tencent quotes are fresh for each symbol during the session.

Because live data is not visible locally, reliability must come from server-side health checks and logs.

### Data health report

`scripts/data_health_report.py` is the server-side data contract for Hermes and the health gate. It is read-only and checks the actual production database tables:

- active HK/US stock universe from `stocks`;
- latest daily K-line coverage and 60-day history coverage from `klines`;
- latest OHLC integrity (`open/high/low/close` positive and internally consistent);
- latest `signal_v4/v4_full` signal date and lag versus market K-lines;
- latest `signal_v4_*` feature run status from `engine_feature_runs`.

Daily v4 signal timing gate:

- `signal_engine_v4.py` now treats v4 as a full-day/daily signal generator by default. If the selected `trade_date` is the current local date and local time is before `SIGNAL_V4_DAILY_SIGNAL_READY_TIME` (`16:15` by default), it exits successfully after logging a safe skip and does not write `engine_signal_scores` or `engine_feature_runs`.
- Override is possible only with `SIGNAL_V4_ALLOW_INTRADAY_DAILY=1`; do not set this for production execution unless Hermes/operator explicitly wants partial daily-bar signals.
- `data_health_report.py` has a matching `DATA_HEALTH_DAILY_SIGNAL_READY_TIME` (`16:15` by default). If the latest current-day feature run was generated before the cutoff, or if the current session is still before cutoff, `feature_run.status=FAIL` and the top-level `data_health.status=FAIL`.
- This prevents a 09:30/10:00 partial daily K-line run from being treated as a reliable full-day signal. `hermes_review_packet.py` merges `data_health_fail` into review item blocking reasons, and execute mode remains blocked through the system-health gate.

Example:

```bash
/usr/bin/python3 /root/data_health_report.py --output /tmp/data_health_report.json --text
```

Recommended read-only cron:

```bash
*/15 * * * * /usr/bin/python3 /root/data_health_report.py --output /tmp/data_health_report.json --text >> /tmp/data_health_report.log 2>&1
```

Integration rules:

- `system_health_check.py` calls the same data health builder directly, so `data_health.status=FAIL` makes overall health `FAIL` and execute mode remains blocked by `rt_order_intake.py`.
- `hermes_review_packet.py` embeds `/tmp/data_health_report.json` as top-level `data_health`.
- `execution_readiness_report.py` reads `/tmp/quantmind_system_health.json` by default; the reference cron writes this file with `system_health_check.py --output`.
- If `data_health.status=FAIL`, every review item is marked not eligible and Hermes should write `reject` or `hold`.
- `WARN` is degraded context: Hermes may still produce reports and criticism, but should reduce confidence and avoid treating strategy evidence as institutionally reliable until the warning is explained.
- The report is additive and backward compatible; if the cron is not installed yet, packet generation still works, but Hermes receives a missing-file context and should treat data confidence as incomplete.

`scripts/kline_integrity_repair.py` is the matching manual repair tool for the `invalid_latest_ohlc` failure. It fetches Tencent raw `day` rows for the affected latest daily K-lines, compares them with the current DB rows, and emits a hash-confirmed repair plan:

```bash
/usr/bin/python3 /root/kline_integrity_repair.py --output /tmp/kline_integrity_repair.json --text
```

The default mode is dry-run. DB repair requires an explicit matching hash:

```bash
/usr/bin/python3 /root/kline_integrity_repair.py \
  --output /tmp/kline_integrity_repair_apply.json \
  --apply \
  --confirm-plan-hash <plan_hash> \
  --text
```

The tool backs up current rows under `/tmp/kline_integrity_backups/`, updates only the target `klines` daily rows in the plan, marks repaired rows with `data_source='tencent_day_repair'`, and never submits orders. `kline_batch.py` and `quantmind_daily_pipeline.py` now also reject future Tencent rows where `high < low`, prices are non-positive, or `open/close` fall outside `low..high`.

K-line ingestion reliability:

- `kline_batch.py` defaults to `KLINE_BATCH_FETCH_COUNT=120` for routine refreshes. This is enough for the 60-day health/history requirements and avoids repeatedly shipping 2000-row-per-symbol SQL files during intraday refreshes.
- Multi-symbol database transactions are disabled by default with `KLINE_BATCH_ALLOW_MULTI_SYMBOL_TRANSACTION=0`. Routine refreshes flush each symbol independently, so one oversized/failed symbol write cannot silently roll back a group of symbols.
- `kline_batch.py` uses a single-instance lock by default at `/tmp/kline_batch.lock`, configurable through `KLINE_BATCH_LOCK_FILE`. If a previous run is still active, the next cron invocation exits cleanly without starting a second overlapping writer. Use `--no-lock` or `KLINE_BATCH_DISABLE_LOCK=1` only for tests/debug.
- `kline_batch.py` now counts `ok/fail` from actual `_flush_batch()` write results. A symbol is no longer counted as `ok` just because Tencent fetch succeeded; failed DB writes are surfaced in the market summary and logs.
- If an operator needs a full historical backfill, run it explicitly with a larger `KLINE_BATCH_FETCH_COUNT` and review the logs separately from the intraday refresh job.

Server rollout on 2026-06-12:

- `/root/data_health_report.py` was deployed and a read-only cron was added.
- `/root/kline_integrity_repair.py` was deployed as a manual hash-gated repair tool.
- `kline_batch.py` and `quantmind_daily_pipeline.py` were updated to skip future invalid OHLC rows instead of writing them to `klines`.
- Initial data health found HK `invalid_latest_ohlc=6`; five stale historical rows were repaired with plan hash `f6bfed8376d4541c`, then one current-day row was repaired with plan hash `b04014269ac2251a`.
- Repair backups were written under `/tmp/kline_integrity_backups/`.
- Post-repair `invalid_latest_ohlc=0`; data health became `WARN` because HK latest-date coverage remained below 80% and some active HK/US symbols were still stale or missing daily K-lines.
- `hermes_review_packet.py` exposed `data_health.status=WARN` and `submits_orders=false`; no simulation execution job was enabled.

K-line ingestion retry rollout on 2026-06-12:

- Investigation showed high-liquidity symbols such as `01810`, `01398`, `00883`, `01929`, and `00101` had valid 2026-06-12 Tencent K-line rows, but the DB remained stale after the old 10-symbol batch writer.
- Server backups: `/root/quantmind_backup_20260612_120155_kline_batch_retry` and `/root/quantmind_backup_20260612_121022_kline_individual_flush`.
- `kline_batch.py` now uses 120-row routine fetches and per-symbol flushes by default.
- Controlled small-batch smoke updated `01398`, `00883`, `01929`, and `00101` to 2026-06-12.
- Full server refresh improved HK latest-date coverage from `69.07%` (`201/291`) to `90.38%` (`263/291`) and removed the `latest_kline_coverage_below_80pct` warning.
- Universe hygiene improved from HK `92` problem symbols (`31.62%`) to `30` problem symbols (`10.31%`).
- `data_health.status` remained `FAIL` during the session only because the latest `signal_v4_20260612` feature run was generated before the full-day cutoff; this is the intended full-day signal gate.

K-line single-instance/write-count hardening on 2026-06-12:

- This is a no-loss operational change for Hermes and existing jobs. Existing cron lines can keep calling `/usr/bin/python3 -u /root/kline_batch.py`; the script itself handles lock acquisition.
- The lock prevents concurrent cron/manual K-line writers from racing on the same `klines` rows or producing misleading interleaved logs.
- The `ok/fail` market summaries now represent database write outcomes, not only fetch outcomes. This makes `data_health_report.py`, `system_health_check.py`, and Hermes packet interpretation less optimistic after partial DB failures.
- No schema, table, Hermes judgment contract, simulation execution path, or Feishu/Hermes bridge behavior changes are required.
- Server backup before deployment: `/root/quantmind_backup_20260612_123414_kline_lock_write_counts`.
- Server `py_compile` passed for `/root/kline_batch.py`.
- Server smoke confirmed a mocked write failure returns `ok/fail=(0, 2)` instead of counting fetched symbols as successful.
- Server smoke confirmed a second process is blocked by the single-instance lock while the first process holds it.
- Rollback is safe: restore the previous `/root/kline_batch.py` backup. Data already written through the upsert path remains compatible with the existing `klines` schema.

Legacy strategy runner new-entry gate on 2026-06-12:

- `quantmind_strategy_runner.py` now defaults to `QM_STRATEGY_ALLOW_NEW_POSITIONS=0`.
- With the default setting, the script can still refresh account context, evaluate existing holdings, run stop-loss/take-profit exits, sync portfolio cash, send reports, and update heartbeat.
- New BUY orders from legacy DB `engine_signal_scores` require `QM_STRATEGY_ALLOW_NEW_POSITIONS=1` in the environment. This is intentionally separate from v5 `alert-sim`; it should stay off while v5/Hermes uses `rt_order_intake.py` as the alert-specific simulation path.
- `config/crontab.txt` makes the default explicit with `QM_STRATEGY_ALLOW_NEW_POSITIONS=0` on the legacy runner line.
- No Hermes packet schema, judgment schema, v5 alert format, or `rt_order_intake.py` execute gate changed.
- Server backup before deployment: `/root/quantmind_backup_20260612_123929_legacy_runner_new_entry_gate`.
- Server `py_compile` passed for `/root/quantmind_strategy_runner.py`.
- Server smoke imported the runner without contacting API/DB and confirmed default new-entry gate is disabled, explicit `QM_STRATEGY_ALLOW_NEW_POSITIONS=1` enables it, and candidate selection returns no new orders when disabled.

Daily full-day signal gate rollout on 2026-06-12:

- Server scripts backed up to `/root/quantmind_backup_20260612_114400_daily_signal_full_day_gate` before deploying `signal_engine_v4.py` and `data_health_report.py`.
- Server `py_compile` passed for both scripts.
- At `2026-06-12 11:44:34 +0800`, `signal_engine_v4.py --preflight --json` reported `status=WARN`, `trade_date=2026-06-12`, `candidate_count=201`, `write_blocked=true`, and `block_reason=current_session_before_daily_signal_ready_time_16:15`.
- A direct `signal_engine_v4.py` smoke exited `0` after logging a safe skip and did not write a new signal run.
- `data_health_report.py` reported `status=FAIL`, `feature_run.status=FAIL`, and notes `current_session_before_daily_signal_ready_time` plus `latest_daily_signal_run_generated_before_full_day_cutoff` for the existing `signal_v4_20260612` run generated at `09:34`.
- `system_health_check.py --json` returned `FAIL`, and `hermes_review_packet.py` produced `health.status=FAIL`, `data_health.status=FAIL`, `execution_safety.submits_orders=false`; review items included `system_health_fail` and `data_health_fail`.
- Visibility update backed up to `/root/quantmind_backup_20260612_114654_data_health_notes_visibility`; health logs now include the exact feature-run notes causing the FAIL.

Interpretation: during the HK trading session, K-line collection can continue, but daily v4 signals from incomplete same-day bars are not trustworthy for trade approval. The system should return to non-FAIL only after a post-cutoff/full-day signal run replaces the partial run, subject to the remaining coverage and universe warnings.

### Universe hygiene report

`scripts/universe_hygiene_report.py` explains the stale/missing active-symbol warnings from `data_health_report.py`. It is read-only and does not update `stocks.is_active`.

It classifies active HK/US symbols into:

- `keep_active`;
- `monitor_or_refetch_after_close` for one-day lag;
- `candidate_refetch_then_review` for short stale periods or thin recent history;
- `candidate_deactivate_or_symbol_mapping` for severe stale symbols;
- `candidate_remove_from_stock_universe` for unusual symbol format or non-stock entries.

Example:

```bash
/usr/bin/python3 /root/universe_hygiene_report.py --output /tmp/universe_hygiene_report.json --text
```

Recommended read-only cron:

```bash
10 * * * * /usr/bin/python3 /root/universe_hygiene_report.py --output /tmp/universe_hygiene_report.json --text >> /tmp/universe_hygiene_report.log 2>&1
```

Integration rules:

- `hermes_review_packet.py` embeds the report as top-level `universe_hygiene`.
- The report exposes top-level `status`, `summary`, `active_symbol_count`, `problem_count`, and `high_priority_count` for Hermes/operator triage. `WARN` means the active universe has stale, thin-history, low-liquidity, or format issues that need review; it does not submit or block orders by itself.
- The report emits `stock_universe_hygiene_proposal_v1` with `manual_review_required=true` and `auto_applied=false`.
- `universe_hygiene_report.py` is for cleaning the active stock universe; `universe_rank_report.py` is for selecting a v5 watchlist candidate from the clean/enough universe.
- Do not auto-deactivate symbols solely from this report. Use it to review whether a symbol needs refetch, symbol mapping correction, manual deactivation, or exclusion from v5 watchlists.

Manual promotion tool:

`scripts/stock_universe_hygiene_promote.py` is the only write-capable helper in this layer, and it is manual-only. It must not be installed as cron and it does not submit orders, restart services, or touch watchlists.

Safe dry-run:

```bash
/usr/bin/python3 /root/stock_universe_hygiene_promote.py --report-file /tmp/universe_hygiene_report.json --symbol hkHSI --text
```

Apply requires all of the following:

- explicit `--symbol` selection for every row to change;
- `--apply`;
- matching `--confirm-proposal-hash` from the dry-run output;
- no open `positions` row with status `active` or `holding` and positive quantity for the selected symbol;
- backup of matching `stocks` rows under `/tmp/stock_universe_hygiene_backups/` before the update.

Default allowed action is only `candidate_remove_from_stock_universe`, which is intended for obvious non-stock or invalid-format rows such as index placeholders. Severe stale ordinary stocks such as `00011` or `SQ` are rejected by default even if they are high-priority candidates. To deactivate those, an operator must first decide that refetch/symbol mapping is not the right fix and then pass an explicit extra allow-list, for example `--allow-action candidate_deactivate_or_symbol_mapping`.

Server rollout on 2026-06-12:

- `/root/universe_hygiene_report.py` was deployed and a read-only cron was added.
- Smoke report showed HK active universe `293` symbols, `99` problem symbols, and `11` high-priority candidates.
- High-priority HK examples included severely stale symbols such as `00011`, `00489`, `00658`, `00663`, `00754`, `00845`, `00925`, `00959`, `03333`, plus non-stock/format candidates `hkHSI` and `hkHSTECH`.
- US active universe had `63` symbols, `10` problem symbols, with `SQ` as a high-priority stale candidate.
- `hermes_review_packet.py` exposed `universe_hygiene.schema=universe_hygiene_report_v1` and `auto_applies_stock_changes=false`.
- No `stocks` rows were changed, no v5 restart was performed, and no execution cron was enabled.

Promotion rollout on 2026-06-12:

- `/root/stock_universe_hygiene_promote.py`, `/root/hermes_v5_crontab.txt`, and `/root/HERMES_V5_INTEGRATION.md` were backed up to `/root/quantmind_backup_20260612_111756_stock_universe_hygiene_promote` before upload.
- Server `py_compile` passed for `/root/stock_universe_hygiene_promote.py`.
- Dry-run selected only `hkHSI` and `hkHSTECH` with proposal hash `afdc7a1159a7c95a`; stale ordinary stock `00011` was rejected with `recommended_action_not_allowed`.
- The hash-confirmed apply changed only `hkHSI` and `hkHSTECH` from `stocks.is_active=true` to `false`, with row backup `/tmp/stock_universe_hygiene_backups/stocks_20260612_111913.json`.
- Post-apply hygiene showed HK active universe `291`, `9` high-priority HK stale ordinary stocks, and no `candidate_remove_from_stock_universe` rows. US still has `SQ` as a severe stale candidate.
- Post-apply data health remained `WARN`: HK latest-date coverage `68.73%`, US stale-symbol warning, invalid latest OHLC `0`, and fresh signal dates.
- `hermes_review_packet.py` smoke produced `hermes_signal_review_packet_v1`, `health.status=WARN`, `execution_safety.submits_orders=false`, `data_health.status=WARN`, and `universe_hygiene.source.auto_applies_stock_changes=false`.
- No ordinary stale stocks were deactivated, no watchlist was promoted, no service was restarted, and no execution cron was enabled.

Universe hygiene summary/protection hardening on 2026-06-12:

- `universe_hygiene_report.py` now emits top-level `status`, `summary`, `active_symbol_count`, `problem_count`, and `high_priority_count`, so Hermes can read the hygiene severity without walking market internals.
- `stock_universe_hygiene_promote.py` now blocks apply if a selected symbol appears in `positions` with status `active` or `holding` and positive quantity.
- This prevents a universe cleanup from silently removing a symbol that is still part of the user or simulation advisory/reporting surface.
- This is still manual-only and hash-confirmed; no cron, watchlist, service, or execution path is changed by this hardening.
- Server backup before deployment: `/root/quantmind_backup_20260612_124452_universe_hygiene_summary_position_guard`.
- Server `py_compile` passed for `/root/universe_hygiene_report.py` and `/root/stock_universe_hygiene_promote.py`.
- Server smoke report emitted `status=WARN`, `active_symbol_count=354`, `problem_count=40`, and `high_priority_count=10`.
- Server monkeypatch smoke confirmed promotion apply is blocked with `selected_symbol_has_open_position` for a held symbol and does not call the apply path.

### Read-only server observations on 2026-06-12

A read-only preflight against the current server showed:

- Docker containers `quantmind`, `quantmind-celery`, `quantmind-db`, and `quantmind-redis` are running and healthy.
- Current `/root/rt_signal_engine_v5.py` is running by `nohup`; `rt_signal_engine_v5` systemd service is inactive.
- The current server has `/tmp/rt_signal_alert.json`, but no `/tmp/rt_signal_alerts.jsonl` queue yet.
- Current server alerts do not include `signal_id`, `confirmed`, or `full_score`, so they are not safe for alert-specific execution.
- No Hermes bridge cron line is currently installed.
- Latest active HK/US daily K-lines were `2026-06-11`, while latest `signal_v4/v4_full` rows were still `2026-06-10`; new openings should be blocked while this remains true.
- Portfolio 8 positions use status `holding`.
- The server `engine_feature_runs` schema uses `expected_symbols`, `ready_symbols`, and `missing_symbols`; the local scripts support these names as well as `expected_count`, `ready_count`, and `missing_count`.

Treat these as pre-rollout observations, not as permanent assumptions. Re-run `system_health_check.py --json` after deployment.

### Candidate smoke on 2026-06-12

The candidate files were staged side-by-side under `/root/quantmind_v5_candidate_20260612_015004` without replacing active `/root/*.py` scripts.

Observed:

- `python3 -m py_compile *.py` passed on server Python 3.12.
- `system_health_check.py --json` executed and returned `FAIL`, as intended, because `signal_v4/v4_full` latest date was `2026-06-10` while latest K-lines were `2026-06-11`.
- `hermes_review_packet.py --ephemeral-state --stdout --output ''` executed and returned a packet with `health_status=FAIL`.
- The packet saw current old-style alerts but marked `eligible_count=0` because they lacked `confirmed`, `full_score`, and `generated_at`, and because system health was failing.

This is the desired fail-closed behavior. Do not treat these old-style alerts as tradable v5 alerts.

### Server repair on 2026-06-12

The active `/root` scripts were backed up to `/root/quantmind_backup_20260612_015502` before deployment.

Deployed active scripts:

- `signal_engine_v4.py`
- `kline_batch.py`
- `quantmind_daily_pipeline.py`
- `update_portfolio_prices.py`
- `quantmind_strategy_runner.py`
- `rt_alert_bridge.py`
- `rt_signal_engine_v5.py`
- `rt_order_intake.py`
- `system_health_check.py`
- `portfolio_report.py`
- `hermes_review_packet.py`

No crontab change was made, v5 was not restarted, and `alert-sim` was not enabled.

Post-deploy checks:

- `/root/signal_engine_v4.py --preflight --json` returned `status=OK`, `trade_date=2026-06-11`, `run_id=signal_v4_20260611`, and `candidate_count=325`.
- One manual `/root/signal_engine_v4.py` run completed `325/325` symbols and wrote `signal_v4_20260611`.
- `engine_signal_scores` now has `2026-06-11|325|22 BUY|129 HOLD|174 SELL`.
- `engine_feature_runs` now has `signal_v4_20260611|signal_ready|325|325|0`.
- `/root/system_health_check.py --json` improved from `FAIL` to `WARN`; remaining WARN is expected until the new v5 JSONL queue exists.
- `quantmind_strategy_runner.py` quality gate rejects high-score BUY rows with low RR, for example RR `0`, `1.36`, and `1.37` were rejected by the `QM_STRATEGY_MIN_RR_RATIO=1.5` rule.

### v5 systemd rollout on 2026-06-12

Old `/tmp/rt_signal_*` files were moved to `/root/rt_signal_tmp_backup_20260612_020535`.

`rt_signal_engine_v5.py` is now supervised by systemd:

```bash
systemctl is-active rt_signal_engine_v5
```

Expected current state:

- `rt_signal_engine_v5.service` is enabled and active.
- Main process is `/usr/bin/python3 -u /root/rt_signal_engine_v5.py`.
- `/tmp/rt_signal_alerts.jsonl` exists and grows append-only.
- `/tmp/rt_signal_alert.json` is written by the new v5 format.
- `system_health_check.py --json` returns `OK` when v4 signals are fresh and v5 alerts satisfy the contract.

One read-only cron job was added after backing up crontab to `/root/crontab_backup_20260612_020855.txt`:

```bash
* * * * * /bin/bash -lc "cd /root && [ -f /root/.quantmind_env ] && . /root/.quantmind_env; /usr/bin/python3 /root/hermes_review_packet.py --output /tmp/hermes_signal_review_packet.json >> /tmp/hermes_review_packet.log 2>&1"
```

This job only writes `/tmp/hermes_signal_review_packet.json`. It does not call `rt_alert_bridge.py`, does not write Hermes judgments, and does not submit orders.

Post-rollout observed packet:

- `schema=hermes_signal_review_packet_v1`
- `health_status=OK`
- `review_item_count=20`
- `eligible_count=3`
- `alert_selection` records raw source count and BUY/SELL/WATCH distribution.
- `execution_safety.review_only=true`
- `execution_safety.submits_orders=false`

No bridge cron, `alert-sim`, or `legacy-sim` job is enabled.

### Alert quality observation on 2026-06-12

`alert_quality_report.py` was deployed to `/root/alert_quality_report.py`.

A read-only cron job was added after backing up crontab to `/root/crontab_backup_20260612_022355.txt`:

```bash
*/15 * * * * /usr/bin/python3 /root/alert_quality_report.py --output /tmp/rt_alert_quality_report.json --text >> /tmp/rt_alert_quality_report.log 2>&1
```

This job only refreshes `/tmp/rt_alert_quality_report.json` and appends a text summary to `/tmp/rt_alert_quality_report.log`.

First observed report:

- `total_alerts=55`
- `BUY=14`, `SELL=8`, `WATCH=33`
- `directional_confirmed_rate=36.36%`
- `directional_validation_pass_rate=36.36%`
- `packet_eligible_rate=20%`
- symbol conflicts included `NFLX` and `ZH`
- recommendations included keeping execution disabled until more session data and better v5 filtering are available.

This supports keeping `alert-sim` disabled.

### v5 volume trigger hardening

`rt_signal_engine_v5.py` now computes volume anomaly ratio as:

```text
current cumulative volume / (20-day average daily volume * elapsed_session_fraction)
```

Session fractions use regular session minutes:

- HK: 330 minutes, with the lunch break handled.
- US: 390 minutes.

The default volume anomaly threshold remains `3.0`, but it is now compared against expected cumulative volume instead of one-minute average volume. This should reduce WATCH noise from normal mid-session cumulative volume.

Server rollout:

- Previous `/root/rt_signal_engine_v5.py` was backed up to `/root/quantmind_backup_20260612_022750/rt_signal_engine_v5.py`.
- `rt_signal_engine_v5.service` was restarted after deploy and remained active.
- Sample US mid-session volume ratio changed to about `1.01` for normal cumulative volume and about `5.78` for a true high-volume case.
- No new `成交量異動` alert appeared in the observed post-restart window.

### Confirmed-only Hermes packet on 2026-06-12

`hermes_review_packet.py` now defaults to confirmed directional alerts only. The deployed server packet observed after this change had:

- `health_status=OK`
- `source_alert_count=55`
- `confirmed_directional_count=8`
- `unconfirmed_directional_count=14`
- `review_item_count=8`
- `review_types=["BUY","SELL"]`
- `all_review_items_confirmed=true`
- `eligible_count=4`
- `execution_safety.submits_orders=false`

This means unconfirmed `BUY`/`SELL` alerts remain visible for diagnostics, but Hermes does not dry-run or judge them unless an operator explicitly runs the packet with `--include-unconfirmed`. After the later emission hardening, new unconfirmed candidates are emitted as `WATCH` rows with `candidate_signal_type`, so they remain visible without polluting the directional queue.

The later alert quality scan at `2026-06-12T02:36:56` showed:

- `total_alerts=72`
- `BUY=25`, `SELL=13`, `WATCH=34`
- `directional_alerts=38`
- `directional_confirmed_rate=44.74%`
- `directional_validation_pass_rate=44.74%`
- latest packet `review_items=8`, `eligible=4`
- remaining symbol conflicts included `NFLX` and `ZH`

Even with health `OK`, these quality numbers are not enough to enable `alert-sim`. Keep execution disabled until at least one full HK session and one full US session show stable confirmation quality, low conflict rate, and explainable dry-run acceptance/rejection reasons.

### v5 watchlist provenance

`rt_signal_engine_v5.py` now loads its scan universe from a JSON watchlist file before falling back to the built-in default list. The default server path is:

```bash
/root/rt_signal_watchlist.json
```

Repository reference config:

```bash
config/rt_signal_watchlist.json
```

The file format is:

```json
{
  "schema": "rt_signal_watchlist_v1",
  "markets": {
    "HK": {"symbols": ["00700", "03690"]},
    "US": {"symbols": ["AAPL", "MSFT"]}
  }
}
```

Environment overrides are available for emergency/manual experiments:

```bash
RT_SIGNAL_HK_WATCHLIST=00700,03690 RT_SIGNAL_US_WATCHLIST=AAPL,MSFT /usr/bin/python3 /root/rt_signal_engine_v5.py
```

Runtime behavior:

- valid JSON config gives each market `watchlist_source=file`;
- env overrides give only the overridden market `watchlist_source=env`;
- missing or invalid config falls back to the built-in list and logs a warning;
- every new alert carries `watchlist_id`, `watchlist_source`, and `watchlist_count`.

`alert_quality_report.py` now summarizes `by_watchlist_source` and recommends restarting v5 with a configured watchlist when directional alerts are missing watchlist metadata. Older alerts in `/tmp/rt_signal_alerts.jsonl` will naturally show `missing` until enough new v5 alerts are produced after restart.

Server rollout on 2026-06-12:

- `/root/rt_signal_watchlist.json` was deployed from `config/rt_signal_watchlist.json`;
- `rt_signal_engine_v5.service` now sets `RT_SIGNAL_WATCHLIST_FILE=/root/rt_signal_watchlist.json`;
- v5 was restarted under systemd and stayed active;
- startup log showed `HK=63`, `US=32`, `watchlist_source=file`, `watchlist_id=93b0e133f61908ff`;
- `system_health_check.py --json` stayed `OK`;
- `alert_quality_report.py` continued to recommend keeping execution disabled because current queued alerts were older and still missing watchlist metadata.

### v5 strategy config

`rt_signal_engine_v5.py` now loads trigger thresholds and risk multiples from a versioned strategy config before falling back to defaults. The default server path is:

```bash
/root/rt_signal_strategy_config.json
```

Repository reference config:

```bash
config/rt_signal_strategy_config.json
```

The default config matches the previous hard-coded behavior:

- `BUY` confirmed when `full_score >= 0.25`;
- `SELL` confirmed when `full_score <= -0.25`;
- volume anomaly ratio threshold `3.0`;
- signal cooldown `1800` seconds;
- ATR stop/take-profit multiples `2.0` and `3.0`;
- unconfirmed directional candidates are emitted as `WATCH` by default through `emission.emit_unconfirmed_directional_as_watch=true`.

Each new alert carries:

- `strategy_config_id`;
- `strategy_config_source`;
- `strategy_config_version`.

For downgraded unconfirmed directional candidates:

- `signal_type="WATCH"`;
- `candidate_signal_type` contains the original `BUY` or `SELL` candidate;
- `suppressed_directional_reason="unconfirmed_directional"`;
- `execution_candidate=false`;
- candidate risk fields are preserved as `candidate_entry_price`, `candidate_stop_loss`, `candidate_take_profit`, and `candidate_rr_ratio`.

This reduces same-symbol BUY/SELL queue conflicts without losing diagnostic visibility. Set `RT_SIGNAL_EMIT_UNCONFIRMED_DIRECTIONAL_AS_WATCH=0` only for explicit research runs where operators want old-style unconfirmed directional rows.

`alert_quality_report.py` summarizes `by_strategy_config_source` and recommends restarting v5 with configured strategy metadata when scanned directional alerts are missing these fields. Older queue entries naturally show `missing` until new alerts are produced after restart.

Server rollout on 2026-06-12:

- `/root/rt_signal_strategy_config.json` was deployed from `config/rt_signal_strategy_config.json`;
- `rt_signal_engine_v5.service` now sets `RT_SIGNAL_STRATEGY_CONFIG_FILE=/root/rt_signal_strategy_config.json`;
- v5 was restarted under systemd and stayed active;
- startup log showed `strategy_config_id=8c5fa44224376503`, `strategy_config_source=file`, and `version=v5-compatible-default-20260612`;
- default config preserved the previous BUY/SELL confirmation thresholds, volume anomaly threshold, cooldown, and ATR stop/take-profit multiples.

Operator workflow for manually changing strategy behavior:

1. Review `/tmp/strategy_review_report.json`.
2. Review `/tmp/rt_signal_strategy_config_proposal.json`.
3. Manually copy approved changes into `/root/rt_signal_strategy_config.json`.
4. Restart `rt_signal_engine_v5.service`.
5. Confirm new alerts carry the new `strategy_config_id`.

### Ranked universe candidate

`scripts/universe_rank_report.py` is a read-only candidate generator for the v5 scan universe. It does not update the live watchlist and does not submit orders.

It ranks active HKEX/NASDAQ/NYSE stocks using:

- latest daily K-line freshness;
- usable history length;
- 20-day liquidity percentile from K-line amount;
- recent zero-volume days;
- 20-day volatility band;
- 20/60-day momentum;
- latest `signal_v4/v4_full` side and score;
- simulation sizing fit for the 100k HKD paper portfolio, using current `RT_ORDER_POSITION_SIZE_PCT` semantics. HK symbols whose one-lot notional is above the per-position allocation are kept visible in `top_ranked` but blocked from the candidate watchlist with `sim_allocation_below_one_lot`.

Default outputs:

```bash
/usr/bin/python3 /root/universe_rank_report.py --output /tmp/universe_rank_report.json --watchlist-output /tmp/rt_signal_watchlist_candidate.json --text
```

Important behavior:

- `/tmp/universe_rank_report.json` is Hermes context;
- `/tmp/rt_signal_watchlist_candidate.json` is a candidate file for human review;
- `/tmp/watchlist_diff_report.json` compares the live watchlist with the candidate file and explains additions/removals using universe blockers and, when needed, universe hygiene issues;
- `/root/rt_signal_watchlist.json` is not changed automatically;
- candidate JSON has `source.manual_review_required=true` and `source.auto_applied=false`;
- `hermes_review_packet.py` reads `/tmp/universe_rank_report.json` into `universe_context` when available.
- `hermes_review_packet.py` reads `/tmp/watchlist_diff_report.json` into `watchlist_diff` when available.
- `source.sim_equity_hkd`, `source.sim_position_size_pct`, and `source.sim_max_alloc_hkd` document the lot-aware sizing assumptions used for candidate filtering.
- each market exposes `top_ranked` for quick review and `ranked_symbols` as compact full ranked context for diff/audit lookups. Hermes should use `ranked_symbols` when explaining why a live symbol is not in the candidate watchlist.

If `markets.HK.blocker_counts.sim_allocation_below_one_lot` is high, Hermes should treat it as a universe construction issue for the 100k HKD simulation account. Do not approve around intake sizing. Prefer reviewing the watchlist mix, paper-account sizing policy, or whether high-price HK names should remain observation-only until account size or allocation policy changes.

Recommended read-only cron:

```bash
0 * * * * /usr/bin/python3 /root/universe_rank_report.py --output /tmp/universe_rank_report.json --watchlist-output /tmp/rt_signal_watchlist_candidate.json --text >> /tmp/universe_rank_report.log 2>&1
5 * * * * /usr/bin/python3 /root/watchlist_diff_report.py --output /tmp/watchlist_diff_report.json --text >> /tmp/watchlist_diff_report.log 2>&1
```

`watchlist_diff_report.py` is read-only. It sets `source.manual_review_required=true`, `source.auto_applies_watchlist=false`, and `source.submits_orders=false`. It is meant to make live-vs-candidate changes auditable before any operator edits `/root/rt_signal_watchlist.json`. When a live symbol is not present in the ranked universe but appears in `/tmp/universe_hygiene_report.json`, removals are explained with `hygiene:*` blockers such as stale K-lines or missing history. Healthy active symbols missing from the ranked report are marked `active_universe_not_ranked`; symbols missing from both ranked and active-universe hygiene context are marked `not_in_active_or_ranked_universe`.

Each market also includes `ranked_coverage`, comparing active-universe hygiene context with ranked-universe context. `active_not_ranked_count` means active symbols did not receive ranked metrics and should be investigated before promoting a watchlist candidate; `ranked_not_active_count` means ranked output contains symbols missing from the current active-universe context. Both are diagnostics only and do not block packet generation by themselves.

The report emits `proposal` with schema `rt_signal_watchlist_change_proposal_v1`. This is a machine-readable, hash-stamped review artifact containing per-market `add_symbols`, `remove_symbols`, and `remove_symbols_missing_active_universe`. It sets `manual_review_required=true`, `auto_applied=false`, `does_not_restart_services=true`, and `does_not_submit_orders=true`. Hermes may cite `proposal.proposal_hash` in a recommendation, but operators must still manually edit `/root/rt_signal_watchlist.json` and plan any service restart separately.

`scripts/watchlist_promote.py` is a manual-only helper for applying a reviewed watchlist proposal without hand-editing JSON. Default mode is dry-run and only writes a promotion report. Apply mode requires:

- `--apply`;
- `--confirm-proposal-hash <proposal.proposal_hash>`;
- current `/root/rt_signal_watchlist.json` hash still matching the source hash recorded in `/tmp/watchlist_diff_report.json`.

On apply it backs up the target watchlist first and writes the proposed `rt_signal_watchlist_v1`; it does not restart `rt_signal_engine_v5.service`, does not install cron, and does not submit orders. Operators must restart the service manually only after reviewing the written file.

The promotion report includes `current_watchlist_id` and `proposed_watchlist_id` using the same digest algorithm as `rt_signal_engine_v5.py`. After a manual apply and service restart, confirm new v5 alerts carry `watchlist_id=<proposed_watchlist_id>` before trusting watchlist-scoped alert quality, outcome, or strategy learning reports.

Server rollout on 2026-06-12:

- `universe_rank_report.py` was deployed to `/root/universe_rank_report.py`;
- previous packet/docs/cron files were backed up to `/root/quantmind_backup_20260612_084937_universe_rank`;
- first report observed `HK symbols=286 candidates=79/80` and `US symbols=62 candidates=20/50`;
- top HK candidates included `00700`, `01398`, `03988`, `00939`, and `03888`;
- top US candidates included `ASML`, `CRWD`, `DDOG`, `JPM`, and `UNH`;
- generated `/tmp/rt_signal_watchlist_candidate.json` had `manual_review_required=true` and `auto_applied=false`;
- `hermes_review_packet.py` exposed `universe_context.schema=universe_rank_report_v1`;
- packet `execution_safety.submits_orders=false` remained unchanged.

Operator workflow for changing the live v5 universe:

1. Review `/tmp/universe_rank_report.json` and `/tmp/rt_signal_watchlist_candidate.json`.
2. Compare candidate changes with current `/root/rt_signal_watchlist.json`.
3. Manually copy approved symbols into `/root/rt_signal_watchlist.json`.
4. Restart `rt_signal_engine_v5.service`.
5. Confirm new alerts carry the new `watchlist_id` and `watchlist_source=file`.

Recommended server checks:

```bash
docker exec quantmind-db psql -U quantmind -d quantmind -t -A -c "select count(*), max(timestamp) from klines where interval='day';"
docker exec quantmind-db psql -U quantmind -d quantmind -t -A -c "select trade_date, count(*) from engine_signal_scores group by trade_date order by trade_date desc limit 5;"
tail -n 20 /tmp/rt_signal_alerts.jsonl
cat /tmp/rt_signal_state.json
systemctl status rt_signal_engine_v5
```

Minimum health rules Hermes should enforce before trusting v5:

- Latest K-line date is not stale for the relevant market.
- v5 process has been running continuously.
- Alert queue is parseable JSONL.
- Confirmed BUY/SELL alerts include `entry_price`, `stop_loss`, `take_profit`, `full_score`, and `signal_id`.
- `rt_order_intake.py` dry-run accepts/rejects alerts for explainable reasons.
- Execute-mode intake requires a fresh Hermes judgment for the same `signal_id`.
- Portfolio report JSON is generated successfully before Hermes writes trade judgments.
- No simulation execution mode is enabled unless explicitly intended.

### Health check script

Deploy and run:

```bash
/usr/bin/python3 /root/system_health_check.py
```

Machine-readable output:

```bash
/usr/bin/python3 /root/system_health_check.py --json
```

The script is read-only. It checks:

- active HK/US K-line latest dates;
- latest `signal_v4/v4_full` score date and BUY/HOLD/SELL counts;
- recent `signal_v4_*` feature runs;
- portfolio 8 position status distribution;
- portfolio 8 cash/current capital row;
- portfolio 8 `positions` vs `sim_trades` ledger reconciliation through `sim_position_reconcile.py`;
- v5 latest alert file and JSONL queue parseability;
- whether `rt_signal_engine_v5.py` is running.

Simulation ledger health distinguishes structural drift from valuation drift:

- missing/open/stale position rows, quantity/cost mismatch, or trade-ledger conflicts remain `FAIL`;
- current price, market value, unrealized P&L, P&L rate, weight, or portfolio-total-only differences are `OK` only when the differing open-position rows have positive current price/market value and `positions.updated_at` is not older than the reconcile `price_date`; this means the simulation ledger structurally matches `sim_trades` and the difference is a fresh live/near-live valuation snapshot;
- valuation-only differences without that freshness evidence remain `WARN`, because stale DB prices should not be treated as reliable live valuation.

`update_portfolio_prices.py` now also updates `unrealized_pnl_rate` and portfolio `current_capital`/`total_value` after refreshing position prices.

Hermes should treat `FAIL` as "do not execute new trades". `WARN` can still send reports, but should be described as degraded.

### Reporting checks

Recommended read-only report checks:

```bash
/usr/bin/python3 /root/portfolio_report.py --output /tmp/portfolio_report.json --text
/usr/bin/python3 /root/portfolio_report.py --text
/usr/bin/python3 /root/sim_position_reconcile.py --output /tmp/sim_position_reconcile_report.json --text
/usr/bin/python3 /root/market_context_report.py --output /tmp/market_context_report.json --text
/usr/bin/python3 /root/alert_quality_report.py --json >/tmp/rt_alert_quality_report.json
/usr/bin/python3 /root/alert_quality_report.py --text
/usr/bin/python3 /root/rt_signal_outcome_report.py --output /tmp/rt_signal_outcome_report.json --text
/usr/bin/python3 /root/hermes_judgment_audit_report.py --output /tmp/hermes_judgment_audit_report.json --text
```

Hermes should use `/tmp/portfolio_report.json` as part of its prompt/context when judging v5 alerts. The Hermes review packet already embeds the same portfolio context and exposes `portfolio_risk` directly, but `execution_readiness_report.py` needs a fresh `/tmp/portfolio_report.json` snapshot for portfolio-performance, trade-review, and freshness gates.

### Alert quality report

`scripts/alert_quality_report.py` is a read-only session review for v5 alerts. It reads `/tmp/rt_signal_alerts.jsonl` and the latest `/tmp/hermes_signal_review_packet.json`.

By default, the report scopes its main metrics to the current v5 sample: the latest directional alert with both `strategy_config_id` and `watchlist_id`, plus other scanned alerts with the same pair. Older legacy alerts and prior config/watchlist versions are counted under `sample_scope.excluded_*` but do not drive the main quality rates or recommendations. Use `--sample-scope all` only for historical research across mixed queue versions.

It reports:

- total alert counts by `BUY`/`SELL`/`WATCH` and market;
- top-level `schema=alert_quality_report_v1`, `status`, and summary counts for Hermes packet summaries;
- directional confirmed rate and validation pass rate;
- recent packet eligible rate and blocking reasons;
- trigger-level counts, average full score, average RR, and queue-marked signed move;
- symbols with conflicting BUY and SELL alerts in the scanned window;
- recommendations for whether to keep observing, tighten v5 filters, or keep execution disabled.

The forward move is diagnostic only: it compares an alert's price with the latest later same-symbol alert in the scanned JSONL queue. It is not a realized P&L calculation and should not be used alone to approve trades.

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/rt_alert_quality_report.json` into top-level `alert_quality_summary` when the file exists.
- This field is read-only session diagnostics. It helps Hermes criticize noisy alert sessions, but it does not approve execution and does not change `rt_order_intake.py` gates.

Example:

```bash
/usr/bin/python3 /root/alert_quality_report.py --output /tmp/rt_alert_quality_report.json --text
```

Optional Feishu report:

```bash
/usr/bin/python3 /root/alert_quality_report.py --send-feishu --text
```

### Alert event store

`scripts/rt_alert_event_store.py` is the durability bridge for the v5 alert queue. It reads `/tmp/rt_signal_alerts.jsonl`, deduplicates by `signal_id`, and prepares an idempotent DB upsert into `rt_signal_alert_events`.

Default mode is dry-run:

```bash
/usr/bin/python3 /root/rt_alert_event_store.py --output /tmp/rt_alert_event_store_report.json --text
```

The dry-run report includes:

- `schema=rt_alert_event_store_report_v1`;
- `schema_hash` for the reviewed DB table contract;
- `batch_hash` for the scanned alert batch;
- raw/deduplicated alert counts and `by_signal_type` summary;
- safety flags confirming that it does not submit orders, change strategy config, or restart services.

Apply mode is intentionally hash-gated:

```bash
/usr/bin/python3 /root/rt_alert_event_store.py \
  --apply \
  --confirm-schema-hash <schema_hash> \
  --output /tmp/rt_alert_event_store_report.json \
  --text
```

Apply mode only creates/updates the audit table and upserts current alert events on `signal_id`. It does not modify the live alert JSONL, does not change Hermes judgments, does not change `rt_order_intake.py` state, and does not submit simulation orders.

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/rt_alert_event_store_report.json` into top-level `alert_event_store` when the file exists.
- Missing or dry-run event-store status does not block packet generation; it tells Hermes/operator that long-term alert history still depends on JSONL retention.
- Once apply mode is reviewed and enabled, the DB table becomes the durable audit trail for comparing alerts, judgments, and later outcome evidence.

Recommended staged cron:

```bash
*/5 * * * * /usr/bin/python3 /root/rt_alert_event_store.py --output /tmp/rt_alert_event_store_report.json --text >> /tmp/rt_alert_event_store.log 2>&1
```

Only after reviewing the emitted `schema_hash`, replace that dry-run line with the hash-confirmed `--apply` line. This is operational audit persistence only; it is not an execution path.

### Market context report

`scripts/market_context_report.py` is a read-only market regime input for Hermes. The current database does not have reliable index or ETF K-lines, so the report uses the active HKEX/NASDAQ/NYSE stock pool itself as a breadth proxy.

It reports by market:

- latest daily K-line date distribution;
- 20/50-day evaluable coverage;
- percentage of symbols above MA20/MA50;
- percentage of symbols up over 1 day;
- average and median 1/5/20-day returns;
- average and median 20-day daily volatility;
- latest v4 `BUY/HOLD/SELL` distribution;
- `risk_on`, `mixed`, or `risk_off` regime classification;
- notes and recommendations for Hermes review.

Example:

```bash
/usr/bin/python3 /root/market_context_report.py --output /tmp/market_context_report.json --text
```

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/market_context_report.json` into `market_context` when the file exists.
- Missing market context does not block packet generation, but Hermes should treat it as reduced confidence.
- In `risk_off` regimes, Hermes should normally `reject`, `hold`, or `reduce` new BUY approvals unless the alert has a clearly documented exception and all execution gates still pass.

Important limitation: this is a stock-pool breadth proxy, not a real index feed and not a news/event model. It improves context, but it does not replace event risk, macro, earnings, or liquidity checks.

### Market context rollout on 2026-06-12

`market_context_report.py` was deployed to `/root/market_context_report.py` and a read-only cron was added:

```bash
*/30 * * * * /usr/bin/python3 /root/market_context_report.py --output /tmp/market_context_report.json --text >> /tmp/market_context_report.log 2>&1
```

The first server run showed:

- HK: `regime=risk_off`, `risk_level=medium`, `symbol_count=285`, `above_ma20_pct=17.61`, `avg_5d_pct=-3.543`, latest date `2026-06-11`, v4 sides `BUY=17/HOLD=105/SELL=150`
- US: `regime=risk_off`, `risk_level=medium`, `symbol_count=62`, `above_ma20_pct=24.53`, `avg_5d_pct=-5.5882`, latest date `2026-06-11`, v4 sides `BUY=5/HOLD=24/SELL=24`
- recommendations: `HK:risk_off_require_reduced_or_rejected_new_buys`, `HK:buy_signals_against_weak_breadth`, `US:risk_off_require_reduced_or_rejected_new_buys`, `US:buy_signals_against_weak_breadth`

The Hermes packet smoke test after deployment showed:

- `health_status=OK`
- `review_item_count=17`
- `market_context.schema=market_context_report_v1`
- `market_context` regimes: HK `risk_off`, US `risk_off`
- `execution_safety.submits_orders=false`

Current interpretation: Hermes should be skeptical of new BUY approvals in both HK and US until breadth improves or the specific alert has a strong documented exception. This does not block advice/reporting; it changes the burden of proof for approvals.

### Market context execute gate

`rt_order_intake.py` now enforces market context in execute mode for new BUY orders. In `risk_off` regimes, a new BUY is rejected unless the Hermes judgment includes:

- `market_regime_exception=true`
- `market_regime_exception_reason` with a specific explanation
- confidence at or above `RT_ORDER_MIN_MARKET_EXCEPTION_CONFIDENCE`, default `0.80`

Server probe after deployment used a confirmed US BUY alert while both HK and US market context were `risk_off`:

- without exception: `rt_order_intake.py --mode execute` returned `status=rejected`, reason `market_context_gate_failed`;
- market reasons included `market_regime_risk_off` and `buy_signals_against_weak_breadth`;
- with explicit high-confidence exception: market gate returned `PASS`;
- the exception probe still returned `api_token_missing` because API credentials were intentionally not provided;
- neither probe produced `order_result`.

This confirms that Hermes approval alone cannot bypass weak market breadth, and a risk-off BUY override must be explicit and auditable.

### Hermes judgment audit

`scripts/hermes_judgment_audit_report.py` is a read-only audit for the local Hermes judgment artifact. It reads `/tmp/hermes_trade_judgments.jsonl`, resolves each judgment's `packet_id` from `/tmp/hermes_review_packet_archive/` when available, and falls back to the latest `/tmp/hermes_signal_review_packet.json` only when needed. The report emits top-level `status=OK` when all observed judgments pass audit and `status=FAIL` when any judgment fails or duplicate signal judgments are found.

It reports:

- total judgment count and decision distribution;
- judgments whose `signal_id` is not in the resolved packet;
- judgments missing `packet_id`;
- judgments whose `packet_id` archive is missing;
- approvals for `eligible_for_approval=false` review items;
- approvals for unconfirmed alerts;
- approvals while health is `FAIL`;
- risk-off BUY approvals without `market_regime_exception=true`;
- approvals while strategy evidence is unresolved or below thresholds;
- expired judgments and duplicate signal judgments.

Example:

```bash
/usr/bin/python3 /root/hermes_judgment_audit_report.py --output /tmp/hermes_judgment_audit_report.json --text
```

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/hermes_judgment_audit_report.json` into `judgment_audit` when the file exists.
- `execution_readiness_report.py` treats `judgment_audit.status=FAIL` as a hard block because un-auditable or contradictory Hermes judgments cannot support execute readiness.
- Missing audit does not block packet generation.
- Any audit recommendation beginning with `fix_or_reject_judgments` or `approvals_conflict_with_execution_gates` should be treated as a hard reason to keep `alert-sim` disabled.

### Hermes judgment audit rollout on 2026-06-12

`hermes_judgment_audit_report.py` was deployed to `/root/hermes_judgment_audit_report.py` and a read-only cron was added:

```bash
*/10 * * * * /usr/bin/python3 /root/hermes_judgment_audit_report.py --output /tmp/hermes_judgment_audit_report.json --text >> /tmp/hermes_judgment_audit_report.log 2>&1
```

The first server run showed:

- `schema=hermes_judgment_audit_report_v1`
- `judgment_count=0`
- `review_item_count=20`
- `eligible_review_item_count=10`
- `reason_counts={}`
- recommendation: `no_hermes_judgments_observed_yet`

The Hermes packet smoke test after deployment showed:

- `health_status=OK`
- `review_item_count=20`
- `judgment_audit.schema=hermes_judgment_audit_report_v1`
- `judgment_audit.judgment_count=0`
- `execution_safety.submits_orders=false`

Current interpretation: Hermes has not written trade judgments yet. This is a clean initial audit state, not a failure. Once Hermes begins writing `/tmp/hermes_trade_judgments.jsonl`, this report becomes the first-line QA for LLM decision quality.

### Hermes judgment event store

`scripts/hermes_judgment_event_store.py` is the durability bridge for Hermes trade judgments. It reads `/tmp/hermes_trade_judgments.jsonl`, joins the latest `/tmp/hermes_judgment_audit_report.json` when available, and prepares idempotent DB upserts into `hermes_trade_judgment_events`.

Default mode is dry-run:

```bash
/usr/bin/python3 /root/hermes_judgment_event_store.py --output /tmp/hermes_judgment_event_store_report.json --text
```

The dry-run report includes:

- `schema=hermes_judgment_event_store_report_v1`;
- `schema_hash` for the reviewed DB table contract;
- `batch_hash` for the scanned judgment batch;
- judgment/event counts, duplicate count, decision distribution, and audit-status distribution;
- safety flags confirming that it does not submit orders, change intake state, change strategy config, or restart services.

Apply mode is hash-gated:

```bash
/usr/bin/python3 /root/hermes_judgment_event_store.py \
  --apply \
  --confirm-schema-hash <schema_hash> \
  --output /tmp/hermes_judgment_event_store_report.json \
  --text
```

Apply mode only creates/updates the audit table and upserts judgment events on a stable `judgment_key`. It does not replace the JSONL judgment contract, does not make an approval executable, does not modify `/tmp/rt_order_intake_state.json`, and does not call the simulation API.

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/hermes_judgment_event_store_report.json` into top-level `judgment_event_store` when the file exists.
- Hermes should continue writing trade judgments to `/tmp/hermes_trade_judgments.jsonl`; the event store is a persistence/audit layer behind that contract.
- Missing or dry-run event-store status does not block packet generation. It tells Hermes/operator that long-term judgment history still depends on JSONL retention.

Recommended staged cron:

```bash
*/10 * * * * /usr/bin/python3 /root/hermes_judgment_event_store.py --output /tmp/hermes_judgment_event_store_report.json --text >> /tmp/hermes_judgment_event_store.log 2>&1
```

Only after reviewing the emitted `schema_hash`, replace the dry-run line with the hash-confirmed `--apply` line. This is decision audit persistence only; it is not an execution path.

### Order intake event store

`scripts/rt_order_intake_event_store.py` is the durability bridge for `rt_order_intake.py` decisions. It reads `/tmp/rt_order_intake_state.json`, extracts both `dry_runs` and `processed`, and prepares idempotent DB upserts into `rt_order_intake_events`.

Default mode is dry-run:

```bash
/usr/bin/python3 /root/rt_order_intake_event_store.py --output /tmp/rt_order_intake_event_store_report.json --text
```

The dry-run report includes:

- `schema=rt_order_intake_event_store_report_v1`;
- `schema_hash` for the reviewed DB table contract;
- `batch_hash` for the current intake-decision batch;
- counts by ledger (`dry_runs` vs `processed`), status (`dry_run`, `rejected`, `submitted`, `error`), and mode;
- safety flags confirming that it does not submit orders, change intake state, change strategy config, or restart services.

Apply mode is hash-gated:

```bash
/usr/bin/python3 /root/rt_order_intake_event_store.py \
  --apply \
  --confirm-schema-hash <schema_hash> \
  --output /tmp/rt_order_intake_event_store_report.json \
  --text
```

Apply mode only creates/updates the audit table and upserts intake decision events. It does not call the simulation API, does not mutate `/tmp/rt_order_intake_state.json`, and does not make a rejected or dry-run item executable.

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/rt_order_intake_event_store_report.json` into top-level `order_intake_event_store` when the file exists.
- Missing or dry-run intake event-store status does not block packet generation. It tells Hermes/operator that long-term intake-decision history still depends on JSON state retention.
- This closes the audit chain between alert review and simulated execution result: v5 alert -> packet -> Hermes judgment -> intake decision -> order result / rejection.

Recommended staged cron:

```bash
*/10 * * * * /usr/bin/python3 /root/rt_order_intake_event_store.py --output /tmp/rt_order_intake_event_store_report.json --text >> /tmp/rt_order_intake_event_store.log 2>&1
```

Only after reviewing the emitted `schema_hash`, replace the dry-run line with the hash-confirmed `--apply` line. This is intake-decision audit persistence only; it is not an execution path.

### Packet archive rollout on 2026-06-12

`hermes_review_packet.py` now archives every generated packet by `packet_id` under:

```bash
/tmp/hermes_review_packet_archive/
```

The judgment schema now requires `packet_id`. Hermes must copy the `packet_id` from the packet into each judgment object.

Server smoke after deployment showed:

- generated packet `packet_id=eb972880ae04164d`
- archive path existed at `/tmp/hermes_review_packet_archive/eb972880ae04164d.json`
- an audit probe with a temporary `hold` judgment and matching `packet_id` resolved `packet_source=packet_archive` and passed
- an audit probe missing `packet_id` failed with `judgment_missing_packet_id`
- live audit remained clean with `judgment_count=0`

This fixes the main historical-audit gap: Hermes judgments can now be checked against the exact packet version Hermes reviewed, not only the latest rolling packet.

### Portfolio risk rollout on 2026-06-12

`portfolio_report.py` now emits portfolio-level risk context and `hermes_review_packet.py` exposes it as top-level `portfolio_risk`.

Server deployment:

- previous `/root/portfolio_report.py`, `/root/hermes_review_packet.py`, docs, and crontab were backed up to `/root/quantmind_backup_20260612_080110_portfolio_risk`;
- `/root/portfolio_report.py` and `/root/hermes_review_packet.py` compiled successfully on server Python;
- `/tmp/portfolio_context.json` was generated read-only;
- `/tmp/hermes_signal_review_packet.json` was regenerated with `--ephemeral-state`, so no dry-run intake ledger was consumed;
- `system_health_check.py --json` returned `status=OK`;
- crontab still did not contain enabled `rt_alert_bridge`, `alert-sim`, or `legacy-sim` execution jobs.

Observed portfolio risk:

- simulation portfolio `8` reported computed fallback total `114526.16` HKD from K-line valuation, while the backend portfolio row reported `14480.62` HKD;
- all 10 `positions.current_price` values for portfolio `8` were zero, so valuation used fallback prices;
- recent/full `sim_trades` implied open symbols `00177`, `00929`, `03328`, and `03888` missing from `positions`;
- `positions` still showed closed symbols such as `00017`, `00743`, `00775`, `00880`, `09922`, `LI`, and `TSLA`;
- portfolio risk was marked `critical` with flags including `all_position_prices_missing_or_zero_in_db`, `fallback_valuation_used`, `portfolio_row_value_disagrees_with_computed_value`, and `positions_table_conflicts_with_trade_ledger`.

Hermes packet impact:

- packet `packet_id=174bc9c477cac8f3` had `health_status=OK` and `review_item_count=20`;
- `portfolio_risk.schema=portfolio_risk_report_v1`;
- all review items had `eligible_for_approval=false`;
- `eligible_count=0`;
- portfolio risk blocking reasons were added to every review item;
- `execution_safety.submits_orders=false`.

Current interpretation: signal generation can continue, but simulation execution must stay disabled until the backend position ledger and price refresh path are reconciled. Hermes can still use reports for advice and diagnostics, but should not approve new simulated orders while the simulation portfolio state is `critical`.

### Simulation position reconciliation

`scripts/sim_position_reconcile.py` is the controlled repair tool for the portfolio 8 `positions` table.

It derives open simulation positions from canonical `sim_trades`, compares them with `positions`, and builds a plan containing:

- missing open positions to insert;
- stale open positions to close;
- existing positions whose quantity, cost, price, market value, P&L, exchange, or status should be updated;
- portfolio `current_capital`/`total_value` summary update using existing `available_cash` plus repaired position value.

Safe dry-run:

```bash
/usr/bin/python3 /root/sim_position_reconcile.py --output /tmp/sim_position_reconcile_report.json --text
```

Apply is deliberately two-step:

```bash
/usr/bin/python3 /root/sim_position_reconcile.py --json
/usr/bin/python3 /root/sim_position_reconcile.py --apply --confirm-plan-hash <plan_hash> --text
```

The apply path:

- requires a matching `plan_hash` from the current dry-run;
- writes a JSON backup of the current `portfolios` and `positions` rows under `/tmp/sim_position_reconcile_backups`;
- updates only the database portfolio/position ledger;
- does not call the simulation order API;
- does not write Hermes judgments;
- does not enable `rt_alert_bridge`, `alert-sim`, or `legacy-sim`.

Use this only to repair accounting state that already follows from `sim_trades`. It is not a substitute for signal review or trade execution gates.

Server repair on 2026-06-12:

- dry-run plan `baf2eb95170df4a0` showed 15 actions: insert `00177`, `00929`, `03328`, `03888`; update `00288`, `00816`, `00867`; close stale `00017`, `00743`, `00775`, `00880`, `09922`, `LI`, `TSLA`; update portfolio total from `14480.62` to `97510.62`;
- apply succeeded with backup `/tmp/sim_position_reconcile_backups/portfolio_8_20260612_081110.json`;
- post-apply dry-run reported `action_count=0`;
- portfolio context reported reconciliation `PASS` and `db_invalid_price_count=0`;
- after refining exit-pressure semantics, simulation portfolio risk dropped from `critical` to `low`;
- health check remained `OK`;
- Hermes packet still had `execution_safety.submits_orders=false`; review items stayed ineligible because current alerts were stale, not because of portfolio data corruption.

Valuation freshness classification on 2026-06-12:

- `system_health_check.py` was updated so valuation-only reconcile actions become `OK` when current position prices are positive and freshly updated versus the daily K-line reconcile `price_date`.
- Server script backup: `/root/quantmind_backup_20260612_113034_health_valuation_freshness`.
- Server smoke showed `simulation_ledger=OK` with detail `positions structurally match sim_trades; fresh live valuation differs from daily-kline reconcile baseline`.
- Hermes packet smoke showed `health.status=WARN`, `execution_safety.submits_orders=false`, and packet `simulation_ledger=OK`.
- Overall health remained `WARN` because `data_health` still reported HK latest-date coverage below 80% and stale-symbol warnings. Treat that as the next reliability target, not as simulation ledger corruption.

### Position review rollout on 2026-06-12

`portfolio_report.py` now emits `position_review` items for existing holdings that need Hermes analysis.

Server smoke after deployment:

- `position_review.schema=portfolio_position_review_v1`;
- `item_count=5`;
- urgency counts: `high=1`, `medium=4`;
- action counts: `reduce_or_exit_review=1`, `risk_review=4`;
- high item: `00929`, because latest v4 was still `BUY` but unrealized P&L was below the -8% review threshold;
- medium risk-review items: `03888`, `00177`, `00816`, `03328`, mainly due v4 risk flags such as Bollinger band touches;
- simulation portfolio risk was `low` after separating true exit pressure from ordinary signal risk flags;
- Hermes packet exposed the same `position_review` and kept `execution_safety.submits_orders=false`;
- no `portfolio_risk:exit_pressure_requires_review_before_new_buy` blocking reason appeared after refinement.

Interpretation: Hermes now receives a structured review queue for existing holdings without turning it into orders. New BUY approvals should only be blocked by true unresolved exit pressure, not by generic risk flags that merely deserve LLM review.

### Hermes position judgment advisory audit

`scripts/hermes_position_judgment_audit_report.py` is a read-only audit for Hermes judgments on `position_review.items`.

This is separate from `hermes_trade_judgments.jsonl`:

- trade judgments review v5 `review_items` and can become one of the required gates for a future `rt_order_intake.py --mode execute`;
- position judgments review existing holdings and are advisory only;
- position judgments must never be consumed by `rt_order_intake.py`;
- position judgments do not call the simulation API and do not submit orders.

Default advisory judgment file:

```bash
/tmp/hermes_position_judgments.jsonl
```

Schema is provided at `config/hermes_position_judgment.schema.json`.

Hermes should append one JSON object per reviewed position item:

```json
{
  "schema": "hermes_position_judgment_v1",
  "packet_id": "copy from hermes_signal_review_packet_v1.packet_id",
  "review_id": "copy from position_review.items[].review_id",
  "portfolio_id": 8,
  "role": "simulation",
  "symbol": "00929",
  "decision": "watch",
  "confidence": 0.78,
  "reviewed_at": "2026-06-12T10:06:00",
  "reviewer": "hermes",
  "advisory_only": true,
  "submits_orders": false,
  "supporting_factors": ["latest signal remains BUY but unrealized loss is elevated"],
  "opposing_factors": ["exit now may crystallize loss before stop confirmation"],
  "risk_notes": ["review again next session before adding new exposure"]
}
```

Valid decisions are `hold`, `watch`, `reduce`, `exit`, and `trail_stop`. For user portfolio items, keep the machine-readable `decision` to `hold` or `watch`; put manual reduce/exit advice in `risk_notes` so no downstream automation can confuse user advice with executable intent. For simulation items, `reduce`, `exit`, and `trail_stop` are still advisory and require a separate gated execution path if an operator later wants to act.

Audit command:

```bash
/usr/bin/python3 /root/hermes_position_judgment_audit_report.py --output /tmp/hermes_position_judgment_audit_report.json --text
```

The audit checks:

- judgment schema and required advisory flags;
- exact `packet_id` and `review_id` linkage through `/tmp/hermes_review_packet_archive/`;
- symbol, portfolio, and role consistency with the reviewed `position_review` item;
- user portfolio action-token violations;
- high-urgency hold/watch decisions with weak rationale;
- expired and duplicate review judgments.

The report emits top-level `status=OK` when no advisory judgments have been observed yet or when all observed position judgments pass the audit. It emits `status=FAIL` when any position judgment violates the advisory contract, mismatches its archived packet item, expires, or duplicates a `review_id`. `execution_readiness_report.py` treats `status=FAIL` as a hard block because unsafe advisory judgments can confuse user-holding advice, simulation position review, and future operator decisions even though they do not submit orders.

Hermes packet integration:

- `hermes_review_packet.py` exposes `position_judgment_contract` at the top level;
- `hermes_review_packet.py` reads `/tmp/hermes_position_judgment_audit_report.json` into `position_judgment_audit` when the file exists;
- missing position judgment audit does not block packet generation;
- `position_judgment_audit` failures should keep the position judgment workflow in advisory review until the JSONL artifact is corrected.

Recommended read-only cron:

```bash
*/10 * * * * /usr/bin/python3 /root/hermes_position_judgment_audit_report.py --output /tmp/hermes_position_judgment_audit_report.json --text >> /tmp/hermes_position_judgment_audit_report.log 2>&1
```

### Signal outcome report

`scripts/rt_signal_outcome_report.py` is a read-only forward outcome evaluator for v5 alerts. It reads `/tmp/rt_signal_alerts.jsonl`, deduplicates directional alerts by `signal_id`, then checks future daily K-lines for the same symbol.

By default, the outcome evidence uses the same current-sample scope as `alert_quality_report.py`: latest `strategy_config_id + watchlist_id` only. This prevents old v5 revisions or legacy alerts without provenance from backing a new config in `rt_order_intake.py` strategy evidence gates. Use `--sample-scope all` for offline research only; do not use the all-scope report as execute evidence.

It reports:

- evaluated directional alert count and duplicate count;
- top-level `status`, `evaluated_signal_count`, `resolved_signal_count`, `pending_signal_count`, `pending_reasons`, and `primary_horizon_metric` for Hermes packet summaries;
- 1/3/5 trading-day signed close return by signal direction;
- win rate by horizon;
- stop/take-profit touch rates using daily high/low after the alert quote date;
- trigger-level, symbol-level, and confirmed/unconfirmed performance;
- strategy-config and watchlist attribution via `strategy_config_id`, `strategy_config_version`, and `watchlist_id`;
- full per-signal `evaluations` plus a shorter `recent_evaluations` view for packet/debug readability;
- recommendations such as keeping shadow mode when the sample is too small.

Example:

```bash
/usr/bin/python3 /root/rt_signal_outcome_report.py --output /tmp/rt_signal_outcome_report.json --text
```

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/rt_signal_outcome_report.json` into `strategy_evidence` when the file exists.
- Missing outcome evidence does not block packet generation, because new alerts may not have future daily K-lines yet.
- Outcome evidence is context for LLM criticism/support, not an execution approval. Hermes must still obey `review_items[].eligible_for_approval`, health status, and the judgment gate.

Important limitation: this report uses daily bars. It can say whether a future daily candle touched stop or target, but it cannot reconstruct exact intraday event order from one daily candle. When stop and target are both touched on the same daily bar, the report marks `first_hit=ambiguous_same_day`.

The attribution fields are additive and backward compatible. Older queued alerts that were emitted before v5 strategy/watchlist provenance was added are grouped under `missing`; new alerts should group by the active `/root/rt_signal_strategy_config.json` digest and `/root/rt_signal_watchlist.json` digest. This lets Hermes compare forward outcome quality by configuration version without changing the live strategy config, watchlist, or execution path.

### Signal outcome rollout on 2026-06-12

`rt_signal_outcome_report.py` was deployed to `/root/rt_signal_outcome_report.py` and a read-only cron was added:

```bash
*/30 * * * * /usr/bin/python3 /root/rt_signal_outcome_report.py --output /tmp/rt_signal_outcome_report.json --text >> /tmp/rt_signal_outcome_report.log 2>&1
```

The first server run showed:

- `raw_alert_count=75`
- `directional_alert_count=40`
- `evaluated_signal_count=40`
- `duplicate_signal_count=0`
- `resolved_signal_count=0`
- `pending_or_invalid_count=40`
- `pending_reasons={"no_future_daily_klines":40}`
- recommendation: `outcome_sample_not_ready_keep_collecting_daily_klines`

This is expected because the current v5 queue is newer than the latest available future daily K-lines. The report should become useful after one or more subsequent trading days have been loaded by the K-line jobs.

The Hermes packet smoke test after deployment showed:

- `health_status=OK`
- `review_item_count=17`
- `strategy_evidence.schema=rt_signal_outcome_report_v1`
- `strategy_evidence.evaluated_signal_count=40`
- `strategy_evidence.resolved_signal_count=0`
- `execution_safety.submits_orders=false`

This adds a feedback channel for Hermes without creating any execution path. Until `resolved_signal_count` and horizon-level win/return metrics have meaningful sample size, Hermes should treat outcome evidence as "insufficient data", not as support for execution.

### Signal outcome event store

`scripts/rt_signal_outcome_event_store.py` is the durability bridge for forward outcome evidence. It reads `/tmp/rt_signal_outcome_report.json`, persists individual `evaluations` by `signal_id`, and keeps the full per-signal outcome JSON for later strategy review.

Default mode is dry-run:

```bash
/usr/bin/python3 /root/rt_signal_outcome_event_store.py --output /tmp/rt_signal_outcome_event_store_report.json --text
```

The dry-run report includes:

- `schema=rt_signal_outcome_event_store_report_v1`;
- `schema_hash` for the reviewed DB table contract;
- `batch_hash` for the current outcome batch;
- source report status, current `sample_scope`, evaluated/resolved/pending counts, and primary recommendation;
- event counts by evaluation status and primary-horizon status;
- safety flags confirming that it does not submit orders, change strategy config, or restart services.

Apply mode is hash-gated:

```bash
/usr/bin/python3 /root/rt_signal_outcome_event_store.py \
  --apply \
  --confirm-schema-hash <schema_hash> \
  --output /tmp/rt_signal_outcome_event_store_report.json \
  --text
```

Apply mode only creates/updates the audit table and upserts outcome rows by `signal_id`. Outcome rows are expected to evolve from `pending` to `resolved` as future daily K-lines arrive. It does not alter `rt_signal_outcome_report.json`, does not relax strategy evidence thresholds, and does not submit simulation orders.

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/rt_signal_outcome_event_store_report.json` into top-level `signal_outcome_event_store` when the file exists.
- Missing or dry-run outcome event-store status does not block packet generation. It tells Hermes/operator that long-term outcome evidence still depends on report file retention.
- Execute gates still read `/tmp/rt_signal_outcome_report.json`; the event store is for durable audit, cohort analysis, and later strategy/Hermes quality review.

Recommended staged cron:

```bash
*/30 * * * * /usr/bin/python3 /root/rt_signal_outcome_event_store.py --output /tmp/rt_signal_outcome_event_store_report.json --text >> /tmp/rt_signal_outcome_event_store.log 2>&1
```

Only after reviewing the emitted `schema_hash`, replace the dry-run line with the hash-confirmed `--apply` line. This is outcome-evidence persistence only; it is not an execution path.

### Hermes visibility contract patch on 2026-06-12

`rt_signal_outcome_report.py` now exposes the same key evidence already present under `counts` and `overall` as top-level fields:

- `raw_alert_count`
- `directional_alert_count`
- `evaluated_signal_count`
- `duplicate_signal_count`
- `resolved_signal_count`
- `pending_signal_count`
- `pending_or_invalid_count`
- `pending_reasons`
- `primary_horizon`
- `primary_horizon_metric`
- `primary_recommendation`

`alert_quality_report.py` now exposes `schema=alert_quality_report_v1`, `status`, and top-level alert/session summary counts such as `total_alert_count`, `directional_alert_count`, `packet_review_item_count`, `packet_eligible_count`, and `symbol_conflict_count`.

`hermes_review_packet.py` now exposes the latest alert quality report as top-level `alert_quality_summary`.

`hermes_review_packet.py` also promotes `rt_order_intake.py` dry-run strategy evidence failures into review-item blocking reasons:

- `strategy_evidence_would_block_execute`
- `strategy_evidence:<gate_reason>`

It also promotes symbol-conflict dry-run failures into review-item blocking reasons:

- `symbol_conflict_would_block_execute`
- `symbol_conflict:<gate_reason>`

This keeps `review_items[].eligible_for_approval` aligned with the execute gate. Hermes should treat these items as `reject_or_hold` until enough forward outcome evidence exists; it should not write an approval merely because the intake mode was dry-run.

### Current-sample scope patch on 2026-06-12

The alert quality and signal outcome reports now default to `sample_scope.mode=latest_strategy_config_and_watchlist`. The selected scope is inferred from the latest scanned directional alert that carries both `strategy_config_id` and `watchlist_id`.

Operational effect:

- current reports stop blaming the active v5 config for old queue rows emitted before watchlist/strategy provenance existed;
- outcome evidence gates become more conservative because only the current strategy/watchlist version can accumulate samples for execution approval;
- `sample_scope.excluded_alert_count` and `sample_scope.excluded_directional_alert_count` preserve visibility into excluded legacy or prior-version rows;
- `--sample-scope all` remains available for research, debugging, and historical comparisons.

This changes evidence attribution only. No order submission code, Hermes judgment schema, alert bridge mode, cron execution path, watchlist promotion path, or strategy threshold was enabled or relaxed.

This is an additive read-only contract change. Existing nested fields remain unchanged, no strategy thresholds changed, no signal generation changed, no Hermes judgment schema changed, and no simulation execution path was enabled. Hermes can integrate it without migration by preferring the new top-level fields when present and falling back to the existing nested `counts`/`overall`/`directional_quality` fields for older reports.

### Strategy evidence execute gate

`rt_order_intake.py` now enforces the outcome report in execute mode. This means a future `alert-sim` rollout has four independent approvals before an order can be submitted:

- system health is not `FAIL`;
- strategy evidence passes the configured outcome thresholds;
- market context allows the trade, or Hermes explicitly documents a high-confidence risk-off exception for a new BUY;
- Hermes has written a fresh matching `approve` or `reduce` judgment for the same `signal_id`.

Current server state should still reject execute mode because `rt_signal_outcome_report.py` has `resolved_signal_count=0` and all evaluated signals are pending future daily K-lines. This is intentional. Operators may still run dry-runs to inspect proposed orders and rejection reasons, but should not disable `RT_ORDER_REQUIRE_STRATEGY_EVIDENCE` unless doing an explicitly documented emergency/manual experiment.

Server probe after deployment:

- a temporary matching Hermes `approve` judgment was written for one confirmed v5 `BUY` alert;
- `rt_order_intake.py --mode execute` returned `status=rejected`;
- rejection reason was `strategy_evidence_gate_failed`;
- strategy reasons were `overall_outcome_sample_below_30` and `trigger_outcome_sample_below_5`;
- no `order_result` was produced.

This confirms that Hermes approval alone cannot bypass the unresolved strategy evidence state.

### Symbol conflict execute gate

`rt_order_intake.py` rejects execute mode when the current-scope alert queue contains an opposite-direction alert for the same symbol. The scope uses `strategy_config_id + watchlist_id` when present, and falls back to same signal date for legacy alerts without provenance.

This gate is intentionally conservative:

- it catches noisy v5 sessions where a symbol oscillates between BUY and SELL;
- it prevents a future `alert-sim` rollout from buying and then quickly selling the same symbol because the rolling queue held contradictory evidence;
- it makes Hermes explain conflicts at review time instead of relying on strategy review summaries alone.

Operationally this is additive. Existing dry-run/report jobs keep running, no order path is enabled, and operators can still research historical mixed queues through the report layer.

### v5 unconfirmed-directional emission hardening

`rt_signal_engine_v5.py` now downgrades unconfirmed directional candidates to `WATCH` by default. A candidate is unconfirmed when it fails the configured full-score threshold, for example a BUY candidate with `full_score < min_full_score` or a SELL candidate with `full_score > max_full_score`.

The emitted row keeps diagnostic context:

- `candidate_signal_type`;
- `suppressed_directional_reason=unconfirmed_directional`;
- `execution_candidate=false`;
- `candidate_entry_price`, `candidate_stop_loss`, `candidate_take_profit`, and `candidate_rr_ratio`.

This reduces source-level BUY/SELL conflicts and keeps the current-scope outcome report focused on execution candidates. It does not enable any execution path and does not hide the event from Hermes diagnostics. To preserve old-style unconfirmed directional rows for a controlled research run, set `RT_SIGNAL_EMIT_UNCONFIRMED_DIRECTIONAL_AS_WATCH=0` before starting v5.

### Strategy review policy

`scripts/strategy_review_report.py` converts raw v5 outcome and alert-quality diagnostics into a read-only trigger policy for Hermes.

It reads:

- `/tmp/rt_signal_outcome_report.json`
- `/tmp/rt_alert_quality_report.json`

Default output:

```bash
/usr/bin/python3 /root/strategy_review_report.py --output /tmp/strategy_review_report.json --text
```

The report emits:

- `overall_policy`, normally `keep_shadow_or_dry_run` until enough outcome evidence exists;
- per-trigger policies:
  - `shadow_only` when the forward outcome sample is too small;
  - `tighten_thresholds` when alert validation, packet eligibility, or queue marks are weak;
  - `disable_execution_review` when outcome return/win-rate/stop-hit structure is poor;
  - `candidate_allow_after_other_gates` only when the trigger passes this read-only review;
- recommendations such as `disable_or_rework_trigger:BUY:...` or `tighten_trigger_thresholds:SELL:...`.

Important: this report is not an execution approval. It sets:

```json
{
  "source": {
    "read_only": true,
    "auto_applies_strategy_changes": false
  }
}
```

`hermes_review_packet.py` reads the report into top-level `strategy_review` when available. Hermes should treat weak trigger policy as a reason to reject, hold, or reduce confidence, but execute mode still requires the hard gates in `rt_order_intake.py`: health, outcome evidence, market context, Hermes trade judgment, portfolio risk, and API credentials.

Recommended read-only cron:

```bash
*/30 * * * * /usr/bin/python3 /root/strategy_review_report.py --output /tmp/strategy_review_report.json --text >> /tmp/strategy_review_report.log 2>&1
```

### Strategy learning report

`scripts/strategy_learning_report.py` is the read-only cross-stage review that ties the v5 workflow together:

- v5 alerts from `/tmp/rt_signal_alerts.jsonl`;
- Hermes trade judgments from `/tmp/hermes_trade_judgments.jsonl`;
- `rt_order_intake.py` dry-run/execute decisions from `/tmp/rt_order_intake_state.json`;
- forward outcomes from `/tmp/rt_signal_outcome_report.json`.

Default output:

```bash
/usr/bin/python3 /root/strategy_learning_report.py --output /tmp/strategy_learning_report.json --text
```

It reports:

- how many signals can be joined across alert, judgment, intake, and outcome stages;
- `sample_scope`, defaulting to the latest `strategy_config_id + watchlist_id`, plus `all_join_counts` for the unfiltered historical join;
- overall resolved sample, average signed return, and win rate for the configured horizon;
- cohort comparison for Hermes `approve/reduce` versus `reject/hold` versus missing judgment;
- per-trigger forward returns;
- intake rejection/acceptance cohorts, including dominant rejection reasons;
- actionability cohorts under `by_actionability`, separating real trade candidates from observation-only rows and execution blockers;
- `intake_coverage`, showing how many scoped signals have a dry-run/execute intake decision versus `missing_intake_decision`, split into `directional`, `watch`, and `other` cohorts;
- `sizing_blocker_diagnostics` for `quantity_zero_after_risk_and_lot_rounding`, including lot size, one-lot notional/risk, raw quantity before lot rounding, and binding limits such as `allocation_budget_below_one_lot` or `risk_budget_below_one_lot`;
- `sizing_blocker_remediation`, linking sizing blockers to the current watchlist change proposal when the blocked symbols are already proposed for removal;
- recommendations such as collecting more outcomes, reviewing weak triggers, or reviewing Hermes prompt/gate behavior when approvals do not outperform rejected/held signals.

`sell_without_position` and `alert_too_old` are intentionally retained in `by_intake_reason` for auditability, but they are not treated as dominant blocker recommendations. Those rows mean the signal is useful only as a market observation or forward-outcome learning sample, not as evidence that an executable trade flow is broken. If a large number of signals fall into `blocked_sizing_or_lot`, `blocked_portfolio_constraint`, `blocked_strategy_evidence`, or `blocked_symbol_conflict`, that is a real review item for sizing rules, universe selection, trigger thresholds, or current-queue conflict policy.

By default, the report scopes its main cohorts to the current v5 version: the latest alert with both `strategy_config_id` and `watchlist_id`, plus joined rows sharing that pair. This keeps old strategy/watchlist revisions from driving current Hermes learning recommendations. Use `--sample-scope all` only for offline historical research across mixed queue versions; do not use all-scope output as current execute evidence.

If `intake_coverage.directional.coverage_pct` is low, Hermes should treat the trade-learning report as incomplete rather than as proof that the strategy itself is failing. Missing directional intake decisions usually mean confirmed BUY/SELL alerts were never run through dry-run/execute intake, not that they passed or failed the trading gate. Low overall coverage caused mainly by `WATCH` rows is less severe for execution learning because WATCH rows are observation-only; use `watch` coverage to assess observation durability separately.

When `sizing_blocker_diagnostics.by_binding_limit` is dominated by `allocation_budget_below_one_lot`, Hermes should not approve around the gate. Treat it as evidence that the current 100k HKD simulation sizing, watchlist price level, or HK lot-size assumptions make the signal non-executable. When it is dominated by `risk_budget_below_one_lot`, review stop distance, volatility filters, and risk-per-trade settings before considering any config proposal. The report remains read-only and does not change sizing rules by itself.

If `sizing_blocker_remediation.covered_by_watchlist_removal_count` is high, Hermes may recommend reviewing the referenced `watchlist_proposal_hash` as the safer remediation path. This does not apply the proposal; it only connects current sizing failures to an existing manual watchlist review artifact.

When sizing blockers are fully covered by a watchlist removal proposal, the learning report suppresses generic dominant sizing/blocker recommendations. Hermes should treat the watchlist proposal review as the primary remediation path, not as evidence to loosen risk limits or approve around lot-size gates.

Important: this report is not an execution approval and it does not modify strategy config. It sets:

```json
{
  "source": {
    "read_only": true,
    "auto_applies_strategy_changes": false,
    "submits_orders": false
  }
}
```

`hermes_review_packet.py` reads the report into top-level `strategy_learning` when available. It also emits an additive top-level `strategy_learning_brief` so Hermes does not need to search the full nested report for the current reliability state. The full `strategy_learning` object remains authoritative and unchanged for existing readers.

`strategy_learning_brief` is read-only and contains:

- current `sample_scope.strategy_config_id` and `sample_scope.watchlist_id`;
- outcome evidence, including whether the minimum resolved sample is met;
- overall, directional, and WATCH intake coverage;
- Hermes judgment effect for approved/reduced versus rejected/held cohorts;
- sizing blocker remediation status, covered/uncovered symbols, `watchlist_proposal_hash`, `current_watchlist_id`, and `proposed_watchlist_id`;
- the leading learning recommendations.

Hermes should use the brief as an attention guide: low directional intake coverage means learning evidence is incomplete; low overall coverage caused by WATCH rows is an observation-quality issue; sizing blocker remediation is manual watchlist proposal context only. The brief must not apply watchlists, restart services, submit orders, or override `rt_order_intake.py` execute gates.

`hermes_review_packet.py` also emits top-level `simulation_trade_review_brief`, summarizing realized simulation-trade review and portfolio performance from `portfolio_report.py`: total value, return versus initial capital, unrealized PnL percent of cost, lookback window, trade count, closed trade count, closed win rate, estimated closed PnL, largest win/loss, and review notes. Hermes should treat weak or missing realized simulation-trade evidence as a reason to hold/reject new exposure even when paper signal outcomes look positive.

Hermes should use strategy learning to critique its own judgment quality and to support future strategy proposals, but execute mode still depends on `rt_order_intake.py` gates and the current outcome evidence.

Recommended read-only cron:

```bash
*/30 * * * * /usr/bin/python3 /root/strategy_learning_report.py --output /tmp/strategy_learning_report.json --text >> /tmp/strategy_learning_report.log 2>&1
```

### Simulation performance attribution

`scripts/simulation_performance_report.py` is the read-only bridge between the 100k HKD simulation portfolio and Hermes strategy discipline. It reads `/tmp/portfolio_report.json` when available, or builds the same portfolio context directly, then converts realized simulation behavior into a compact attribution report:

- simulation portfolio total value and return versus initial capital;
- recent closed-trade count, win rate, and estimated FIFO P&L;
- blocking review notes such as `recent_closed_trades_negative` and `loss_rate_above_60pct`;
- portfolio risk level and risk flags;
- worst closed symbols by estimated P&L;
- open position risk rows, including high-priority holdings and current recommendations;
- recommendations such as keeping `alert-sim` disabled, prioritizing high-risk position judgments, and inspecting worst closed symbols.

Default command:

```bash
/usr/bin/python3 /root/simulation_performance_report.py --output /tmp/simulation_performance_report.json --text
```

The output schema is `simulation_performance_report_v1`. `status=FAIL` means recent simulation behavior does not support new exposure: for example total simulation return is not positive, closed P&L is not positive, closed-trade win rate is too low, blocking simulation review notes are present, or simulation portfolio risk is critical. `status=WARN` means the realized trade sample is acceptable but portfolio risk still requires manual/Hermes review. `status=OK` means the simulation performance attribution layer has no blocking or warning reason. This report is read-only: it does not submit orders, change strategy thresholds, repair positions, or apply watchlist/config proposals.

`hermes_review_packet.py` reads this report into top-level `simulation_performance` when `/tmp/simulation_performance_report.json` exists. Hermes should use it as a hard critique layer before supporting new BUY exposure: positive paper signal outcomes are not enough when the simulation portfolio is losing money or recent closed trades are weak.

`execution_readiness_report.py` also reads `/tmp/simulation_performance_report.json`. A `simulation_performance.status=FAIL` hard-blocks readiness, while `WARN` prevents `READY` until the operator/Hermes has reviewed the risk context.

Recommended read-only cron:

```bash
*/30 * * * * /usr/bin/python3 /root/simulation_performance_report.py --output /tmp/simulation_performance_report.json --text >> /tmp/simulation_performance_report.log 2>&1
```

### Execution readiness dashboard

`scripts/execution_readiness_report.py` is a read-only dashboard that combines the main safety evidence Hermes and the operator need before considering any future execute-mode change:

- system health;
- data health;
- market context freshness and regime/risk state;
- resolved forward outcome sample;
- directional intake coverage;
- simulation portfolio risk and reconciliation state;
- watchlist proposal remediation state;
- Hermes judgment audit status;
- Hermes advisory position judgment audit status;
- simulation performance attribution status;
- alert quality status.

Default command:

```bash
/usr/bin/python3 /root/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text
```

The output schema is `execution_readiness_report_v1`. `status=BLOCKED` means at least one hard gate is missing or failing, including stale or missing report timestamps, missing system health, missing data health, missing/invalid market context, failed Hermes trade judgment audit, failed Hermes advisory position judgment audit, failed simulation performance attribution, insufficient resolved outcomes, missing/non-positive average signed forward return, missing/weak win rate, weak target-vs-stop hit evidence, weak favorable/adverse excursion evidence, low directional intake coverage, insufficient Hermes judgment-effect evidence, missing simulation portfolio risk context, or failed simulation reconciliation. `status=WARN` means no hard gate failed, but manual review is still required, for example when market context is `risk_off`/high-risk, when advisory position judgment audit is missing/unknown, when simulation performance attribution is missing/unknown/warning, or when a watchlist proposal covers sizing blockers but has not been reviewed/applied/restarted. `status=READY` is necessary context only; it does not enable bridge execution, does not submit orders, and does not override `rt_order_intake.py` execute gates or matching Hermes trade judgments.

By default, all critical input reports, including `/tmp/market_context_report.json`, `/tmp/watchlist_diff_report.json`, `/tmp/simulation_performance_report.json`, and `/tmp/hermes_position_judgment_audit_report.json`, must have a recognized timestamp and be no older than 90 minutes. Market context with `risk_off` regime or `high` risk level prevents `READY` and requires stricter manual/Hermes review; `rt_order_intake.py` remains the authoritative execute gate for market-regime exceptions. Fresh watchlist diff context is required because sizing-blocker remediation may depend on a specific hash-stamped manual watchlist proposal. The forward-evidence gate requires at least five resolved signals, positive average signed forward return, win rate above 50%, stop hit rate not exceeding target hit rate, stop hit rate not above 50%, and favorable/adverse excursion ratio above 1. The Hermes judgment-effect gate also requires at least five resolved approved/reduced judgments, at least five resolved rejected/held judgments for comparison, positive approved/reduced average return, approved/reduced win rate above 50%, and approved/reduced average return above rejected/held average return. The advisory position-judgment audit gate requires `status=OK` or `PASS`; `status=FAIL` blocks readiness because user/simulation holding reviews would no longer be safely advisory and auditable. The simulation performance attribution gate requires `status=OK` or `PASS`; `status=FAIL` blocks readiness because the realized simulation portfolio evidence contradicts new exposure. The simulation portfolio-performance gate requires positive total return versus initial capital and unrealized PnL not below -5% of cost. The simulation trade-review gate requires at least three closed simulation trades, positive estimated closed PnL, closed win rate above 50%, and no blocking review notes such as `recent_closed_trades_negative` or `loss_rate_above_60pct`. The thresholds are explicit CLI/env settings:

```bash
/usr/bin/python3 /root/execution_readiness_report.py \
  --min-resolved-outcomes 5 \
  --min-win-rate-pct 50 \
  --max-stop-hit-rate-pct 50 \
  --min-favorable-to-adverse-ratio 1 \
  --min-hermes-effect-sample 5 \
  --min-sim-closed-trades 3 \
  --min-sim-return-pct 0 \
  --min-sim-unrealized-pnl-pct -5 \
  --max-report-age-minutes 90 \
  --min-directional-intake-coverage-pct 80 \
  --output /tmp/execution_readiness_report.json \
  --text
```

`hermes_review_packet.py` reads this report into top-level `execution_readiness` when available. Hermes should use it as an operator dashboard and must continue to treat `review_items`, `strategy_evidence`, `data_health`, `portfolio_risk`, and intake gates as authoritative for each specific signal.

Recommended read-only cron:

```bash
*/30 * * * * /usr/bin/python3 /root/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text >> /tmp/execution_readiness_report.log 2>&1
```

### Strategy config proposal

`scripts/strategy_config_proposal.py` converts `strategy_review_report.py` output into a candidate `rt_signal_strategy_config_v1` file for human review.

Default command:

```bash
/usr/bin/python3 /root/strategy_config_proposal.py --output /tmp/rt_signal_strategy_config_proposal.json --text
```

It proposes:

- `enabled=false` for triggers with `disable_execution_review`;
- tighter per-trigger `min_full_score` or `max_full_score` for `tighten_thresholds`;
- `review_mode=shadow_only_pending_sample` for `shadow_only`.

Important behavior:

- it reads the current `/root/rt_signal_strategy_config.json`;
- it writes only `/tmp/rt_signal_strategy_config_proposal.json`;
- it never overwrites the live strategy config;
- proposal output has `manual_review_required=true` and `auto_applied=false`;
- promotion requires an operator to copy reviewed changes into `/root/rt_signal_strategy_config.json` and restart `rt_signal_engine_v5.service`.

Recommended read-only cron:

```bash
5,35 * * * * /usr/bin/python3 /root/strategy_config_proposal.py --output /tmp/rt_signal_strategy_config_proposal.json --text >> /tmp/strategy_config_proposal.log 2>&1
```

### Strategy config promotion

`scripts/strategy_config_promote.py` is the guarded manual promotion tool for `/tmp/rt_signal_strategy_config_proposal.json`.

Dry-run is the default:

```bash
/usr/bin/python3 /root/strategy_config_promote.py --proposal-file /tmp/rt_signal_strategy_config_proposal.json --target-config-file /root/rt_signal_strategy_config.json --text
```

Apply requires the exact `proposal_hash`:

```bash
/usr/bin/python3 /root/strategy_config_promote.py \
  --proposal-file /tmp/rt_signal_strategy_config_proposal.json \
  --target-config-file /root/rt_signal_strategy_config.json \
  --apply \
  --confirm-proposal-hash <proposal_hash> \
  --text
```

Optional restart is explicit:

```bash
/usr/bin/python3 /root/strategy_config_promote.py \
  --proposal-file /tmp/rt_signal_strategy_config_proposal.json \
  --target-config-file /root/rt_signal_strategy_config.json \
  --apply \
  --confirm-proposal-hash <proposal_hash> \
  --restart-service \
  --text
```

Safety behavior:

- dry-run by default;
- validates proposal schema and `manual_review_required=true`;
- recalculates the proposed config hash before apply;
- refuses apply without `--confirm-proposal-hash`;
- backs up the target under `/tmp/rt_signal_strategy_config_backups/`;
- does not restart `rt_signal_engine_v5.service` unless `--restart-service` is present.

Server rollout on 2026-06-12:

- `strategy_config_proposal.py` was deployed to `/root/strategy_config_proposal.py`;
- previous v5/service/docs/cron files were backed up to `/root/quantmind_backup_20260612_091104_strategy_config`;
- first proposal used current config `8c5fa44224376503` and produced proposal `d368d751817c7d76`;
- it proposed seven `tighten_thresholds` changes from the current `strategy_review`;
- `auto_applied=false` and `manual_review_required=true`;
- no live strategy config was overwritten.

Server rollout on 2026-06-12:

- `strategy_review_report.py` was deployed to `/root/strategy_review_report.py`;
- previous packet/docs/cron files were backed up to `/root/quantmind_backup_20260612_085948_strategy_review`;
- first report returned `overall_policy=keep_shadow_or_dry_run`;
- reasons included `overall_outcome_sample_below_30` and `symbol_conflicts_present_in_alert_queue`;
- observed trigger policies were all `tighten_thresholds` because current forward outcome samples were still unresolved and alert-quality/eligible rates were weak;
- `hermes_review_packet.py` exposed `strategy_review.schema=strategy_review_report_v1`;
- packet `execution_safety.submits_orders=false` remained unchanged.

## Known Remaining Gaps

- v5 still reads history directly through `docker exec psql`; this should eventually move behind a database adapter.
- v5 watchlists now load from `/root/rt_signal_watchlist.json`, and `universe_rank_report.py` produces a ranked candidate file, but live watchlist promotion is still manual and does not yet include full walk-forward universe validation.
- The existing v4 cron path still exists. Keep it until Hermes confirms v5 output is reliable enough to become the only signal path.
- `rt_order_intake.py` still depends on the QuantMind simulation API shape; test on the server before enabling `alert-sim`.
- Hermes LLM judgment is represented as a local JSONL artifact. The next improvement should enrich the judgment prompt with broader market/news/context inputs.
- `portfolio_report.py` estimates closed-trade P&L from available `sim_trades`; improve it if the backend exposes canonical realized P&L rows.
- The server can show `positions` rows lagging behind `sim_trades`; `portfolio_risk` now detects this, but it does not repair the backend position ledger.
- `sim_position_reconcile.py` can repair portfolio 8 positions from `sim_trades`, but it should stay hash-gated and manually reviewed until the backend API's own position update path is understood.
- `rt_signal_outcome_report.py` uses daily K-lines, not minute bars, so stop/target sequencing is conservative and incomplete.
- `market_context_report.py` uses stock-pool breadth because no reliable index/ETF K-lines were available in the current database.
- `hermes_judgment_audit_report.py` relies on retained packet archives under `/tmp/hermes_review_packet_archive`; keep an eye on retention if Hermes needs long-horizon judgment QA.
