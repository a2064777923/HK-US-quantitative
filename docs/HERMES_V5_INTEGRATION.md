# Hermes v5 Integration

**Updated:** 2026-06-14

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
- The append-only queue is written before the legacy latest-alert file. If queue append fails, the latest file is left unchanged rather than showing an alert that never entered the event history.
- Realtime quotes are handled as one temporary intraday bar. They no longer get appended to historical daily arrays every scan, which avoids RSI/MA/MACD drift during the day.
- Every v5 alert now declares its timeframe basis with `timeframe_scope=completed_daily_ohlcv_with_realtime_quote`, `primary_timeframe=1d`, `realtime_input=single_quote_temporary_bar`, `intraday_minute_bars_used=false`, and `intraday_evidence_policy=external_read_only_context_only`. Hermes should treat this as the technical-signal contract: v5 does not consume stored minute bars directly, and 5m/15m/30m/60m evidence must come from the separate read-only intraday context/quality reports.
- MA5 price-cross triggers compare the realtime quote against the latest completed daily MA5, not an MA5 recalculated with the same temporary quote. This prevents the quote from moving its own threshold before the cross is tested. MA10/MA20 crossover triggers still compare the temporary current MA state against the latest completed historical MA state, and are symmetric: `MA金叉` emits a BUY candidate and `MA死叉` emits a SELL candidate under the same confirmation, risk/reward, cooldown, and Hermes review gates.
- Realtime `full_score` trend reasons such as `多頭排列`, `短均線偏強`, `空頭排列`, and `短均線偏弱` also compare the quote against completed daily MA5/MA10/MA20. The temporary quote may affect RSI/MACD/ATR display calculations, but it no longer moves the moving-average trend thresholds used to confirm or challenge the same quote.
- Bollinger trigger checks and `full_score` Bollinger reasons compare the realtime quote against the latest completed 20-day daily Bollinger bands instead of bands recalculated with the same temporary quote. This prevents an extreme intraday quote from widening the band before the breach is tested. It does not change alert fields, order intake, simulation state, or execution mode.
- v5 only scans symbols with at least 30 completed valid and aligned daily OHLCV bars, matching the multi-factor `full_score` lookback requirement. Symbols with shorter, failed, or misaligned history loads are skipped with a startup log entry instead of emitting partial-history WATCH/BUY/SELL alerts. This does not change watchlists, strategy thresholds, order intake, simulation state, or intraday context reports.
- BUY/SELL candidates carry a full-score confirmation flag. The default confirmation floor is now deliberately multi-factor: BUY requires `full_score >= 0.45` and SELL requires `full_score <= -0.45`, so a single weak trigger contribution such as standalone RSI/布林 evidence is not enough to enter the confirmed directional queue. Unconfirmed directional candidates are downgraded to `WATCH` by default, with `candidate_signal_type` and candidate risk fields retained for diagnostics.
- Confirmed BUY/SELL candidates must also satisfy the v5 risk model's minimum risk/reward ratio. The default `risk_model.min_rr_ratio=1.2` matches the order-intake minimum, and v5 refuses strategy config values below 1.2 while allowing stricter values above it. v5 calculates `rr_ratio` from the rounded entry/stop/take-profit prices that downstream intake will actually see. Lower-RR directional triggers are emitted as diagnostic `WATCH` rows with candidate risk fields, not trade candidates.
- Directional candidates require a finite positive ATR before v5 can calculate executable stop/take-profit geometry. Missing, non-finite, or non-positive ATR now downgrades the candidate to `WATCH` with `risk_geometry_reason=missing_or_invalid_atr`; v5 does not synthesize a default ATR or fabricated risk prices.
- `execution_candidate=true` is now explicitly reserved for alerts that remain BUY/SELL after policy downgrades, are full-score confirmed, and have valid risk geometry. Hermes and order-intake readers should not infer executability from `candidate_signal_type` alone; diagnostic WATCH rows may still carry candidate risk fields for review, but they are not executable candidates.
- v5 rounds risk prices with price-aware precision: 2 decimals for prices at or above 1, 3 decimals below 1, and 4 decimals below 0.1. This keeps HK/US low-price symbols from having stop/take-profit geometry distorted by a fixed 2-decimal round.
- Realtime quotes are normalized before they update the temporary bar or trigger checks. Missing, non-finite, or non-positive prices are rejected without emitting alerts; optional high/low/volume/change fields are clamped to a safe same-price/zero-volume tick when the price itself is valid.
- v5 also normalizes `quote_time`/`market` at the quote boundary before alert construction. Datetime-like quote timestamps are converted to stable text, and market codes are upper-cased, so strict JSON alert persistence cannot be broken by a non-string vendor time object. This does not change scoring, trigger thresholds, order intake, simulation state, or Hermes judgment rules.
- The v5 main loop skips realtime quotes whose vendor timestamp is missing/unparseable, more than 15 minutes stale, or more than 120 seconds in the future relative to the quote market's local clock. US freshness compares against DST-aware New York time derived from the HKT loop clock, while HK freshness uses HKT directly. This is fail-closed before temporary-bar updates and trigger checks; it does not change alert fields, Hermes/order-intake schemas, simulation state, or execution mode.
- v5 now requires normalized quote `market` to be `HK` or `US`, and it skips alerts when the symbol format does not match the quote market. This fail-closed check prevents AAPL-style US symbols from being emitted as HK alerts, numeric HK symbols from being emitted as US alerts, or missing-market quotes from entering Hermes, event-store, order-intake, or outcome learning with ambiguous market semantics.
- When vendor `change_pct` is missing or non-finite but `prev_close` is valid, v5 derives `change_pct` from `price / prev_close - 1`. This keeps large-move WATCH alerts and Hermes-facing alert context from being flattened to `0%` just because one optional quote field was absent; it does not change trigger thresholds, watchlists, order intake, or simulation state.
- HK/US scan gating now uses HKT-aware session helpers. US regular-session checks convert HKT to America/New_York time before testing 09:30-16:00 ET, so both daylight-time sessions (about 21:30-04:00 HKT) and standard-time sessions (about 22:30-05:00 HKT) are handled without changing cron or polling settings. This prevents winter US sessions from opening one hour early and closing one hour early in v5.
- Signal cooldown keys are symbol/type/trigger based rather than HKT-calendar-date based. This keeps one US overnight session from re-emitting the same technical trigger just because Hong Kong local time crossed midnight.
- Cooldown keys are written after v5 applies policy downgrades such as unconfirmed-directional-to-WATCH, shadow-only, or invalid risk geometry. A downgraded diagnostic WATCH no longer suppresses a later confirmed BUY/SELL in the same cooldown window, while repeated WATCH diagnostics and repeated confirmed directionals still cool down independently.
- `signal_id` date prefixes prefer the alert's parsed `quote_time` only when the quote timestamp contains a full date, falling back to `generated_at` when the quote timestamp is missing, unparseable, or time-only such as `HH:MM:SS`. Time-only vendor values are still allowed for elapsed-session volume normalization, but they are not allowed to stamp event-store/outcome identity with the server's current calendar date. This keeps overnight US alerts and outcome/event-store tracing aligned to dated market data without changing cooldown keys, order intake, Hermes judgments, or execution mode.
- Timezone-aware quote timestamps are converted to the quote market's local timezone before v5 derives the `signal_id` date prefix. For example, a UTC quote that is already the next HKT trading date is stamped with the HK local date instead of the raw UTC date. This only affects alert identity/date attribution; scoring, trigger thresholds, order intake, simulation state, and execution mode are unchanged.
- `signal_id` buckets use the actual trigger cooldown seconds, including per-trigger overrides, so a valid alert emitted after a shorter configured cooldown does not collide with the previous alert in event-store, order-intake, or outcome deduplication.
- Alert output is strict JSON. Non-finite internal values such as NaN/inf `full_score` or ATR are normalized before alert construction, and `send_alert()` refuses to write non-standard JSON tokens to the latest alert file or append-only queue.
- Cooldown state in `/tmp/rt_signal_state.json` is normalized on read/write and saved through an atomic temp-file replace. Corrupt or wrong-shaped state falls back to an empty cooldown ledger, while malformed cooldown entries are dropped instead of poisoning the whole engine. This preserves alert idempotency without changing strategy thresholds, Hermes judgment, order intake, simulation state, or crontab wiring.
- Volume anomaly WATCH alerts and the v5 `full_score` volume factor compare cumulative intraday volume with expected cumulative daily volume based on elapsed HK/US session minutes. Quote timestamps with compact vendor formats such as `YYYYMMDDHHMMSS` and `YYYYMMDDHHMM` are parsed directly, and timezone-aware ISO timestamps such as `...Z` are converted to the quote market's local session time before elapsed minutes are calculated. If the quote timestamp is missing or unparseable, v5 now skips the volume anomaly/factor instead of falling back to server clock time. This avoids stale one-minute/daily-volume mismatches and keeps confirmation scoring aligned with the alert's volume-anomaly definition.
- v5 only uses realtime quote volume for volume anomaly and `full_score` volume confirmation when the quote volume can be normalized to shares. US quotes and unlabelled internal test quotes are treated as shares. Tencent HK quote volume is labelled `volume_unit=board_lot`; unless a trusted `lot_size`/`board_lot_size` is present, v5 skips that volume factor instead of comparing lots with daily share volume. This is a fail-closed HK reliability rule, not an execution-mode change.
- The v5 `full_score` volume factor is directional. Heavy volume on an uptick adds confirmation, while heavy volume on a downtick subtracts confirmation with `full_reasons` such as `放量下跌...`; flat heavy volume is not treated as automatic BUY support. This prevents selloff volume from helping oversold BUY candidates pass confirmation and is additive for Hermes readers because the alert schema is unchanged.
- The `5日動量...` `full_reasons` item contributes signed confirmation to `full_score` using a true 5-bar lookback (`current / close_5_bars_ago - 1`), including when the current point is a realtime temporary bar. Positive 5-day momentum adds modest confirmation and negative 5-day momentum subtracts it. This keeps the human/Hermes explanation aligned with the actual threshold calculation without changing alert fields, order intake, simulation state, or execution mode.
- v5 no longer truncates `full_reasons` before writing the alert payload. Hermes receives the complete list of score-affecting explanations under the existing `full_reasons` field, so newly added factors such as directional volume and 5-day momentum cannot be hidden behind earlier reasons. This is a no-loss integration change: the field name and type stay the same, order intake still treats it as a list, and execution eligibility remains governed by `signal_type`, `confirmed`, `execution_candidate`, and risk geometry.
- Every v5 `full_score` contribution now has a matching `full_reasons` entry, including moderate trend, RSI, MACD, and volume branches. This closes an explanation gap where a BUY/SELL could pass confirmation from score-affecting evidence that was not visible to Hermes. Scores, thresholds, alert fields, order intake, simulation state, and execution mode are unchanged.

### Hermes bridge

`scripts/rt_alert_bridge.py` now supports explicit execution modes:

- `RT_ALERT_EXECUTION_MODE=notify` - default, prints Hermes notification text only.
- `RT_ALERT_EXECUTION_MODE=alert-dry-run` - runs `rt_order_intake.py` per alert in dry-run mode and reports the proposed order/rejection reason.
- `RT_ALERT_EXECUTION_MODE=alert-sim` - runs `rt_order_intake.py` per alert in execute mode.
- `RT_ALERT_EXECUTION_MODE=legacy-sim` - preserves old behavior by running `quantmind_sim_trader.py` after actionable alerts.

By default, the bridge also reads the latest `/tmp/hermes_signal_review_packet.json` and appends the matching signal's Hermes context digest to the notification text: market regime, intraday confirmation and 5m/15m/30m/60m rolling-window coverage, news/macro summaries, event risk and event support, sentiment, fundamentals availability, source reliability, simulation-performance metrics, execution-readiness blocker details, and required judgment attention. This is read-only display context. It does not write judgments, update DB rows, change alert queues, or submit simulation orders. Set `RT_ALERT_INCLUDE_PACKET_CONTEXT=0` to return to the old compact alert text.

When the bridge runs from cron on the same server that produces `/tmp/rt_signal_alerts.jsonl` and `/tmp/hermes_signal_review_packet.json`, set `RT_ALERT_REMOTE=local`. In local mode the bridge reads alert, packet, and sent-state files directly instead of self-SSHing to `root@38.76.164.106`. This preserves the same notify-only behavior while removing a fragile dependency on server SSH key setup. The recommended read-only notification cron is:

```cron
* * * * * RT_ALERT_REMOTE=local RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py >> /tmp/rt_alert_bridge.log 2>&1
```

To deliver the same notify-only text to Feishu, explicitly add `RT_ALERT_SEND_FEISHU=1` after Feishu app credentials have been verified in the runtime environment. `feishu_notify.py` intentionally has no code-level credential fallback; it reads `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, and `FEISHU_CHAT_ID` from the environment or from `FEISHU_ENV_FILE`, defaulting to `/root/.quantmind_env`. This remains advisory notification only: it does not change `RT_ALERT_EXECUTION_MODE`, call order intake in notify mode, submit simulation orders, write judgments, or mutate portfolios. When Feishu delivery is enabled, `rt_alert_bridge.py` updates `/tmp/rt_signal_sent.json` and `/tmp/rt_position_review_sent.json` only after `feishu_notify.send_feishu_message()` returns success. If Feishu credentials are missing, token/message delivery fails, or the sender raises, the bridge logs the failure, returns non-zero, and leaves sent-state unchanged so the alert or high-urgency position review can retry on the next cron run.

Recommended secret file shape:

```bash
export FEISHU_APP_ID="..."
export FEISHU_APP_SECRET="..."
export FEISHU_CHAT_ID="..."
```

```cron
# Optional Feishu delivery; keep disabled until credentials and chat routing are verified.
# * * * * * /bin/bash -lc "cd /root && [ -f /root/.quantmind_env ] && . /root/.quantmind_env; RT_ALERT_REMOTE=local RT_ALERT_SEND_FEISHU=1 RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py >> /tmp/rt_alert_bridge.log 2>&1"
```

The bridge treats packet freshness as display-critical context. `RT_ALERT_MAX_PACKET_CONTEXT_AGE_MINUTES=10` by default. If the packet is stale, the alert is still emitted, but the notification includes a global `Hermes審核狀態：STALE` warning and a per-alert `Hermes上下文：STALE` line before any digest summary. If the packet is missing or has an invalid `generated_at`, the notification says `Hermes審核狀態：MISSING` or `Hermes審核狀態：INVALID` and does not summarize review items from that packet. Packet files generated by the server often use local naive timestamps such as `2026-06-13T20:57:31`; the bridge now interprets those naive timestamps in the server's local timezone before converting to UTC, instead of assuming UTC and falsely marking HKT packets as future-dated. If the bridge runs on a host whose local timezone differs from the report producer, set `RT_ALERT_NAIVE_TIMESTAMP_UTC_OFFSET=+08:00` or another explicit offset. If a fresh or stale packet exists but the alert's `signal_id` is not in it, the notification says `Hermes審核：NO_MATCH` and marks the alert as a technical signal that has not completed comprehensive review. These warnings are display-only: they do not block alert emission, submit orders, change simulation state, or mutate any DB rows. They prevent stale, missing, invalid, or unmatched packet context from looking like Hermes-approved market/news/intraday evidence while preserving notify-only behavior.

Important: `legacy-sim` does not place orders for each v5 alert directly. It runs the existing simulation trader, which still reads `engine_signal_scores` from the database. Use `notify` first unless Hermes intentionally wants that legacy behavior.

### Alert-specific order intake

`scripts/rt_order_intake.py` is the new safe interface between v5 alerts and the 100k HKD simulation portfolio.

It accepts one v5 alert or a JSONL queue and applies:

- `signal_id` idempotency through `/tmp/rt_order_intake_state.json`;
- separate dry-run and execute ledgers, so shadow runs do not consume the signal for later execution;
- `confirmed`, `full_score`, risk/reward, alert age, and price-geometry validation;
- execute-only strategy evidence gate using `/tmp/rt_signal_outcome_report.json`;
- execute-only aggregate readiness gate using `/tmp/execution_readiness_report.json`;
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

Execute mode also requires aggregate readiness by default. The gate reads `/tmp/execution_readiness_report.json` and rejects execution unless `status=READY`, `ready_for_execute=true`, and the report is fresh. Defaults:

- `RT_ORDER_REQUIRE_EXECUTION_READINESS=1`
- `RT_ORDER_MAX_READINESS_REPORT_AGE_HOURS=2`

Dry-run mode does not block on this gate, but includes `execution_readiness.would_block_execute=true` and the blocking/warning gate summaries so Hermes can see why execute mode would remain closed.

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
  "risk_notes": ["keep default 1% equity risk cap"],
  "context_review": {
    "technical_signal_reviewed": true,
    "portfolio_risk_reviewed": true,
    "strategy_evidence_reviewed": true,
    "data_health_reviewed": true,
    "execution_readiness_reviewed": true,
    "market_context_reviewed": true,
    "intraday_context_reviewed": true,
    "external_market_context_reviewed": true,
    "event_catalysts_reviewed": true,
    "event_catalyst_signals_reviewed": true,
    "market_sentiment_reviewed": true,
    "fundamentals_context_reviewed": true,
    "source_reliability_reviewed": true,
    "simulation_performance_reviewed": true,
    "cron_wiring_reviewed": true,
    "notes": ["external context was not supportive enough to increase size"]
  },
  "external_market_context_risk_acknowledged": true,
  "external_market_context_ids": ["macro-risk-1"],
  "external_market_context_notes": ["negative macro/news context was reviewed; confidence is capped and size is not increased"],
  "external_market_context_support_acknowledged": true,
  "external_market_context_support_ids": ["macro-support-1"],
  "external_market_context_support_notes": ["positive high-impact external context was reviewed; it supports risk appetite but does not override readiness, data-health, portfolio, or intake gates"],
  "market_context_coverage_acknowledged": true,
  "market_context_coverage_status": "OK",
  "market_context_coverage_notes": ["market regime, breadth, native-index, and cross-market context were fresh enough that absence of risk_off was not treated as hidden support"],
  "external_market_context_coverage_acknowledged": true,
  "external_market_context_coverage_status": "OK",
  "external_market_context_coverage_notes": ["external context coverage was fresh enough that absence of extra news/macro items was not treated as hidden support"],
  "event_catalyst_support_acknowledged": true,
  "event_catalyst_support_signal_ids": ["event:support-1"],
  "event_catalyst_support_notes": ["positive event-catalyst support was reviewed; it improves confidence only within existing gates and does not create an execution bypass"],
  "event_catalyst_coverage_acknowledged": true,
  "event_catalyst_coverage_status": "OK",
  "event_catalyst_coverage_notes": ["event-catalyst coverage was fresh enough that absence of extra watchlist catalysts was not treated as hidden support"],
  "event_catalyst_signal_coverage_acknowledged": true,
  "event_catalyst_signal_coverage_status": "OK",
  "event_catalyst_signal_coverage_notes": ["event-catalyst signal coverage was fresh enough that absence of extra event-review signals was not treated as hidden support"],
  "fundamentals_context_limit_acknowledged": true,
  "fundamentals_context_symbols": ["00700"],
  "fundamentals_context_missing_metrics": ["pb", "ps", "roe_pct", "earnings_growth_pct"],
  "fundamentals_context_notes": ["fundamentals are Tencent fallback only; PE is available but valuation/profitability/growth/leverage coverage is incomplete"],
  "fundamentals_context_coverage_acknowledged": true,
  "fundamentals_context_coverage_status": "OK",
  "fundamentals_context_coverage_notes": ["fundamentals coverage was fresh enough that absence of extra valuation/profitability warnings was not treated as hidden support"],
  "fundamentals_context_support_acknowledged": true,
  "fundamentals_context_support_symbols": ["00700"],
  "fundamentals_context_support_metrics": ["pe_ttm", "roe_pct", "earnings_growth_pct"],
  "fundamentals_context_support_notes": ["full fresh fundamentals were reviewed; valuation, profitability, and earnings growth support confidence only within existing gates"],
  "market_sentiment_risk_acknowledged": true,
  "market_sentiment_indicator_ids": ["hk-flow"],
  "market_sentiment_notes": ["risk-off capital flow was reviewed; confidence is capped and size is not increased"],
  "market_sentiment_coverage_acknowledged": true,
  "market_sentiment_coverage_status": "OK",
  "market_sentiment_coverage_notes": ["market sentiment coverage was fresh enough that absence of extra risk-off indicators was not treated as hidden support"],
  "market_sentiment_support_acknowledged": true,
  "market_sentiment_support_indicator_ids": ["vix-risk-on"],
  "market_sentiment_support_notes": ["risk-on VIX/capital-flow support was reviewed; it improves context only within existing gates and does not create an execution bypass"],
  "source_reliability_limit_acknowledged": true,
  "source_reliability_components": ["external_market_context", "fundamentals_context"],
  "source_reliability_reasons": ["external_context_only_public_fallback_sources", "fundamentals_partial_metric_coverage"],
  "source_reliability_notes": ["source reliability is degraded, so this judgment does not claim full news/fundamental awareness and confidence is not increased for unverified context"],
  "hermes_alpha_evidence_acknowledged": true,
  "hermes_alpha_evidence_status": "INSUFFICIENT",
  "hermes_alpha_evidence_reasons": ["approved_or_reduced_audit_pass_sample_below_minimum"],
  "hermes_alpha_evidence_notes": ["Hermes approval alpha is not yet proven; this approval is based on current gates only and does not increase size"],
  "intraday_context_acknowledged": true,
  "intraday_context_status": "OK",
  "intraday_context_notes": ["same-session minute context was reviewed; confidence is not increased when intraday momentum contradicts the BUY"],
  "intraday_signal_evidence_acknowledged": true,
  "intraday_signal_evidence_alignment": "challenges_signal",
  "intraday_signal_evidence_codes": ["session_down_challenges_buy"],
  "intraday_signal_evidence_notes": ["5m/15m/30m/60m/session evidence challenged the BUY, so confidence was capped and size was not increased"]
}
```

Valid decisions:

- `approve` - intake may execute the planned quantity.
- `reduce` - intake may execute no more than `max_quantity`.
- `reject` or `hold` - intake rejects the trade.

`reduce.max_quantity` is still rounded down to the market lot size. If the reduced quantity is below one lot, intake rejects the trade instead of forcing an odd-lot order.

For `approve` and `reduce`, `context_review` is mandatory and audited. Hermes must set every required `*_reviewed` field to `true` only after checking the corresponding packet section: technical alert, portfolio risk, strategy evidence, data health, execution readiness, market context, intraday context, external news/macro/capital-flow context, event catalysts, event catalyst review signals, market sentiment, fundamentals context, source reliability, simulation performance, and cron/report wiring. This is the machine-readable proof that the LLM judgment was not a pure technical-rule approval.

`hermes_review_packet.py` exposes the same required `context_review` checklist in top-level `judgment_contract.append_jsonl_object.context_review`, including `intraday_context_reviewed`. Tests compare that packet contract directly with `hermes_judgment_audit_report.py::REQUIRED_CONTEXT_REVIEW_FLAGS`, so Hermes should not receive a stale judgment template that later fails audit. The packet contract also lists structured intraday acknowledgement fields (`intraday_context_acknowledged`, `intraday_context_status`, `intraday_context_notes`) for cases where `context_digest.required_judgment_attention` raises intraday missing/stale, contradiction, or timeframe-coverage-limit items, plus the finer `intraday_signal_evidence_*` fields when 5m/15m/30m/60m/session evidence supports, challenges, conflicts, or has quality/source limits.

`config/hermes_trade_judgment.schema.json` also mirrors the same checklist through `$defs.required_context_review`. For `approve` and `reduce`, the schema requires `context_review` and requires every audit flag to be present with `const: true`; a checkbox object with missing or `false` flags is schema-invalid before it reaches the audit report. This keeps JSON-schema validation, packet guidance, and the judgment audit on the same contract.

When `external_market_context.items[]` relevant to the reviewed BUY symbol, market, or global macro/capital-flow scope contains `sentiment=negative`, Hermes must not approve or reduce the BUY unless the judgment includes:

- `external_market_context_risk_acknowledged=true`;
- `external_market_context_ids[]` containing at least one relevant `external_market_context.items[].id`, title, or URL;
- `external_market_context_notes[]` explaining how negative news, macro, capital-flow, event, or sentiment context affected confidence, sizing, rejection, or hold logic.

`hermes_judgment_audit_report.py` fails missing evidence with `missing_external_market_context_risk_acknowledgement`, `external_market_context_ids_missing_or_unmatched`, or `external_market_context_notes_missing`.

When `external_market_context.items[]` relevant to the reviewed BUY symbol, market, or global macro/capital-flow scope contains fresh `sentiment=positive` context with `impact_score` or `score >= 0.7`, Hermes may use it as support only if the judgment includes:

- `external_market_context_support_acknowledged=true`;
- `external_market_context_support_ids[]` containing at least one relevant positive `external_market_context.items[].id`, title, or URL;
- `external_market_context_support_notes[]` explaining how positive news, macro, capital-flow, event, or sentiment context affected confidence or sizing.

This is a support contract, not a bypass. Positive external context must not override data-health, execution-readiness, system-health, portfolio-risk, source-reliability, or order-intake blocks. `hermes_judgment_audit_report.py` fails missing support evidence with `missing_external_market_context_support_acknowledgement`, `external_market_context_support_ids_missing_or_unmatched`, or `external_market_context_support_notes_missing`.

`external_market_context_report.py` also tags each item with `provider_grade=trusted|public_fallback|unknown`. A public-fallback or unknown positive high-impact item may still be useful as context, but it is not broker, official, or vendor-grade evidence. Such items increment `summary.fallback_positive_high_impact_count` or `summary.unknown_positive_high_impact_count`, and `source_reliability_report.py` degrades the `external_market_context` component with `external_context_positive_high_impact_public_fallback` or `external_context_positive_high_impact_unknown_provider`. Hermes can still cite the item, but any approve/reduce judgment must also include the normal `source_reliability_*` acknowledgement and must not increase confidence as though the catalyst were independently trusted.

When `review_items[].context_digest.required_judgment_attention[]` contains `market_context_coverage_limit_requires_acknowledgement`, the market-regime context layer itself is missing, stale, risky, failed, invalid, or missing the reviewed signal's HK/US market slice. Hermes must not treat the absence of `risk_off_market_context_requires_exception_for_buy` as evidence that the market is benign. Any `approve` or `reduce` judgment must include:

- `market_context_coverage_acknowledged=true`;
- `market_context_coverage_status` matching the reviewed `context_digest.market_context.status`;
- `market_context_coverage_notes[]` explaining how the coverage limit affected confidence, sizing, rejection, or hold logic.

`hermes_judgment_audit_report.py` fails missing coverage acknowledgement with `missing_market_context_coverage_acknowledgement`, `market_context_coverage_notes_missing`, or `market_context_coverage_status_mismatch`. This is an additive judgment contract only: it does not change alert selection, order intake, DB rows, simulation execution, crontab, or market-context generation.

When `review_items[].context_digest.required_judgment_attention[]` contains `external_market_context_coverage_limit_requires_acknowledgement`, the news/macro/event/capital-flow context layer itself is missing, stale, risky, failed, or invalid. Hermes must not treat the absence of `external_market_context.items[]` as evidence that no relevant news, macro, event, or capital-flow risk exists. Any `approve` or `reduce` judgment must include:

- `external_market_context_coverage_acknowledged=true`;
- `external_market_context_coverage_status` matching the reviewed `context_digest.external_market_context.status`;
- `external_market_context_coverage_notes[]` explaining how the coverage limit affected confidence, sizing, rejection, or hold logic.

`hermes_judgment_audit_report.py` fails missing coverage acknowledgement with `missing_external_market_context_coverage_acknowledgement`, `external_market_context_coverage_notes_missing`, or `external_market_context_coverage_status_mismatch`.

When `event_catalyst_signals.signals[]` contains a `CHALLENGE_BUY_REVIEW` whose `related_v5_signal_ids[]` includes the reviewed BUY, Hermes must not approve or reduce the BUY unless the judgment includes:

- `event_catalyst_risk_acknowledged=true`;
- `event_catalyst_signal_ids[]` containing the relevant `event_catalyst_signals.signals[].signal_id`;
- `event_catalyst_risk_notes[]` explaining why the negative event challenge was accepted, reduced, or overridden.

`hermes_judgment_audit_report.py` fails missing evidence with `missing_event_catalyst_signal_risk_acknowledgement`, `event_catalyst_signal_ids_missing_or_unmatched`, or `event_catalyst_signal_risk_notes_missing`.

When `event_catalyst_signals.signals[]` contains a `SUPPORT_BUY_REVIEW` whose `related_v5_signal_ids[]` includes the reviewed BUY, Hermes may use it as support only if the judgment includes:

- `event_catalyst_support_acknowledged=true`;
- `event_catalyst_support_signal_ids[]` containing the relevant `event_catalyst_signals.signals[].signal_id`;
- `event_catalyst_support_notes[]` explaining how the positive event catalyst affected confidence or sizing.

This is deliberately separate from the negative event risk fields. SUPPORT_BUY_REVIEW may strengthen the explanation for an already eligible technical signal, but it must not override technical confirmation, data-health, execution-readiness, system-health, source-reliability, portfolio-risk, or order-intake blocks. `hermes_judgment_audit_report.py` fails missing support evidence with `missing_event_catalyst_support_acknowledgement`, `event_catalyst_support_signal_ids_missing_or_unmatched`, or `event_catalyst_support_notes_missing`.

When `review_items[].context_digest.required_judgment_attention[]` contains `event_catalyst_coverage_limit_requires_acknowledgement`, the watchlist-linked event-catalyst layer itself is missing, stale, risky, failed, or invalid. Hermes must not treat the absence of `event_catalysts.candidates[]` as evidence that no watchlist event risk exists. Any `approve` or `reduce` judgment must include:

- `event_catalyst_coverage_acknowledged=true`;
- `event_catalyst_coverage_status` matching the reviewed `context_digest.event_catalysts.status`;
- `event_catalyst_coverage_notes[]` explaining how the coverage limit affected confidence, sizing, rejection, or hold logic.

`hermes_judgment_audit_report.py` fails missing coverage acknowledgement with `missing_event_catalyst_coverage_acknowledgement`, `event_catalyst_coverage_notes_missing`, or `event_catalyst_coverage_status_mismatch`.

When `review_items[].context_digest.required_judgment_attention[]` contains `event_catalyst_signal_coverage_limit_requires_acknowledgement`, the event-driven review-signal layer itself is missing, stale, risky, failed, or invalid. Hermes must not treat the absence of `event_catalyst_signals.signals[]` as evidence that no event risk exists. Any `approve` or `reduce` judgment must include:

- `event_catalyst_signal_coverage_acknowledged=true`;
- `event_catalyst_signal_coverage_status` matching the reviewed `context_digest.event_catalyst_signals.status`;
- `event_catalyst_signal_coverage_notes[]` explaining how the coverage limit affected confidence, sizing, rejection, or hold logic.

`hermes_judgment_audit_report.py` fails missing coverage acknowledgement with `missing_event_catalyst_signal_coverage_acknowledgement`, `event_catalyst_signal_coverage_notes_missing`, or `event_catalyst_signal_coverage_status_mismatch`.

When `market_sentiment.indicators[]` relevant to the reviewed BUY symbol, market, or global scope contains `direction=risk_off`, `sentiment=negative`, or a negative `score`, Hermes must not approve or reduce the BUY unless the judgment includes:

- `market_sentiment_risk_acknowledged=true`;
- `market_sentiment_indicator_ids[]` containing at least one relevant `market_sentiment.indicators[].id`, name, or indicator type;
- `market_sentiment_notes[]` explaining how quantified risk-off/negative sentiment affected confidence, sizing, rejection, or hold logic.

`hermes_judgment_audit_report.py` fails missing evidence with `missing_market_sentiment_risk_acknowledgement`, `market_sentiment_indicator_ids_missing_or_unmatched`, or `market_sentiment_notes_missing`.

When `market_sentiment.indicators[]` relevant to the reviewed BUY symbol, market, or global scope contains fresh `direction=risk_on|positive` context with `score >= 0.25`, Hermes may use it as support only if the judgment includes:

- `market_sentiment_support_acknowledged=true`;
- `market_sentiment_support_indicator_ids[]` containing at least one relevant `market_sentiment.indicators[].id`, name, or indicator type;
- `market_sentiment_support_notes[]` explaining how quantified risk-on/positive sentiment affected confidence or sizing.

This support contract is separate from the risk-off acknowledgement. Risk-on sentiment may strengthen the explanation for an otherwise eligible BUY, but it must not override technical confirmation, data-health, execution-readiness, system-health, source-reliability, portfolio-risk, or order-intake blocks. `hermes_judgment_audit_report.py` fails missing support evidence with `missing_market_sentiment_support_acknowledgement`, `market_sentiment_support_indicator_ids_missing_or_unmatched`, or `market_sentiment_support_notes_missing`.

When `review_items[].context_digest.required_judgment_attention[]` contains `market_sentiment_coverage_limit_requires_acknowledgement`, the volatility/capital-flow/risk-appetite sentiment layer itself is missing, stale, risky, failed, or invalid. Hermes must not treat the absence of `market_sentiment.indicators[]` as evidence that risk appetite is normal. Any `approve` or `reduce` judgment must include:

- `market_sentiment_coverage_acknowledged=true`;
- `market_sentiment_coverage_status` matching the reviewed `context_digest.market_sentiment.status`;
- `market_sentiment_coverage_notes[]` explaining how the coverage limit affected confidence, sizing, rejection, or hold logic.

`hermes_judgment_audit_report.py` fails missing coverage acknowledgement with `missing_market_sentiment_coverage_acknowledgement`, `market_sentiment_coverage_notes_missing`, or `market_sentiment_coverage_status_mismatch`.

When `market_context.markets.<market>.cross_market.alignment=conflicts_with_breadth`, any `approve` or `reduce` judgment for a `BUY` must explicitly discuss both sides of the conflict: stock-pool breadth and cross-market sentiment/index/VIX evidence. `hermes_judgment_audit_report.py` accepts that discussion in `supporting_factors`, `opposing_factors`, `risk_notes`, `context_review.notes`, or `market_regime_exception_reason`. Missing either side fails the audit with `cross_market_conflict_breadth_not_discussed` or `cross_market_conflict_sentiment_not_discussed`.

When `fundamentals_context.items[]` for the reviewed BUY symbol contains `valuation_flags=["partial_fundamentals"]`, `source=tencent_quote_snapshot`, or `fundamental_completeness.level=partial|empty`, Hermes must not approve or reduce the BUY unless the judgment includes:

- `fundamentals_context_limit_acknowledged=true`;
- `fundamentals_context_symbols[]` containing the reviewed symbol;
- `fundamentals_context_missing_metrics[]` containing at least one missing metric from `fundamental_completeness.missing_metrics`;
- `fundamentals_context_notes[]` explaining that the available metrics are incomplete fallback context and how this affected confidence or sizing.

`hermes_judgment_audit_report.py` fails missing evidence with `missing_fundamentals_context_limit_acknowledgement`, `fundamentals_context_symbols_missing_or_unmatched`, `fundamentals_context_missing_metrics_not_discussed`, or `fundamentals_context_notes_missing`.

When `review_items[].context_digest.required_judgment_attention[]` contains `fundamentals_context_coverage_limit_requires_acknowledgement`, the valuation/profitability/growth/dividend/leverage context layer itself is missing, stale, risky, failed, or invalid. Hermes must not treat the absence of `fundamentals_context.items[]` as evidence that fundamental risk is neutral. Any `approve` or `reduce` judgment must include:

- `fundamentals_context_coverage_acknowledged=true`;
- `fundamentals_context_coverage_status` matching the reviewed `context_digest.fundamentals_context.status`;
- `fundamentals_context_coverage_notes[]` explaining how the coverage limit affected confidence, sizing, rejection, or hold logic.

`hermes_judgment_audit_report.py` fails missing coverage acknowledgement with `missing_fundamentals_context_coverage_acknowledgement`, `fundamentals_context_coverage_notes_missing`, or `fundamentals_context_coverage_status_mismatch`.

When `fundamentals_context.items[]` for the reviewed BUY symbol is fresh, `fundamental_completeness.level=full`, and has no `valuation_flags`, Hermes may use it as support only if the judgment includes:

- `fundamentals_context_support_acknowledged=true`;
- `fundamentals_context_support_symbols[]` containing the reviewed symbol;
- `fundamentals_context_support_metrics[]` listing the specific valuation, profitability, growth, dividend, or leverage metrics used as support;
- `fundamentals_context_support_notes[]` explaining how those metrics affected confidence or sizing.

This support contract is separate from the partial/fallback limitation contract. Full fundamentals can strengthen the explanation for an otherwise eligible BUY, but must not override technical confirmation, data-health, execution-readiness, system-health, source-reliability, portfolio-risk, or order-intake blocks. `hermes_judgment_audit_report.py` fails missing support evidence with `missing_fundamentals_context_support_acknowledgement`, `fundamentals_context_support_symbols_missing_or_unmatched`, `fundamentals_context_support_metrics_missing_or_unmatched`, or `fundamentals_context_support_notes_missing`.

When top-level `source_reliability.status` is `DEGRADED`, `STALE`, `MISSING`, or `FAIL`, Hermes must not approve or reduce unless the judgment includes:

- `source_reliability_limit_acknowledged=true`;
- `source_reliability_components[]` containing at least one affected `source_reliability.components[].name`;
- `source_reliability_reasons[]` containing at least one matching component reason or report recommendation;
- `source_reliability_notes[]` explaining how source-quality limits affected confidence, sizing, rejection, or hold logic.

This is deliberately stricter than `context_review.source_reliability_reviewed=true`. The checkbox proves Hermes opened the source matrix; the structured fields prove Hermes understood the limitation. `hermes_judgment_audit_report.py` fails missing evidence with `missing_source_reliability_limit_acknowledgement`, `source_reliability_components_missing_or_unmatched`, `source_reliability_reasons_missing_or_unmatched`, or `source_reliability_notes_missing`.

When top-level `strategy_learning_brief.hermes_alpha_evidence.status` is `INSUFFICIENT`, `NEGATIVE`, `MISSING`, or `INVALID`, or when the packet has no `strategy_learning_brief.hermes_alpha_evidence`, Hermes must not approve or reduce unless the judgment includes:

- `hermes_alpha_evidence_acknowledged=true`;
- `hermes_alpha_evidence_status` matching the reviewed status, or `MISSING` when the evidence object is absent;
- `hermes_alpha_evidence_reasons[]` containing at least one reviewed `strategy_learning_brief.hermes_alpha_evidence.reasons[]` value when reasons are present;
- `hermes_alpha_evidence_notes[]` explaining why the LLM layer is not being treated as proven alpha and how that affected confidence, sizing, rejection, or hold logic.

This is a discipline contract, not an execution block by itself. Weak or missing Hermes alpha evidence should keep approvals conservative, but it does not submit orders, mutate simulation state, change strategy settings, or override the normal intake/readiness gates. `hermes_judgment_audit_report.py` fails missing evidence with `missing_hermes_alpha_evidence_acknowledgement`, `hermes_alpha_evidence_status_missing`, `hermes_alpha_evidence_status_mismatch`, `hermes_alpha_evidence_reasons_missing_or_unmatched`, or `hermes_alpha_evidence_notes_missing`.

When `review_items[].context_digest.required_judgment_attention[]` contains `intraday_context_missing_or_stale_requires_disclosure`, `intraday_context_challenges_buy_requires_discussion`, `intraday_context_challenges_sell_requires_discussion`, `intraday_context_timeframe_conflict_requires_disclosure`, `intraday_timeframe_coverage_limited_requires_disclosure`, `intraday_context_quality_degraded_requires_disclosure`, `intraday_market_not_open_requires_session_context`, or `intraday_market_session_overrides_limit_requires_disclosure`, Hermes must not approve or reduce unless the judgment includes:

- `intraday_context_acknowledged=true`;
- `intraday_context_status` matching the reviewed `review_items[].context_digest.intraday_context.status` when known;
- `intraday_context_notes[]` explaining how same-session momentum, missing/stale minute coverage, incomplete rolling 5m/15m/30m/60m coverage, contradiction, market-session state, calendar-override limits, or minute-bar quality affected confidence, sizing, rejection, or hold logic.

This is deliberately stricter than `context_review.intraday_context_reviewed=true`. The checkbox proves Hermes opened the intraday section; the structured fields prove Hermes responded to the contradiction or data gap. `hermes_judgment_audit_report.py` fails missing evidence with `missing_intraday_context_acknowledgement`, `intraday_context_notes_missing`, or `intraday_context_status_mismatch`.

`review_items[].context_digest.intraday_signal_evidence` is the compact per-signal 5m/15m/30m/60m/session evidence contract. It maps the raw minute digest into `alignment=supports_signal|supports_with_limits|challenges_signal|conflicting_timeframes|limited_context|unavailable_or_stale|neutral_or_insufficient`, with `support_codes`, `challenge_codes`, `conflict_codes`, `quality_codes`, `limit_codes`, and a flattened `codes[]`. When `requires_judgment_acknowledgement=true`, Hermes must not approve or reduce unless the judgment includes:

- `intraday_signal_evidence_acknowledged=true`;
- `intraday_signal_evidence_alignment` copied from the reviewed `context_digest.intraday_signal_evidence.alignment`;
- `intraday_signal_evidence_codes[]` containing at least one reviewed `context_digest.intraday_signal_evidence.codes[]` value when codes are present;
- `intraday_signal_evidence_notes[]` explaining whether 5m/15m/30m/60m/session evidence supported, challenged, conflicted with, had incomplete rolling-window coverage, or was too low-quality to strengthen the judgment.

This is additive to the older `intraday_context_*` fields and is still review-only. It does not submit orders, change alert eligibility, write minute rows, alter cron, promote strategy settings, or replace daily K-lines as the forward-return authority. `hermes_judgment_audit_report.py` fails missing evidence with `missing_intraday_signal_evidence_acknowledgement`, `intraday_signal_evidence_alignment_missing`, `intraday_signal_evidence_alignment_mismatch`, `intraday_signal_evidence_codes_missing_or_unmatched`, or `intraday_signal_evidence_notes_missing`.

`strategy_learning_report.py` also emits `intraday_alignment_effect`, and `strategy_learning_brief.intraday_signal_alignment` copies its `evidence_status`, `evidence_reasons`, `support_vs_challenge_delta_pct`, and read-only `policy` into the Hermes packet. This is the empirical layer that answers whether past signals with intraday support actually outperformed signals challenged by 5m/15m/30m/60m/session evidence. `SUPPORTIVE` means the alignment can be used as soft confirmation and confidence capping context; it still does not grant execution permission. `INSUFFICIENT`, `MISSING`, or `NEGATIVE` means Hermes must treat intraday alignment as diagnostic only and should not promote it into a hard hold/approve rule without more resolved forward outcomes.

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
- latest `external_market_context_report.py` news/macro/event/capital-flow context when `/tmp/external_market_context_report.json` exists;
- latest `event_catalyst_report.py` watchlist-linked external catalysts when `/tmp/event_catalyst_report.json` exists;
- latest `event_catalyst_signal_report.py` event-driven review signals when `/tmp/event_catalyst_signal_report.json` exists;
- latest `market_sentiment_report.py` quantified volatility/capital-flow/risk-appetite context when `/tmp/market_sentiment_report.json` exists;
- latest `fundamentals_context_report.py` valuation/profitability/growth/leverage context when `/tmp/fundamentals_context_report.json` exists;
- latest `cron_audit_report.py` read-only job wiring context when `/tmp/cron_audit_report.json` exists;
- latest `kline_daily_gap_repair.py` dry-run plan context when `/tmp/kline_daily_gap_repair.json` exists;
- latest `kline_gap_source_diagnostic_report.py` unresolved daily-gap source/mapping/universe classifications when `/tmp/kline_gap_source_diagnostic_report.json` exists;
- latest `kline_gap_alternate_provider_probe.py` unresolved daily-gap alternate-provider comparison when `/tmp/kline_gap_alternate_provider_probe.json` exists;
- latest `kline_gap_alternate_provider_repair_plan.py` quality-gated alternate-provider repair candidates when `/tmp/kline_gap_alternate_provider_repair_plan.json` exists;
- latest `intraday_context_report.py` minute-bar confirmation/contradiction context when `/tmp/intraday_context_report.json` exists;
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

When selected alerts are all stale or otherwise observation-only, `review_items[]` is intentionally empty and the packet exposes `review_item_suppression`. Hermes and operators should read `review_item_suppression.status`, `reason_counts`, and `recommendations` before treating an empty packet as a broken review path. `ALL_SELECTED_ALERTS_SUPPRESSED` with `alert_too_old` means the correct action is to wait for fresh confirmed alerts or run packet generation during the relevant market session; Hermes should not write trade judgments for those stale observations.

For readiness freshness gates, keep a machine-readable system health snapshot at the default path:

```bash
/usr/bin/python3 /root/system_health_check.py --output /tmp/quantmind_system_health.json
```

By default each generated packet is also archived by `packet_id` under:

```bash
/tmp/hermes_review_packet_archive/
```

This archive is read by the judgment audit so historical Hermes judgments are checked against the exact packet version they reviewed.

Archive retention is enforced inside `hermes_review_packet.py` before and after each snapshot write. It only prunes `.json` files under the real `/tmp/hermes_review_packet_archive` directory and keeps the newest evidence subject to three limits:

- `HERMES_REVIEW_PACKET_ARCHIVE_MAX_FILES`, default `360`;
- `HERMES_REVIEW_PACKET_ARCHIVE_MAX_AGE_HOURS`, default `24`;
- `HERMES_REVIEW_PACKET_ARCHIVE_MAX_BYTES`, default `1073741824` bytes.

The newest snapshot is preserved even if one unusually large packet exceeds the byte cap. This is a no-loss integration change for live trading paths: it does not edit judgments, alert queues, DB rows, strategy settings, crontab, or simulation state. It only bounds diagnostic packet snapshots so a one-minute packet cron cannot fill `/tmp` and break data-health or Postgres temp-file operations. If Hermes needs longer-horizon judgment QA, export the archive elsewhere or raise the limits deliberately; do not remove the retention guard on the production root disk.

By default the packet scans the latest 500 raw JSONL alerts, scopes them to the latest `strategy_config_id + watchlist_id`, and selects up to 20 confirmed directional `BUY`/`SELL` alerts (`confirmed=true`) for trade review. `WATCH` alerts, unconfirmed candidates, and older config/watchlist rows are counted in `alert_selection.sample_scope`, but are not sent through order-intake dry-run unless `--include-watch`, `--include-unconfirmed`, or `--sample-scope all` is explicitly used for debugging or historical research.

Confirmed directional alerts rejected by order intake with `sell_without_position` or `alert_too_old` are not trade candidates. `sell_without_position` means the simulation portfolio has no position to reduce or exit; `alert_too_old` means the signal missed the configured freshness window. They are moved out of `review_items` into top-level `non_actionable_observations` with `recommended_use=observation_only_no_trade_judgment_required`. This is an additive, lossless packet change:

- source alert counts stay in `alert_selection`;
- the alert and compact intake rejection remain visible for diagnostics and strategy learning;
- Hermes should not write trade judgments for `non_actionable_observations`;
- existing consumers that only read `review_items` continue to work, but see fewer non-executable SELL rows.

Each `review_items[]` row now also includes `context_digest` with schema `hermes_review_item_context_digest_v1`. This is a read-only attention layer that maps the top-level context reports to the specific signal under review:

- `market_context.markets.<market>` matched by signal market, including regime, weak-breadth notes, `native_index_context`, and `cross_market` alignment;
- `external_market_context.items[]` matched by normalized symbol, same market, or global macro/capital-flow/sentiment context;
- `event_catalysts.candidates[]` matched by symbol, market, or global macro/capital-flow scope, with `relevant_candidate_count`, `negative_candidate_count`, and `positive_candidate_count` so Hermes and Feishu can distinguish event risk from event support without inferring from the truncated candidate list;
- `event_catalyst_signals.signals[]` matched first by `related_v5_signal_ids[]`, then by symbol or market;
- `market_sentiment.indicators[]` matched by market or global scope;
- `fundamentals_context.items[]` matched by reviewed symbol;
- `trusted_source_preflight` and `source_reliability` warning/degraded components and recommendations.

Hermes should read `review_items[].context_digest.required_judgment_attention` before writing any `approve` or `reduce` judgment. Typical entries include `risk_off_market_context_requires_exception_for_buy`, `market_context_coverage_limit_requires_acknowledgement`, `buy_signal_against_weak_breadth_requires_explicit_review`, `native_index_public_fallback_requires_source_limit_acknowledgement`, `native_index_conflicts_with_breadth_requires_discussion`, `external_market_context_coverage_limit_requires_acknowledgement`, `event_catalyst_signal_challenges_buy_requires_acknowledgement`, `market_sentiment_coverage_limit_requires_acknowledgement`, `fundamentals_context_coverage_limit_requires_acknowledgement`, `fundamentals_context_limit_requires_acknowledgement`, `intraday_market_session_overrides_limit_requires_disclosure`, `trusted_source_preflight_limit_requires_disclosure`, and `source_reliability_limit_requires_acknowledgement`. These entries tell Hermes which structured acknowledgment fields the judgment audit is likely to require.

No-loss integration contract:

- `context_digest` is additive; existing readers can ignore it.
- It does not change `review_items[].eligible_for_approval`, `recommended_judgment`, or `blocking_reasons`.
- It does not submit orders, edit strategy, edit alert queues, edit crontab, repair K-lines, or write ingest files.
- The top-level packet sections remain authoritative; `context_digest` is a compact pointer set so Hermes does not miss relevant evidence buried in larger reports.
- If `context_digest` says no relevant item was found, Hermes must treat current-event, sentiment, or fundamentals awareness as limited rather than inventing support.
- Positive event-catalyst context may strengthen the narrative for an otherwise eligible BUY, but it does not override technical confirmation, readiness, data-health, source-reliability, portfolio-risk, simulation-performance, or intake gates.
- If `context_digest.market_context.native_index_context.primary_index.provider_grade=public_fallback`, Hermes may use it as fallback evidence but must not describe it as broker, vendor, or official index data.

Testing without writing the intake dry-run ledger:

```bash
/usr/bin/python3 /root/hermes_review_packet.py --ephemeral-state --stdout --output ''
```

The packet is review-only. It does not submit simulation orders. Hermes should use `review_items[].eligible_for_approval` as a hard gate:

- `true` means Hermes may still approve or reduce only after independent LLM review;
- `false` means Hermes must write `reject` or `hold`, or write no judgment.

`review_items[].eligible_for_approval` is now also forced to `false` when top-level `execution_readiness.status` is not `READY`, `ready_for_execute` is not `true`, or the per-alert dry-run intake result reports `execution_readiness.would_block_execute=true`. This is intentionally redundant with `rt_order_intake.py --mode execute`: Hermes sees the same closed gate in review packets before it can write a trade judgment, while execute mode still remains the authoritative submit path.

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
- recent `sim_trades` for FIFO-style closed trade review, preserving available `id`, `trade_id`, and `order_id` lineage;
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
- Closed-trade rows carry entry/exit trade and order IDs when the backend exposes them. This does not prove signal quality by itself; `simulation_performance_report.py` checks whether those order IDs can be traced back to processed v5 intake decisions.
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
- `scripts/kline_daily_gap_repair.py`
- `scripts/kline_gap_source_diagnostic_report.py`
- `scripts/kline_gap_alternate_provider_probe.py`
- `scripts/kline_gap_alternate_provider_repair_plan.py`
- `scripts/intraday_kline_batch.py`
- `scripts/intraday_context_report.py`
- `scripts/market_context_report.py`
- `scripts/external_market_context_report.py`
- `scripts/external_market_context_ingest.py`
- `scripts/event_catalyst_report.py`
- `scripts/event_catalyst_signal_report.py`
- `scripts/market_sentiment_report.py`
- `scripts/market_sentiment_ingest.py`
- `scripts/fundamentals_context_ingest.py`
- `scripts/fundamentals_context_producer.py`
- `scripts/fundamentals_context_report.py`
- `scripts/trusted_source_preflight.py`
- `scripts/trusted_source_discovery_report.py`
- `scripts/cron_audit_report.py`
- `scripts/cron_install_promote.py`
- `scripts/source_reliability_report.py`
- `scripts/readiness_refresh.py`
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
- `/tmp/rt_signal_state.json` stores normalized cooldown state and is rewritten atomically.

### Phase 3: Add Hermes bridge in notify-only mode

Use the safe cron example in `config/hermes_v5_crontab.txt`:

```bash
* * * * * RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py
```

This mode does not touch the simulation trading system. It only emits formatted Hermes text. When `/tmp/hermes_signal_review_packet.json` is present and fresh, the text includes the matching review item's `eligible_for_approval`, `recommended_judgment`, compact `blocking_reasons`, top-level `simulation_performance.status` and reason codes, top-level `strategy_learning_brief.hermes_alpha_evidence`, top-level `execution_readiness.status/ready_for_execute` with compact blocking or warning gate names, plus the matching `context_digest`. The Feishu/Hermes-facing alert therefore shows whether the technical signal is currently reviewable or should be held/rejected, whether Hermes approval has enough realized alpha evidence, whether the aggregate system is ready for execute mode, and whether the signal is supported or contradicted by market regime, intraday data, news/events, direct event-review signals, sentiment, fundamentals, simulation feedback, and source-quality warnings. Direct event-review signals are shown as `事件審核：CHALLENGE_BUY_REVIEW:...` or `事件審核：SUPPORT_BUY_REVIEW:...` when the packet has `event_catalyst_signals.signals[]` matched to the alert by `related_v5_signal_ids[]`. If external/news, event-catalyst, event-review-signal, sentiment, or fundamentals context has no matched detail rows but its `status` is not clean, the notification still shows compact coverage lines such as `新聞/宏觀覆蓋：STALE`, `事件覆蓋：MISSING`, `事件審核覆蓋：RISK`, `情緒覆蓋：RISK`, or `基本面覆蓋：STALE`; missing rows are therefore not presented as neutral evidence. Intraday signal evidence is shown as `分鐘證據：supports_with_limits codes=... ack=required`, using `context_digest.intraday_signal_evidence` so Hermes/operator can see whether 5m/15m/30m/60m/session context supports, challenges, conflicts, or has quality/source limits without opening the packet. Source reliability is shown as `來源可靠性：DEGRADED component:reason...`, using compact problem components from `context_digest.source_limits` so public-fallback, partial-fundamentals, preflight, or provenance weaknesses are visible in the Feishu alert. `Hermes Alpha：INSUFFICIENT/NEGATIVE/SUPPORTIVE`, `執行準備：READY/WARN/BLOCKED`, coverage lines, `來源可靠性：...`, and `分鐘證據：...` are display-only evidence from the packet; they do not change alert eligibility, submit orders, mutate simulation state, or promote strategy settings. If the packet is stale, missing, invalid, or has no matching `signal_id`, the bridge downgrades the wording to `Hermes審核狀態：STALE/MISSING/INVALID` or `Hermes審核：NO_MATCH` and explicitly says the alert is only a technical signal until manual/Hermes review catches up. Hermes must not treat those downgraded notifications as approved comprehensive judgments.

The bridge also emits advisory-only position-review notifications when `RT_ALERT_INCLUDE_POSITION_REVIEW=1` (default) and the latest packet has unsent high-urgency `position_review.items[]`. This works even when there are no new actionable BUY/SELL alerts, so high-risk user or simulation holdings are not hidden behind an empty signal queue. The bridge reads `position_review.items[].review_id`, `urgency`, `recommended_action`, `execution_policy`, position PnL/stop distance, latest signal, `context_digest.position_attention[]`, and `position_judgment_audit.coverage.unjudged_high_urgency_review_count`, then prints a `Hermes持倉風險審核` section for Feishu/Hermes. Each item includes `審核ID：review_id=... judgment_file=/tmp/hermes_position_judgments.jsonl` so Hermes can copy the exact key required by `hermes_position_judgment_audit_report.py` without opening the raw packet. It also prints audit-facing requirements: the five required `context_review` flags, every `position_attention` code that must be copied into `position_attention_codes[]`, `position_attention_acknowledged=true`, `position_attention_effects[]=one_per_code`, and the allowed advisory-only decisions `hold|watch|reduce|exit|trail_stop`. It writes only a separate dedupe file, `/tmp/rt_position_review_sent.json` by default, keyed by `review_id`; it does not append to the alert sent state, call `rt_order_intake.py`, submit orders, change portfolio rows, change simulation state, or write Hermes position judgments. If `position_judgment_audit.coverage.unjudged_high_urgency_examples[]` still contains a previously sent `review_id`, the bridge may remind after `RT_POSITION_REVIEW_REMINDER_HOURS=6` by default; this prevents one missed Feishu delivery from permanently hiding an unresolved high-risk holding while avoiding every-minute spam. Operators can set `RT_POSITION_REVIEW_URGENCY=high,medium` and `RT_POSITION_REVIEW_LIMIT=3` to widen or cap the advisory queue. `RT_ALERT_INCLUDE_POSITION_REVIEW=0` restores the old alert-only bridge behavior without affecting packet generation or position judgment audit reports.

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

`quantmind_sim_trader.py` is now fail-closed by default. Even if `legacy-sim` is selected, the legacy trader exits before login, signal reads, or order submission unless `QM_LEGACY_SIM_TRADER_ENABLE=1` is present in the runtime environment. API credentials must also come from `QM_LEGACY_SIM_API_USER`/`QM_LEGACY_SIM_API_PASSWORD` or the standard `QM_API_USER`/`QM_API_PASSWORD`; the script has no code-level credential fallback. This keeps the compatibility path available for reviewed testing while preventing accidental untraceable simulation orders.

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

### Data source inventory report

`scripts/data_source_inventory_report.py` is the server-side visibility ledger for Hermes. It is read-only and answers what the system can actually see before any source-quality judgment is made:

- core DB tables: `stocks`, `klines`, `engine_signal_scores`, `engine_feature_runs`, `portfolios`, `positions`, and `sim_trades`;
- K-line intervals, `data_source` values, optional `source_granularity`, row counts, symbol counts, and date ranges;
- signal model/feature versions and trade-date ranges;
- portfolio ids and whether the configured simulation portfolio id is separate from configured user portfolio ids;
- `/tmp` context reports such as data health, K-line source-granularity proposal, market context, intraday context, intraday timeframe quality, external context, sentiment, fundamentals, source reliability, portfolio report, simulation performance, and execution readiness;
- provider/input payload files such as `/tmp/external_market_context_inputs.json`, `/tmp/market_sentiment_inputs.json`, `/tmp/fundamentals_context_inputs.json`, `/tmp/market_index_context_inputs.json`, and `/root/rt_signal_watchlist.json`.

Default command:

```bash
/usr/bin/python3 /root/data_source_inventory_report.py --output /tmp/data_source_inventory_report.json --text
```

The output schema is `data_source_inventory_report_v1`. `status=OK` means the inventory found no known visibility weakness. `status=DEGRADED` means Hermes can see the system, but visibility is incomplete, for example missing K-line `data_source`, minute rows without `source_granularity`, stale/missing context reports, context schema mismatches, missing external payload files, or query warnings. `status=FAIL` means a critical visibility layer is missing or unusable, for example missing core DB tables or an empty K-line table.

Hermes packet integration is lossless: `hermes_review_packet.py` embeds the report as top-level `data_source_inventory` when available. Existing jobs can ignore the new section. Hermes should use it to avoid overclaiming source awareness; it is not a trade signal, does not change eligibility, and does not submit orders. Source quality remains controlled by `source_reliability_report.py`, which now reads the inventory and maps warning/error weaknesses into `data_source_inventory_weaknesses` or `data_source_inventory_errors`.

Recommended read-only cron:

```bash
*/10 * * * * /usr/bin/python3 /root/data_source_inventory_report.py --output /tmp/data_source_inventory_report.json --text >> /tmp/data_source_inventory_report.log 2>&1
```

### K-line source granularity report

`scripts/kline_source_granularity_report.py` is the dry-run provenance proposal for minute/hour/daily bar fidelity. It exists because `data_source=tencent_min` or `tencent_minute_query` proves only the provider family; it does not prove whether each minute row is a full OHLCV bar or a one-price-per-minute snapshot.

Default command:

```bash
/usr/bin/python3 /root/kline_source_granularity_report.py --output /tmp/kline_source_granularity_report.json --text
```

The output schema is `kline_source_granularity_report_v1`. `status=OK` means `klines.source_granularity` exists and known source groups are already labelled. `status=ACTION_REQUIRED` means the report generated a hash-stamped `proposal` to add `klines.source_granularity` and/or backfill safe mappings. `status=REVIEW` means some missing granularity groups have no safe mapping and must not be inferred. `status=FAIL` means the `klines` table or metadata is not visible.

The proposal is intentionally conservative:

- `tencent_min` and `tencent_minute_query` minute rows map to `minute_snapshot_price`, not `minute_ohlcv`;
- broker/vendor/official full minute bars may map to `minute_ohlcv` only when their `data_source` already says so;
- daily Tencent/Yahoo rows map to `daily_ohlcv`, while `data_source` continues to carry provider and repair provenance;
- rows with missing or unknown `data_source` are not inferred.

Apply is manual and hash-gated:

```bash
/usr/bin/python3 /root/kline_source_granularity_report.py \
  --apply \
  --confirm-proposal-hash <proposal.proposal_hash> \
  --output /tmp/kline_source_granularity_report.json \
  --text
```

The apply path updates only `klines.source_granularity` and optionally adds that column. It does not change OHLCV prices, volumes, positions, strategy, watchlists, crontab, alert queues, Hermes judgments, or orders. After any approved apply, rerun:

```bash
/usr/bin/python3 /root/data_source_inventory_report.py --output /tmp/data_source_inventory_report.json --text
/usr/bin/python3 /root/intraday_context_report.py --output /tmp/intraday_context_report.json --text
/usr/bin/python3 /root/intraday_timeframe_quality_report.py --output /tmp/intraday_timeframe_quality_report.json --text
/usr/bin/python3 /root/source_reliability_report.py --output /tmp/source_reliability_report.json --text
/usr/bin/python3 /root/hermes_review_packet.py --output /tmp/hermes_signal_review_packet.json --no-archive
```

Hermes packet integration is lossless: `hermes_review_packet.py` embeds the report as top-level `kline_source_granularity`. Source reliability also reads it as `kline_source_granularity`; pending schema/backfill proposals degrade source reliability until reviewed. This is not a trade signal and does not make public snapshot minute rows institution-grade. It only prevents the system from falsely claiming full intraday OHLCV path evidence.

Recommended read-only cron:

```bash
*/30 * * * * /usr/bin/python3 /root/kline_source_granularity_report.py --output /tmp/kline_source_granularity_report.json --text >> /tmp/kline_source_granularity_report.log 2>&1
```

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
- `signal_engine_v4.py` refreshes `engine_feature_runs.updated_at` whenever it upserts or finalizes the same `signal_v4_YYYYMMDD` run. This timestamp is part of the reliability contract. `data_health_report.py` also reads `engine_feature_runs.quality.generated_at` and exposes `feature_run.latest.effective_generated_at`; if DB metadata lags but `quality.generated_at` proves a post-cutoff rerun, the report uses that effective timestamp and adds `feature_run_metadata_timestamp_lagged_quality_generated_at` as an explicit warning note. This avoids a false post-close rerun block while preserving visibility into metadata drift.
- `data_health_report.py` also emits `feature_run.remediation` with schema `signal_v4_daily_run_remediation_v1`. This is a read-only operator plan, not a repair action. It records the latest `signal_v4_*` run, whether the current time is after the cutoff, whether the latest run was generated before the cutoff, a safe preflight command, the manual post-close v4 command, and post-run verification commands.
- If `feature_run.remediation.required_action=run_signal_engine_v4_post_close_under_operator_control`, Hermes/operator should first run `/usr/bin/python3 /root/signal_engine_v4.py --preflight --json`. Only if that preflight is acceptable should an operator explicitly run `/usr/bin/python3 /root/signal_engine_v4.py`, then rerun data health, system health, and readiness refresh. No readiness report, packet builder, cron audit, or refresh helper runs this write command automatically.
- `data_health_report.py` now distinguishes ordinary stale daily K-lines from `minute_fresh_daily_stale_symbols`: active symbols whose minute K-lines have reached the market latest date while their daily `interval='day'` rows are still behind. This is a read-only diagnostic for a daily refresh/aggregation gap. It does not use minute bars to validate daily signals or forward outcomes, because the outcome engine intentionally evaluates 1D/3D/5D evidence only on completed daily bars.
- Hermes can integrate this without migration by reading `markets.*.coverage.minute_fresh_daily_stale_count` and `sample_minute_fresh_daily_stale_symbols` when present. If the count is non-zero, Hermes should explain that minute collection is alive but the daily K-line refresh path is incomplete, keep execution blocked through the existing data-health/readiness gates, and ask an operator to repair or rerun the daily K-line refresh before trusting same-symbol daily signal/outcome evidence.
- `data_health_report.py` also emits `daily_gap_remediation` with schema `kline_daily_gap_remediation_v1`. This points Hermes/operator at the safe dry-run repair command and the hash-confirmed apply template. It is not a repair action; `write_command_requires_operator=true`, `submits_orders=false`, and `changes_crontab=false`.
- `data_health_report.py` emits `markets.*.source_quality` with schema `kline_source_quality_v1`. This summarizes each market's latest daily and latest minute K-line `data_source` values, missing source coverage, latest rows written by repair tools, and sample daily/minute source-family mismatches. `tencent`, `tencent_hk`, `tencent_us`, and `tencent_min` are normalized to the same Tencent family so normal day/min naming differences do not create false conflicts. Hermes should treat missing `data_source` as weak provenance, and should explicitly mention any `repair_daily_latest_count > 0` before trusting same-symbol evidence. Repair rows are acceptable only when they are hash-confirmed, backed up, and followed by data-health/readiness verification; they are not automatic proof that the broader data pipeline is healthy.

Canonical daily-bar contract:

- `interval='day'` now means one completed daily representative bar per `symbol + trade_date` for all v4/v5/Hermes read paths. If the raw table contains multiple rows for the same symbol and date, readers select the latest timestamp for that date with `DISTINCT ON (symbol, timestamp::date) ... ORDER BY timestamp DESC`.
- The canonical read is used by `signal_engine_v4.py`, `rt_signal_engine_v5.py`, `rt_signal_outcome_report.py`, `market_context_report.py`, `universe_rank_report.py`, `universe_hygiene_report.py`, `portfolio_report.py`, `sim_position_reconcile.py`, `system_health_check.py`, and legacy `generate_signals.py`.
- `data_health_report.py` still counts the raw duplicate daily rows. Any duplicate `interval='day'` rows for the same symbol/date produce `duplicate_daily_symbol_dates`, set the market status to `FAIL`, and emit `markets.*.integrity.duplicate_daily_symbol_date_count`, per-date counts, and examples. This is intentional: canonical reads reduce downstream blast radius, but duplicated daily rows prove the ingestion layer is still polluted.
- `kline_batch.py` normalizes future daily writes to a date-shaped timestamp before building SQL inserts. This does not repair existing duplicated rows by itself; operators must use a reviewed, backed-up, hash-confirmed repair path when cleaning live history.
- Hermes should treat `duplicate_daily_symbol_dates` the same way as invalid OHLC: do not approve or reduce new trades, keep execution readiness blocked, and ask an operator to clean or rebuild the affected daily rows before trusting daily signals, outcome evidence, or portfolio valuation baselines.

Intraday extension policy:

- Finer data is useful, but it must be a separate intraday layer, not additional rows masquerading as `interval='day'`.
- Recommended intervals: `60m` or `30m` for market evolution/regime transitions, `15m` for intermediate pullback/reversal confirmation, `5m` for entry/exit timing, and `1m` only for execution-quality diagnostics or very short-lived alerts.
- Daily bars remain the authority for full-day v4 signals, 1D/3D/5D forward outcome evidence, readiness, and portfolio valuation fallback. Intraday bars can add confirmation, timing, and same-day path evidence, but they must not overwrite daily conclusions.
- Hermes should receive a compact intraday digest rather than raw bars: latest session trend, VWAP/MA position, intraday high/low path, volume-vs-expected, stop-first/target-first evidence when observable, and whether intraday data agrees or conflicts with the daily signal.
- No-loss rollout: first keep the existing daily gates unchanged, add a dry-run intraday producer plan plus a read-only intraday digest, embed the digest additively into `hermes_review_packet.py`, shadow-review for at least several HK and US sessions, then consider using intraday confirmation as a stricter gate. Do not use intraday data to relax data-health, strategy-evidence, or execution-readiness blocks.

`scripts/intraday_kline_batch.py` is the dry-run-first producer plan for current-session minute rows. It reads the v5 watchlist or explicit `--symbol` values, fetches Tencent `minute/query` snapshots, validates timestamp/price/volume/OHLC shape, and writes `/tmp/intraday_kline_batch.json` with schema `intraday_kline_batch_report_v1`. The report contains `actions[]`, `unresolved[]`, `plan_hash`, `source.provider_contract=unofficial_public_web_endpoint_unversioned_best_effort`, and an `apply_contract`.

Important producer limits:

- default mode is dry-run and does not write DB;
- DB writes require `--apply --confirm-plan-hash <plan_hash>`;
- apply upserts only planned `klines.interval='min'` symbol/timestamp rows and backs up existing target minute rows first;
- when the relevant HK/US market is closed, weekend, pre-open, or otherwise outside a regular session, the producer skips fetch/apply planning for those symbols and records the skipped count in `/tmp/intraday_kline_batch.json`;
- if the DB schema has `klines.source_granularity`, apply also persists provenance such as `minute_snapshot_price`; older schemas remain compatible, but missing provenance is treated downstream as unverified minute-path fidelity;
- it never writes `interval='day'`, repairs daily K-lines, submits orders, changes alerts, changes strategy, changes watchlists, or edits crontab;
- Tencent `minute/query` returns one price point per minute, not independently observed minute OHLC high/low. The tool stores conservative `open=high=low=close=price`, so Hermes should use it for freshness, trend, volume progression, and rough path context, not for precise intra-minute execution reconstruction;
- sparse US rows from the public endpoint should be treated as execution-quality context only until a stronger vendor/feed adapter is added.

The session skip is intentional. Public minute endpoints can return `HHMM` rows without a full exchange date; stamping those rows onto a Saturday, holiday, or pre-open current date would create false current-session evidence. A skipped producer report is therefore not a trade blocker by itself, but Hermes must not treat it as live intraday confirmation. Wait for the next regular session, or use the last clean `intraday_context_report.py` output only as last-session context.

Manual apply flow after reviewing `/tmp/intraday_kline_batch.json`:

```bash
/usr/bin/python3 /root/intraday_kline_batch.py --output /tmp/intraday_kline_batch_apply.json --apply --confirm-plan-hash <plan_hash> --text
/usr/bin/python3 /root/intraday_context_report.py --output /tmp/intraday_context_report.json --text
/usr/bin/python3 /root/source_reliability_report.py --output /tmp/source_reliability_report.json --text
/usr/bin/python3 /root/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text
```

`scripts/intraday_context_report.py` is the read-only intraday digest layer. It reads already stored `klines.interval='min'` rows for the v5 watchlist and writes `/tmp/intraday_context_report.json` with schema `intraday_context_report_v1`.

It summarizes:

- market-session state (`intraday_market_session_v1`) for HK and US, including `phase`, local time, regular session windows, and whether a configured holiday/half-day override was applied;
- `granularity_policy` (`intraday_granularity_usage_policy_v1`), a read-only contract that tells Hermes how each intraday timeframe may be used without promoting it into an execution authority;
- per-symbol latest minute timestamp, staleness, session return/range, latest price, and source tags;
- latest rolling 5-minute, 15-minute, 30-minute, and 60-minute OHLCV windows, momentum labels, volume expansion/contraction state, row counts, expected minute counts, coverage ratios, and `coverage_status=OK|LIMITED|MISSING`;
- `rolling_windows` keyed by `5m`, `15m`, `30m`, and `60m`, so Hermes can distinguish mature intraday confirmation from partial early-session or gap-limited windows;
- `multi_timeframe_confirmation` with session, latest rolling 5-minute, 15-minute, and latest rolling 60-minute direction votes, dominant direction, alignment, and explicit timeframe contradictions;
- `quality` with bad timestamp, invalid OHLC, duplicate minute timestamp, missing source, large minute-gap, and insufficient 5m/60m aggregation counts;
- top-level `summary.quality_degraded_symbol_count`, `large_gap_symbol_count`, `invalid_ohlc_symbol_count`, `bad_timestamp_symbol_count`, and `duplicate_timestamp_symbol_count`;
- market-level intraday breadth by session-up/session-down percentage;
- Hermes notes such as `intraday_session_down_against_new_buy_review`, `intraday_session_up_supports_buy_review`, `intraday_multi_timeframe_bearish_challenges_buy_review`, `intraday_multi_timeframe_bullish_challenges_sell_review`, `intraday_timeframes_conflicting_requires_disclosure`, `intraday_15m_window_coverage_limited_requires_disclosure`, `intraday_30m_window_coverage_limited_requires_disclosure`, `intraday_60m_window_coverage_limited_requires_disclosure`, `intraday_context_quality_degraded_requires_disclosure`, `intraday_market_not_open_requires_session_context`, and `intraday_context_stale_for_symbol`.

The rolling-window fields are additive and read-only. They do not require a DB migration, do not change the `klines.interval='min'` write path, and do not alter v5 signal generation, order intake, simulation trading, crontab, or execution readiness. Hermes should treat `intraday_5m_window_coverage_limited_requires_disclosure`, `intraday_15m_window_coverage_limited_requires_disclosure`, `intraday_30m_window_coverage_limited_requires_disclosure`, and `intraday_60m_window_coverage_limited_requires_disclosure` as evidence limits: a partial 15m/30m/60m window can still provide same-session color, but it must not be described as full timeframe confirmation or used to increase confidence without explicit disclosure.

Granularity policy:

- `60m` is intraday regime confirmation/challenge. It may confirm or challenge the daily signal direction, identify same-session regime shifts, and affect Hermes confidence or sizing advice. It must not become a standalone BUY/SELL source or override execution-readiness or daily strategy-evidence gates.
- `30m` is intermediate confirmation/reversal checking. It may confirm 60m alignment or flag a reversal against the daily signal. It must not raise confidence when coverage is limited.
- `15m` is trade-timing confirmation. It may confirm or challenge latest momentum and guide timing notes. It must not replace news, macro, fundamentals, source-reliability, or event review.
- `5m` is a near-term timing/noise filter. It may flag immediate contradiction, chasing risk, or unclear short-term context. It must not raise confidence without 15m or 60m support.
- `1m` is execution-quality and path diagnostics. It may help resolve stop/target ordering only when rows have trusted full-OHLCV provenance, measure entry quality, and support postmortem learning. It must not be used as core alpha, confidence support from public snapshot rows, or daily K-line repair/replacement.

This policy is embedded in `/tmp/intraday_context_report.json` and copied into each matching `review_items[].context_digest.intraday_context.granularity_policy`. Hermes should cite it when explaining how finer-grained data affected a judgment. The policy is intentionally conservative: minute/hour data can make the review more market-aware, but daily forward outcomes, data health, execution readiness, source reliability, simulation performance, and Hermes judgment audits remain the controlling gates.

Session-aware status:

- `status=OK` means the relevant market is in a regular session and minute coverage is fresh enough for same-session review.
- `status=CLOSED` means minute rows exist but the market is not in a regular session, for example after close, weekend, lunch break, or pre-open. Hermes may use the rows only as last-session context and must not treat them as live intraday confirmation.
- `status=STALE` means the relevant market is in a regular session and the latest minute row is older than the configured staleness window.
- `status=MISSING` means watchlist symbols have no usable minute rows.
- If the market is closed and a watchlist symbol has no same-session rows, `intraday_context_report.py` classifies the symbol as `CLOSED` rather than `MISSING`. This distinguishes "no live session should be fetched now" from "regular-session minute coverage is broken."
- By default, the session model uses weekday regular-hours approximation. Operators can add `/root/intraday_market_sessions.json` or pass `--market-session-overrides-file <path>` to cover exchange holidays, half-days, or ad-hoc closures. When an override is applied, `holiday_calendar_applied=true`, `override_applied=true`, and `override_reason` explain the source. Without a matching override, `holiday_calendar_applied=false`, so Hermes should still be cautious around official holidays and half-days.

Example market-session override file:

```json
{
  "markets": {
    "HK": {
      "closed_dates": {
        "2026-07-01": "hkex_public_holiday"
      },
      "half_days": {
        "2026-12-24": {
          "reason": "hkex_half_day",
          "session_windows": [{"open": "09:30", "close": "12:00"}]
        }
      }
    },
    "US": {
      "closed_dates": {
        "2026-07-03": "nyse_observed_holiday"
      },
      "session_overrides": {
        "2026-11-27": {
          "reason": "nyse_early_close",
          "session_windows": [{"open": "09:30", "close": "13:00"}]
        }
      }
    }
  }
}
```

This file is read-only context. Updating it does not submit orders, edit strategy, change watchlists, repair K-lines, or write minute rows. After changing it, rerun `intraday_context_report.py`, `source_reliability_report.py`, `execution_readiness_report.py`, and `hermes_review_packet.py`.

Validate the override file before trusting it:

```bash
/usr/bin/python3 /root/intraday_market_session_overrides_report.py --output /tmp/intraday_market_session_overrides_report.json --text
```

The output schema is `intraday_market_session_overrides_report_v1`. `status=OK` means the operator-maintained HK/US override file is readable, syntactically valid, and has enough future override coverage for the configured horizon. `status=WARN` means the file is missing, incomplete, or has weak future coverage; Hermes should treat exchange-calendar awareness as partial. `status=FAIL` means invalid dates or invalid session windows were found and the intraday session model should not be treated as reliable for holiday/half-day interpretation. The repo includes `config/intraday_market_sessions.example.json` as a template; copy and edit it on the server after reviewing official HKEX/NYSE calendars.

Hermes integration is additive:

- `hermes_review_packet.py` embeds `/tmp/intraday_kline_batch.json` as top-level `intraday_kline_batch` so Hermes can see whether minute collection is only a dry-run plan, has unresolved provider/symbol coverage, or has already been hash-applied.
- `hermes_review_packet.py` embeds the full report as top-level `intraday_context`.
- `hermes_review_packet.py` embeds `/tmp/intraday_market_session_overrides_report.json` as top-level `intraday_market_session_overrides`. Hermes should read this before relying on open/closed, lunch-break, holiday, or half-day interpretation; `WARN` or `FAIL` means exchange-calendar awareness is partial and the minute context must not be described as exchange-calendar complete.
- Each trade `review_items[].context_digest` now includes `intraday_minute_producer`, a compact copy of the producer status, mode, `plan_hash`, provider contract, pending apply counts, unresolved count, sparse-US count, invalid-source-row count, and notes. If the producer is `ACTIONABLE`, `PARTIAL`, `UNRESOLVED`, missing, uses the unofficial public Tencent endpoint, is still dry-run/default, has unresolved symbols, sparse US rows, invalid source rows, or has a pending hash-confirmed apply, the item receives `intraday_minute_producer_limit_requires_acknowledgement`.
- Each `review_items[].context_digest` includes `intraday_context` for the reviewed symbol when available.
- Each matching `intraday_context` digest includes `granularity_policy`, so Hermes can distinguish `60m/30m/15m/5m` confirmation/timing evidence from `1m` execution/path diagnostics and avoid treating finer data as automatic alpha.
- Each `review_items[].context_digest` also includes `intraday_signal_evidence`, a compact read-only classification of the same 5m/15m/30m/60m/session evidence. It exposes `alignment`, `support_codes`, `challenge_codes`, `conflict_codes`, `quality_codes`, `limit_codes`, and `requires_judgment_acknowledgement` so Hermes can answer the exact intraday issue instead of writing a generic minute-context note.
- Each `review_items[].context_digest` also includes an `intraday_market_session_overrides` digest for the reviewed market. `WARN`, `FAIL`, `MISSING`, or report warnings add `intraday_market_session_overrides_limit_requires_disclosure`, so Hermes must explain that holiday/half-day awareness is incomplete before using minute evidence.
- Missing or stale intraday context adds `intraday_context_missing_or_stale_requires_disclosure` to `required_judgment_attention`.
- A BUY whose same-symbol intraday session is materially down adds `intraday_context_challenges_buy_requires_discussion`.
- A SELL whose same-symbol intraday session is materially up adds `intraday_context_challenges_sell_requires_discussion`.
- A BUY with bearish-aligned session/5m/15m/30m/60m confirmation also adds `intraday_context_challenges_buy_requires_discussion`; a SELL with bullish-aligned confirmation similarly challenges the SELL review.
- If the latest 5m, latest 15m, latest 60m, and session directions conflict, the packet adds `intraday_context_timeframe_conflict_requires_disclosure`. Hermes should explain whether it treats the conflict as a pullback, reversal warning, low-quality chop, or reason to hold/reduce confidence.
- If any latest rolling window has limited coverage, the packet adds `intraday_timeframe_coverage_limited_requires_disclosure` and `intraday_signal_evidence.limit_codes[]` such as `intraday_15m_window_coverage_limited_requires_disclosure`, `intraday_30m_window_coverage_limited_requires_disclosure`, or `intraday_60m_window_coverage_limited_requires_disclosure`. Hermes must not phrase those windows as full 15m/30m/60m confirmation unless the judgment explicitly acknowledges the coverage limit.
- If same-symbol minute quality is degraded, the packet adds `intraday_context_quality_degraded_requires_disclosure`. Hermes must explain whether the degraded minute evidence reduces confidence, prevents using stop-first/target-first path evidence, or requires holding/rejecting the signal. It must not treat degraded minute data as support for approval.
- If `intraday_minute_producer_limit_requires_acknowledgement` is present, Hermes must not treat the 5m/15m/30m/60m digest as fully reliable support unless the judgment sets `intraday_context_acknowledged=true` and `intraday_context_notes[]` explicitly discusses the producer/apply/source limit, for example that the producer was only a dry-run `ACTIONABLE` plan, public-fallback Tencent data, or not proof that DB `interval='min'` rows have been populated. This uses the existing intraday acknowledgement fields and adds no execution path, DB mutation, cron change, or automatic minute apply.
- When `intraday_signal_evidence.requires_judgment_acknowledgement=true`, Hermes must also set `intraday_signal_evidence_acknowledged=true`, copy `intraday_signal_evidence_alignment`, reference at least one `intraday_signal_evidence_codes[]` value, and explain the effect in `intraday_signal_evidence_notes[]`. This makes support/challenge/conflict/quality handling auditable while leaving daily K-lines as the return authority.
- If the relevant market is not in a regular session, the packet adds `intraday_market_not_open_requires_session_context`. Hermes must explain that minute evidence is last-session context only; it must not raise confidence as though the system had current live intraday confirmation.

Example read-only generation:

```bash
/usr/bin/python3 /root/intraday_kline_batch.py --output /tmp/intraday_kline_batch.json --text
/usr/bin/python3 /root/intraday_context_report.py --output /tmp/intraday_context_report.json --text
/usr/bin/python3 /root/intraday_timeframe_quality_report.py --output /tmp/intraday_timeframe_quality_report.json --text
/usr/bin/python3 /root/intraday_market_session_overrides_report.py --output /tmp/intraday_market_session_overrides_report.json --text
```

Suggested read-only cron after deployment review:

```bash
*/5 * * * * /usr/bin/python3 /root/intraday_kline_batch.py --output /tmp/intraday_kline_batch.json --text >> /tmp/intraday_kline_batch.log 2>&1
*/5 * * * * /usr/bin/python3 /root/intraday_context_report.py --output /tmp/intraday_context_report.json --text >> /tmp/intraday_context_report.log 2>&1
*/5 * * * * /usr/bin/python3 /root/intraday_timeframe_quality_report.py --output /tmp/intraday_timeframe_quality_report.json --text >> /tmp/intraday_timeframe_quality_report.log 2>&1
*/30 * * * * /usr/bin/python3 /root/intraday_market_session_overrides_report.py --output /tmp/intraday_market_session_overrides_report.json --text >> /tmp/intraday_market_session_overrides_report.log 2>&1
```

The cron producer line is still dry-run; it makes the current plan visible but does not populate DB minute rows. The context report does not submit orders, change watchlists, change strategy, repair K-lines, or modify crontab. It is not a signal generator; it is a timing and contradiction layer for Hermes review.

`scripts/intraday_timeframe_quality_report.py` is the compact read-only quality gate for 5m/15m/30m/60m evidence. It reads only `/tmp/intraday_context_report.json` and writes `/tmp/intraday_timeframe_quality_report.json` with schema `intraday_timeframe_quality_report_v1`. It does not query the DB, fetch new minute rows, write DB rows, submit orders, change strategy, apply cron, or repair data. The report summarizes per-market and per-symbol timeframe coverage, limited/missing windows, multi-timeframe conflicts, source-granularity gaps, low-fidelity minute sources, snapshot-like minute rows, stale symbols, and closed-session context.

The timeframe-quality gate does not trust `coverage_status=OK` by itself. For each 5m/15m/30m/60m window, it checks `row_count` against `expected_minute_count` or the timeframe length; underfilled windows are downgraded to `LIMITED`, and empty windows are `MISSING`. This keeps sparse minute/hour evidence from being presented to Hermes as complete confirmation.

Hermes packet integration is lossless: `hermes_review_packet.py` embeds the report as top-level `intraday_timeframe_quality`. Existing jobs can ignore this section. Hermes should use it to cap confidence and explain why 5m/15m/30m/60m evidence is only advisory when coverage is partial or source fidelity is weak. It must not use a clean or degraded timeframe-quality report to relax daily data-health, source-reliability, simulation-performance, strategy-evidence, or execution-readiness gates.

Readiness/source-quality integration is also additive:

- `source_reliability_report.py` tracks `/tmp/intraday_kline_batch.json` as `intraday_kline_batch`. `ACTIONABLE` means minute rows may be available but are not proof of DB coverage until an operator hash-confirms apply and reruns `intraday_context_report.py`; this degrades reliability with `intraday_kline_batch_apply_pending`. Public Tencent minute snapshots also degrade reliability with `intraday_kline_batch_unofficial_public_provider` until a broker/vendor feed adapter is available.
- `source_reliability_report.py` now tracks `/tmp/intraday_context_report.json` as `intraday_context`.
- `source_reliability_report.py` tracks `/tmp/intraday_timeframe_quality_report.json` as `intraday_timeframe_quality`. Limited/missing timeframes, conflicting 5m/15m/30m/60m windows, low-fidelity or snapshot-like minute rows, missing source granularity, stale symbols, and last-session-only closed-market context degrade source reliability.
- `source_reliability_report.py` also tracks `/tmp/intraday_market_session_overrides_report.json` as `intraday_market_session_overrides`.
- Stale or missing watchlist-symbol minute coverage degrades source reliability with `intraday_context_stale_symbols` or `intraday_context_missing_symbols`.
- Closed-market minute coverage degrades source reliability with `intraday_context_market_closed`; this is not a fetch failure, but it is also not live same-session evidence.
- Missing, unreadable, invalid, or unconfigured market-session override files degrade source reliability with `intraday_market_session_overrides_unavailable`. Hermes should then treat holiday/half-day awareness as incomplete and must not claim the intraday session model is exchange-calendar complete.
- WARN validation reports degrade source reliability with `intraday_market_session_overrides_incomplete`; FAIL validation reports fail source reliability with `intraday_market_session_overrides_invalid`.
- Degraded minute quality degrades source reliability with `intraday_context_quality_degraded_symbols`, `intraday_context_large_minute_gaps`, or `intraday_context_invalid_minute_rows`.
- `execution_readiness_report.py` exposes an `intraday_kline_batch` gate. Missing, stale, `ACTIONABLE`, `PARTIAL`, or `UNRESOLVED` producer plans are warning-only; unsafe producer safety contracts or `status=FAIL` block readiness. This keeps minute collection visible without confusing an unapplied dry-run plan with actual DB coverage.
- `execution_readiness_report.py` exposes an `intraday_context` gate. Fresh, complete, clean intraday context passes; closed-market, stale/missing symbol coverage, a missing report, or degraded minute quality is a warning; `status=FAIL` blocks readiness.
- The general `report_freshness` hard-block gate deliberately does not include intraday producer/context reports. Intraday is a confirmation/contradiction layer, so missing minute data must reduce confidence and require disclosure, but it must not be confused with failed daily data, failed strategy evidence, or unsafe execution plumbing.
- Hermes may cite the gate's `refresh_remediation.refresh_command` for an operator. It is read-only and does not submit orders, edit crontab, use `--apply`, or run execute mode.

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
- `system_health_check.py` carries the `feature_run.remediation.required_action` in the `data_health` check detail and check data, so the operational reason for a FAIL is visible without opening the full data-health JSON.
- `execution_readiness_report.py` carries `data_health.feature_run` and `data_health.feature_run.remediation` inside the `data_health` gate data. `data_health.status=FAIL` or a missing/invalid data-health report hard-blocks readiness. `data_health.status=WARN` is a readiness warning, not a hard block by itself; it still prevents `READY` while other warning gates exist and remains visible to Hermes, but it is no longer conflated with invalid OHLC or stale full-day signal failures.
- `hermes_review_packet.py` embeds `/tmp/data_health_report.json` as top-level `data_health`.
- `hermes_review_packet.py` also embeds `/tmp/kline_daily_gap_repair.json` as top-level `kline_daily_gap_repair` when available. This is additive packet context only: it does not change `review_items[].eligible_for_approval`, does not apply DB repairs, and does not make `data_health.status=FAIL` acceptable.
- Existing Hermes consumers can ignore `markets.*.source_quality`; newer prompts should read it as provenance context. `daily_latest_contains_repair_sources` and `daily_latest_data_source_missing` are WARN-level diagnostics that reduce confidence and require explanation, while hard blocking still flows through the existing top-level `data_health.status`.
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

`scripts/kline_daily_gap_repair.py` is the matching manual repair tool for `minute_fresh_daily_stale_symbols`. It finds active HK/US symbols whose minute K-lines are newer than their daily `interval='day'` rows, fetches Tencent daily rows, and prepares a hash-stamped upsert plan for only the missing daily dates:

```bash
/usr/bin/python3 /root/kline_daily_gap_repair.py --output /tmp/kline_daily_gap_repair.json --text
```

The output schema is `kline_daily_gap_repair_report_v1`. `status=OK` means no minute-fresh/daily-stale candidates need repair. `status=ACTIONABLE` means every detected gap has a hash-confirmed daily-row upsert plan. `status=PARTIAL` means some symbols have an actionable plan while others remain unresolved. `status=UNRESOLVED` means no safe repair rows were found and the issue is likely source coverage, symbol mapping, or active-universe quality. `status=WARN` means the planner saw warnings without a clean action/unresolved split. Hermes should surface `recommendations[]`, `summary`, `actions`, `unresolved`, and `apply_contract.manual_apply_command`, but must keep execution blocked until an operator applies the plan and reruns the post-apply verification commands.

Apply is explicit and hash-gated:

```bash
/usr/bin/python3 /root/kline_daily_gap_repair.py \
  --output /tmp/kline_daily_gap_repair_apply.json \
  --apply \
  --confirm-plan-hash <plan_hash> \
  --text
```

The tool backs up existing target rows under `/tmp/kline_daily_gap_backups/` before apply, upserts only planned `klines.interval='day'` rows, marks repaired rows with `data_source='tencent_day_repair'`, and never submits orders, changes crontab, changes watchlists, or changes strategy config. Operators can limit the plan with repeated `--symbol 00959 --symbol 01918` when repairing outcome-blocking names first. After any apply, rerun the report's `apply_contract.post_apply_verification_commands` before trusting the repaired daily outcome evidence.

If a symbol appears under `unresolved`, Hermes should not suggest applying the repair plan for that symbol. The unresolved row includes `source_attempts`, `earliest_source_date`, `latest_source_date`, and booleans such as `source_reaches_target_end` and `source_after_latest_daily`. Use these fields to distinguish a narrow fetch-window issue from a true source/symbol-mapping/universe issue. For example, if minute rows are fresh but the Tencent daily source does not reach `target_end_date`, the safer next step is symbol mapping or active-universe review, not forcing a daily row from minute data.

Hermes packet integration is lossless for existing jobs:

- Existing consumers can ignore top-level `kline_daily_gap_repair`.
- Hermes can use `summary`, `actions`, `unresolved`, and `apply_contract` to explain what an operator may repair manually and what remains a source/mapping issue.
- `apply_contract.does_not_submit_orders=true` and `does_not_change_crontab=true` must remain true before Hermes mentions the plan as a safe manual remediation candidate.
- A repairable action, such as one missing daily row for `01918`, still requires an operator to run the hash-confirmed `--apply` command and rerun data health/readiness afterwards.
- An unresolved symbol, such as a daily source that stops before the target date for `00959`, should be escalated as a data-source, symbol-mapping, or active-universe quality issue instead of patched from minute bars.

`scripts/kline_gap_source_diagnostic_report.py` is an additive read-only diagnostic for the unresolved side of the daily-gap plan. It consumes `/tmp/kline_daily_gap_repair.json`, `/tmp/universe_hygiene_report.json`, `/root/rt_signal_watchlist.json`, and `/tmp/portfolio_report.json` when present, then writes:

```bash
/tmp/kline_gap_source_diagnostic_report.json
```

Default command:

```bash
/usr/bin/python3 /root/kline_gap_source_diagnostic_report.py --output /tmp/kline_gap_source_diagnostic_report.json --text
```

The output schema is `kline_gap_source_diagnostic_report_v1`. It classifies each `kline_daily_gap_repair.unresolved[]` row into operator-review categories such as `active_universe_or_symbol_mapping_issue`, `provider_symbol_mapping_unavailable`, `provider_lag_or_partial_gap`, `provider_stopped_or_mapping_stale`, `source_rows_invalid`, or `provider_fetch_failed`. `status=OK` means no unresolved symbols remain. `status=REVIEW` or `ACTION_REQUIRED` means unresolved symbols need source, mapping, provider, active-universe, or exposure review before same-symbol outcome evidence is trusted. `status=WARN` means one of the upstream diagnostic reports was missing or incomplete.

Each `classifications[]` row now includes `exposure` with schema `unresolved_daily_gap_exposure_v1`:

- `in_current_v5_watchlist` and `watchlist_markets` show whether the unresolved symbol is still part of the active v5 watchlist;
- `has_open_position`, `positions[]`, and `trade_ledger_positions[]` show whether user/simulation holdings or simulation trade-ledger open positions still reference the symbol;
- `deactivation_blockers[]` contains `current_v5_watchlist_member`, `open_position_in_positions_table`, or `open_position_in_simulation_trade_ledger` when applicable;
- `safe_to_deactivate_without_manual_review=false` is always explicit. A clean exposure check is not an automatic deactivate permission; it only means the report found no current watchlist or open-position blocker, and operators must still review symbol mapping/refetch evidence first.

Hermes can integrate this without changing existing jobs by reading `summary.current_v5_watchlist_exposed_count`, `summary.open_position_exposed_count`, `sample_current_v5_watchlist_exposed_symbols`, and each `classifications[].exposure`. If a symbol is exposed through the watchlist, Hermes should ask for watchlist review before universe deactivation. If it has an open position, Hermes must not recommend deactivation until the position review path has handled the holding. This report still does not apply DB repairs, deactivate stocks, edit watchlists, submit orders, mutate simulation state, or exclude outcome evidence.

`hermes_review_packet.py` also maps the same report into each trade `review_items[].context_digest.daily_gap_source_diagnostic`. When the reviewed signal's symbol appears in `classifications[]`, the digest has `matched=true`, includes the category/recommended action/exposure summary, adds `signal_symbol_unresolved_daily_gap_requires_rejection_or_hold` to `required_judgment_attention`, and marks the review item `eligible_for_approval=false` with blocking reasons such as `daily_gap_source_unresolved_symbol`, `daily_gap_source:active_universe_or_symbol_mapping_issue`, and `daily_gap_source:current_v5_watchlist_member`. This is still packet-only review gating: it does not stop v5 alert generation, edit the alert queue, submit orders, mutate simulation state, repair K-lines, or change watchlists. It prevents Hermes from approving a signal whose own symbol is already known to have unresolved daily K-line source/mapping/universe evidence.

Safety contract:

- `source.read_only=true`
- `source.submits_orders=false`
- `source.applies_kline_repairs=false`
- `source.changes_watchlists=false`
- `source.changes_stock_universe=false`
- `source.auto_excludes_from_evidence=false`

Hermes should use `summary.category_counts`, `summary.confidence_counts`, `classifications[]`, and `recommendations[]` to explain why an unresolved symbol is not safe to repair automatically. It must not treat this diagnostic as permission to apply K-line repairs, exclude symbols from evidence, deactivate stocks, change watchlists, or approve trades. `execution_readiness_report.py` treats `REVIEW`, `ACTION_REQUIRED`, `WARN`, and missing diagnostics as warning gates; an unsafe safety contract hard-blocks readiness.

Recommended read-only cron:

```bash
15,45 * * * * /usr/bin/python3 /root/kline_gap_source_diagnostic_report.py --output /tmp/kline_gap_source_diagnostic_report.json --text >> /tmp/kline_gap_source_diagnostic_report.log 2>&1
```

`scripts/kline_gap_alternate_provider_probe.py` is a separate read-only probe for unresolved daily-gap rows. It compares the primary Tencent daily source date from `/tmp/kline_daily_gap_repair.json` with Yahoo daily chart coverage and writes:

```bash
/tmp/kline_gap_alternate_provider_probe.json
```

Default command:

```bash
/usr/bin/python3 /root/kline_gap_alternate_provider_probe.py --output /tmp/kline_gap_alternate_provider_probe.json --text
```

The output schema is `kline_gap_alternate_provider_probe_v1`. It classifies unresolved symbols into categories such as `alternate_provider_has_current_daily_rows`, `providers_agree_symbol_stale_or_suspended`, `alternate_provider_partial_catchup`, `alternate_provider_no_daily_rows`, and `alternate_provider_fetch_failed`.

Safety contract:

- `source.read_only=true`
- `source.submits_orders=false`
- `source.changes_crontab=false`
- `source.applies_kline_repairs=false`
- `source.changes_watchlists=false`
- `source.changes_stock_universe=false`
- `source.auto_uses_alternate_provider_for_repairs=false`
- `source.auto_excludes_from_evidence=false`

Hermes packet integration is additive and lossless:

- `hermes_review_packet.py` embeds it as top-level `kline_gap_alternate_provider_probe`.
- `execution_readiness_report.py` exposes a `kline_gap_alternate_provider_probe` gate. `OK` passes; `REVIEW`, `ACTION_REQUIRED`, `WARN`, or `MISSING` warn; `FAIL` or an unsafe safety contract blocks.
- If Yahoo has current daily rows while Tencent stops at the stale date, Hermes should treat this as provider disagreement requiring manual row comparison before any hash-confirmed repair. It must not use Yahoo rows as automatic outcome evidence.
- If both providers stop at the same stale date, Hermes should prioritize listing-status, suspension, symbol-mapping, or manual deactivation review before trusting the symbol.
- Existing jobs can ignore this field; no existing packet field is removed or reinterpreted.

Recommended read-only cron:

```bash
20,50 * * * * /usr/bin/python3 /root/kline_gap_alternate_provider_probe.py --output /tmp/kline_gap_alternate_provider_probe.json --text >> /tmp/kline_gap_alternate_provider_probe.log 2>&1
```

`scripts/kline_gap_alternate_provider_repair_plan.py` is a stricter read-only quality gate for the alternate-provider rows. It re-fetches the unresolved symbols, extracts candidate gap rows, computes OHLC validity, zero-volume ratio, and flat-OHLC ratio, and writes:

```bash
/tmp/kline_gap_alternate_provider_repair_plan.json
```

Default command:

```bash
/usr/bin/python3 /root/kline_gap_alternate_provider_repair_plan.py --output /tmp/kline_gap_alternate_provider_repair_plan.json --text
```

The output schema is `kline_gap_alternate_provider_repair_plan_v1`. A symbol can be:

- `manual_repair_candidate_after_operator_comparison` when rows pass the quality gate;
- `review_only_quality_not_sufficient_for_repair_plan` when rows exist but are dominated by zero-volume or flat OHLC bars;
- `blocked_invalid_alternate_rows` when alternate rows fail OHLC integrity.

Safety contract:

- `source.read_only=true`
- `source.submits_orders=false`
- `source.applies_kline_repairs=false`
- `source.auto_applies_repairs=false`
- `source.auto_uses_alternate_provider_for_repairs=false`
- `source.auto_excludes_from_evidence=false`
- `operator_contract.manual_review_required=true`
- `operator_contract.manual_apply_command=null`

This plan deliberately has no apply command. It is meant to tell Hermes/operator whether alternate-provider rows are even worth comparing against exchange or broker history. A separate hash-confirmed DB repair tool may be created or run only after an operator approves the exact rows. Zero-volume or flat-OHLC rows must not be used for repair or outcome evidence unless independently confirmed as legitimate exchange bars.

Recommended read-only cron:

```bash
25,55 * * * * /usr/bin/python3 /root/kline_gap_alternate_provider_repair_plan.py --output /tmp/kline_gap_alternate_provider_repair_plan.json --text >> /tmp/kline_gap_alternate_provider_repair_plan.log 2>&1
```

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

Legacy standalone simulation trader gate:

- `quantmind_sim_trader.py` now defaults to disabled and requires `QM_LEGACY_SIM_TRADER_ENABLE=1`.
- It reads API credentials only from environment variables and does not carry a source-code credential fallback.
- The portfolio id is configurable through `QM_LEGACY_SIM_PORTFOLIO_ID`, falling back to `QM_SIM_PORTFOLIO_ID` and then portfolio `8`.
- This does not change the v5 `notify`, `alert-dry-run`, or `alert-sim` path. It only prevents the old DB-signal compatibility trader from creating simulation orders outside the v5/Hermes/intake lineage by accident.

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
- `hermes_review_packet.py` also embeds a top-level `stock_universe_hygiene_promotion_plan` built from the already-loaded hygiene report. This is an in-memory dry-run plan only: it does not query the database, does not apply stock changes, does not change watchlists, does not restart services, and does not submit orders.
- The embedded promotion plan makes Hermes/operator review actionable by surfacing `operator_review_plan.items`, `pre_apply_checklist`, and dry-run/apply command templates for stale ordinary stocks that need a refetch-or-symbol-mapping decision before any manual deactivation.
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

Hermes packet integration:

- `stock_universe_hygiene_promotion_plan.schema=stock_universe_hygiene_promotion_report_v1`.
- `mode=dry-run`, `applied=false`, `selected_count=0` unless an operator explicitly runs the promotion CLI outside the packet.
- `operator_review_plan.status=operator_review_required` when high-priority stale ordinary stocks need manual review before deactivation.
- `safety.read_only_payload_build=true`, `safety.queries_database=false`, and `safety.does_not_change_stock_universe=true` for the embedded plan.
- This is additive context only. It does not relax `execution_readiness`, `rt_order_intake.py`, strategy evidence, data health, or simulation-performance gates.

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

If the quote timestamp is missing or cannot be parsed, v5 skips the volume anomaly calculation and the `full_score` volume factor for that tick. It does not fall back to the server clock, because server-local time can be wrong for US overnight sessions or stale quote payloads.

v5 also requires quote volume units to be comparable with daily K-line volume before applying the ratio. US realtime volume and unlabelled internal quotes are treated as shares. Tencent HK realtime quote volume is marked `volume_unit=board_lot`; if the quote does not include a trusted `lot_size`/`board_lot_size`, v5 skips HK volume anomaly and volume-score contribution for that tick. If a future provider or adapter supplies `volume_unit=board_lot` plus `lot_size`, v5 converts it to shares first. This avoids systematic HK volume-ratio distortion while keeping the same alert schema and Hermes contract.

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
- watchlist symbols are market-validated before scanning: HK accepts five-digit stock codes, and US accepts common uppercase ticker forms such as `AAPL` or `BRK.B`; rejected symbols are omitted and recorded in runtime warnings;
- the same symbol contract is enforced again when v5 loads daily history from `klines`, and the history loader normalizes `days` to a positive integer before building the read-only query;
- daily history rows with non-finite, non-positive, negative-volume, or geometrically invalid OHLCV values are skipped before they can affect RSI, moving averages, MACD, ATR, or trigger checks;
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

The current default config is intentionally stricter than the original hard-coded behavior:

- `BUY` confirmed when `full_score >= 0.45`;
- `SELL` confirmed when `full_score <= -0.45`;
- `full_score` confirmation thresholds can be tightened but not loosened: global and per-trigger BUY `min_full_score` values below `0.45` are rejected, while SELL `max_full_score` values above `-0.45` are rejected. Values must also stay inside the engine score domain `[-1, 1]`; invalid global thresholds fall back to defaults and invalid per-trigger threshold overrides are ignored, so a config typo cannot make every directional trigger confirmed.
- volume anomaly ratio threshold `3.0`; strategy config values below `3.0` are rejected while stricter values above it are allowed, so a config typo or overly aggressive proposal cannot turn volume-anomaly WATCH rows into a low-threshold noise source;
- signal cooldown `1800` seconds;
- per-trigger `enabled` values are normalized from booleans or boolean-like strings, and invalid per-trigger `cooldown_seconds` values are ignored so the trigger inherits the global cooldown instead of a hard-coded fallback;
- ATR stop/take-profit multiples `2.0` and `3.0`;
- minimum directional risk/reward ratio `1.2`;
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

Cooldown is applied to the emitted alert type after this downgrade. For example, an unconfirmed `BUY:站上MA5` emitted as `WATCH` cools down the diagnostic WATCH row, but it does not block a later confirmed `BUY:站上MA5` if the full-score threshold is met before the normal cooldown expires.

For trigger overrides with `review_mode=shadow_only_pending_sample`, v5 also emits directional BUY/SELL triggers as `WATCH` even when the full-score threshold is confirmed. The alert keeps `candidate_signal_type`, `candidate_*` risk fields, `trigger_review_mode`, and `strategy_policy_shadow_only=true`, but `execution_candidate=false` and executable stop/take-profit fields stay empty. This lets Hermes and the learning reports continue collecting evidence without allowing a trigger that strategy review placed back into shadow mode to flow into execution review as a trade candidate.

For invalid directional risk geometry, missing/invalid ATR, or a candidate risk/reward ratio below `risk_model.min_rr_ratio`, v5 also emits the candidate as `WATCH` before it can enter trade review. `risk_geometry_valid=false` with `risk_geometry_reason` explains whether ATR was missing/invalid, entry/stop/take-profit was missing, non-positive, geometrically invalid, or `rr_ratio_below_minimum`; executable `stop_loss`, `take_profit`, and `rr_ratio` stay empty while the `candidate_*` fields and `min_rr_ratio` remain available for diagnostics. The candidate `rr_ratio` is computed from the rounded candidate prices, not merely from ATR multiples, and low-price symbols keep additional decimal precision, so rounding cannot overstate or flatten payoff quality. This keeps low-price, abnormal-ATR, or poor-payoff alerts visible without polluting the BUY/SELL execution-candidate queue.

`alert_quality_report.py` summarizes `by_strategy_config_source` and recommends restarting v5 with configured strategy metadata when scanned directional alerts are missing these fields. Older queue entries naturally show `missing` until new alerts are produced after restart.

Server rollout on 2026-06-12:

- `/root/rt_signal_strategy_config.json` was deployed from `config/rt_signal_strategy_config.json`;
- `rt_signal_engine_v5.service` now sets `RT_SIGNAL_STRATEGY_CONFIG_FILE=/root/rt_signal_strategy_config.json`;
- v5 was restarted under systemd and stayed active;
- startup log showed `strategy_config_id=8c5fa44224376503`, `strategy_config_source=file`, and `version=v5-compatible-default-20260612`;
- current repository config keeps the existing volume anomaly threshold, cooldown, and ATR stop/take-profit multiples, but tightens BUY/SELL confirmation thresholds to `0.45`/`-0.45` so standalone weak trigger evidence is emitted as diagnostic `WATCH` rather than confirmed directional flow. Deploy this by copying the updated reference config to `/root/rt_signal_strategy_config.json` and restarting v5; until then, an already-running server keeps the threshold values from its local config file.

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
- current `/tmp/execution_readiness_report.json`, `/tmp/source_reliability_report.json`, `/tmp/simulation_performance_report.json`, and `/tmp/strategy_learning_report.json` evidence clean enough for promotion.

Promotion evidence guards:

- execution readiness must be `READY` with `ready_for_execute=true`, except for the explicit remediation case where the only warning gate is `watchlist_proposal` and its `proposal_hash` matches the proposal being applied;
- source reliability must be `OK` or `PASS`;
- simulation performance must be `OK` or `PASS`;
- strategy learning must include `audit_pass_judgment_effect`; raw `judgment_effect` is diagnostic only;
- judgment-audit coverage in strategy learning must be available, not truncated, and have zero failed or missing judgment rows;
- audit-pass approved/reduced and rejected/held Hermes judgment-effect samples must each have at least five resolved rows;
- if strategy learning references a sizing-blocker remediation `watchlist_proposal_hash`, it must match the proposal being promoted.

On apply it backs up the target watchlist first and writes the proposed `rt_signal_watchlist_v1`; it does not restart `rt_signal_engine_v5.service`, does not install cron, and does not submit orders. Operators must restart the service manually only after reviewing the written file.

The promotion report includes `promotion_blockers`, compact `promotion_context`, `current_watchlist_id`, and `proposed_watchlist_id` using the same digest algorithm as `rt_signal_engine_v5.py`. After a manual apply and service restart, confirm new v5 alerts carry `watchlist_id=<proposed_watchlist_id>` before trusting watchlist-scoped alert quality, outcome, or strategy learning reports.

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

Hermes packet and readiness integration:

- `hermes_review_packet.py` reads `/tmp/rt_alert_event_store_report.json` into top-level `alert_event_store` when the file exists.
- Missing or dry-run event-store status does not block packet generation; it tells Hermes/operator that long-term alert history still depends on JSONL retention.
- `execution_readiness_report.py` also reads this report through the aggregate `event_store_durability` gate. `dry_run` is safe but only a warning, so it prevents `READY` until the operator either keeps the system in research mode or enables hash-confirmed audit-table persistence.
- Once apply mode is reviewed and enabled, the DB table becomes the durable audit trail for comparing alerts, judgments, and later outcome evidence.

Recommended staged cron:

```bash
*/5 * * * * /usr/bin/python3 /root/rt_alert_event_store.py --output /tmp/rt_alert_event_store_report.json --text >> /tmp/rt_alert_event_store.log 2>&1
```

Only after reviewing the emitted `schema_hash`, replace that dry-run line with the hash-confirmed `--apply` line. This is operational audit persistence only; it is not an execution path.

### Market context report

`scripts/market_context_report.py` is a read-only market regime input for Hermes. The primary regime classification remains backward-compatible and still uses the active HKEX/NASDAQ/NYSE stock pool itself as a breadth proxy. The report now also emits additive `native_index_context` per market when either `index_ohlcv_daily`/`index_daily` contains usable HK/US index series or `/tmp/market_index_context_inputs.json` contains a fresh read-only benchmark snapshot. When `/tmp/market_sentiment_report.json` is available, the report also adds a `cross_market` section per market using quantified index/ETF/VIX-style sentiment inputs from `market_sentiment_report.py`.

It reports by market:

- latest daily K-line date distribution;
- 20/50-day evaluable coverage;
- percentage of symbols above MA20/MA50;
- percentage of symbols up over 1 day;
- average and median 1/5/20-day returns;
- average and median 20-day daily volatility;
- latest v4 `BUY/HOLD/SELL` distribution;
- `risk_on`, `mixed`, or `risk_off` regime classification;
- `native_index_context` from local `index_ohlcv_daily`/`index_daily` rows or `/tmp/market_index_context_inputs.json`, with `status=OK`, `MISSING`, `INSUFFICIENT_HISTORY`, or `STALE`, and `alignment=confirms_breadth`, `conflicts_with_breadth`, `index_direction_without_breadth_confirmation`, `neutral_or_mixed`, or `incomplete`;
- `cross_market` confirmation from quantified sentiment/index/ETF/volatility inputs, with `alignment=confirms_breadth`, `conflicts_with_breadth`, `sentiment_direction_without_breadth_confirmation`, `neutral_or_mixed`, or `incomplete`;
- notes and recommendations for Hermes review.

Example:

```bash
/usr/bin/python3 /root/market_index_context_producer.py --output /tmp/market_index_context_inputs.json --text
/usr/bin/python3 /root/market_context_report.py --output /tmp/market_context_report.json --text
```

Use `--market-sentiment-file` to point at a non-default sentiment snapshot during manual review:

```bash
/usr/bin/python3 /root/market_context_report.py \
  --market-sentiment-file /tmp/market_sentiment_report.json \
  --output /tmp/market_context_report.json \
  --text
```

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/market_context_report.json` into `market_context` when the file exists.
- Missing market context does not block packet generation, but Hermes should treat it as reduced confidence.
- In `risk_off` regimes, Hermes should normally `reject`, `hold`, or `reduce` new BUY approvals unless the alert has a clearly documented exception and all execution gates still pass.
- If `markets.<market>.native_index_context.status` is not `OK`, Hermes must state that native index confirmation is unavailable or incomplete. It must not describe the primary `regime` as real index, macro, or broad-market evidence; it is still stock-pool breadth until native index rows are populated and fresh.
- If `markets.<market>.native_index_context.alignment=conflicts_with_breadth`, Hermes should explicitly discuss both the native index evidence and the stock-pool breadth evidence before supporting or rejecting a signal.
- If `markets.<market>.cross_market.alignment=conflicts_with_breadth`, Hermes should explicitly discuss the conflict between stock-pool breadth and real index/ETF/VIX-style sentiment before supporting or rejecting a signal. This does not override the execute-mode market-context gate; it gives Hermes better evidence for sizing, holding, or documenting a narrowly justified exception.
- The judgment audit enforces that discussion for `BUY` approvals/reductions. A generic `market_context_reviewed=true` checkbox is not enough when breadth and cross-market sentiment disagree.

No-loss integration contract:

- This is an additive schema extension under `market_context.markets.<market>.native_index_context`; existing readers of `regime`, `risk_level`, `breadth`, `returns`, `notes`, and `cross_market` do not need to change.
- The script remains read-only. It reads local DB tables and report JSON only; it does not submit orders, change strategy, write alert queues, repair data, or edit crontab.
- `market_index_context_producer.py` is also read-only. It writes only `/tmp/market_index_context_inputs.json` from public Yahoo chart snapshots for `^GSPC`, `^IXIC`, `SPY`, `QQQ`, `^HSI`, and `2800.HK`; it does not populate DB index tables.
- Existing execute-mode market gates still key off the established `risk_off` and weak-breadth notes. Native index context is evidence quality and review context, not an automatic order signal.
- `source_reliability_report.py` degrades `market_context` when native index context is missing/incomplete, conflicts with breadth, or is available only from public fallback snapshots. `execution_readiness_report.py` also warns when native index context is missing/incomplete, preventing `READY` while true index confirmation is absent.

Important limitation: the primary regime remains a stock-pool breadth proxy. `native_index_context` improves review evidence when a DB index feed or public fallback snapshot is available, but public Yahoo snapshots are not broker/vendor/official-grade data. `cross_market` improves the packet with quantified index/ETF/volatility context from `market_sentiment_report.py`. Neither replaces event risk, macro, earnings, liquidity, or trusted source checks.

### External market context

`scripts/external_market_context_report.py` is the read-only contract for giving Hermes current-event awareness without changing the v5 signal engine or enabling event-driven trading.

It reads external producer files:

```bash
/tmp/external_market_context_inputs.json
/tmp/external_market_context_inputs.jsonl
```

`scripts/external_market_context_producer.py` is the default low-dependency producer for this contract. It fetches public RSS/Atom headlines from Google News market searches, MarketWatch top stories, and CNBC top news, then overwrites `/tmp/external_market_context_inputs.json` with normalized `news`, `macro`, `capital_flow`, or `event` items. It can also read the local Info Hub HTTP bridge with `--include-infohub --infohub-url http://127.0.0.1:8899`.

This producer is only a bootstrap current-event layer. On the current server, Info Hub is a Flask/browser/RSS bridge exposing `/news`, `/macro`, `/finance`, `/search`, `/extract`, and `/kline`; it does not maintain a local structured news database under `/root/info-hub/data`. Items imported from that bridge are tagged as `provider=infohub_public_rss_bridge`, not as Wudao-grade or broker-grade structured context. Wudao MCP flash news, broker/event feeds, capital-flow snapshots, or official macro exporters should replace or augment the same input schema when available.

Expected producers can be Wudao MCP flash news, Info Hub macro summaries, capital-flow snapshots, broker feeds, or a future event-catalyst detector. The script itself does not submit orders, change strategy settings, write the v5 alert queue, or generate v5 BUY/SELL alerts. It normalizes producer output into `news`, `macro`, `capital_flow`, `event`, and `sentiment` items.

Trusted structured providers should use explicit `provider`, `source`, or `producer` names so source reliability can distinguish trusted feeds from public fallback bridges:

- `provider=wudao_mcp_flash_news` or `source=wudao_mcp_flash_news` for Wudao/CLS-style flash news;
- `provider=capital_flow_snapshot`, `source=northbound_flow_snapshot`, or `producer=broker_feed` for northbound/southbound/broker flow snapshots;
- `provider=official_macro_calendar` or `producer=official_macro` for official macro/calendar releases;
- `provider=broker_feed` for broker/event context exported by an authenticated broker data source.

Public RSS, Google News, CNBC top-news RSS, MarketWatch RSS, and the current Info Hub bridge remain useful context but are counted as fallback coverage. They should not clear `source_reliability` degradation by themselves.

Minimal JSON input example:

```json
{
  "items": [
    {
      "id": "wudao-20260612-001",
      "category": "news",
      "source": "wudao_mcp_flash_news",
      "provider": "wudao_mcp_flash_news",
      "producer": "wudao_mcp",
      "title": "Ceasefire headline boosts global risk appetite",
      "summary": "Dow futures rally, oil falls, risk assets bid.",
      "published_at": "2026-06-12T10:00:00",
      "sentiment": "positive",
      "impact_score": 0.86,
      "markets": ["US", "HK"],
      "symbols": [],
      "tags": ["geopolitics", "risk_appetite"]
    }
  ]
}
```

Default command:

```bash
/usr/bin/python3 /root/external_market_context_producer.py --output /tmp/external_market_context_inputs.json --text
/usr/bin/python3 /root/external_market_context_report.py --output /tmp/external_market_context_report.json --text
```

Info Hub bridge command, still writing the same no-loss contract:

```bash
/usr/bin/python3 /root/external_market_context_producer.py --include-infohub --infohub-url http://127.0.0.1:8899 --output /tmp/external_market_context_inputs.json --text
/usr/bin/python3 /root/external_market_context_report.py --output /tmp/external_market_context_report.json --text
```

Producer ingest helper:

```bash
/usr/bin/python3 /root/external_market_context_ingest.py \
  --item-json '{"id":"wudao-20260612-001","category":"news","source":"wudao_mcp_flash_news","provider":"wudao_mcp_flash_news","producer":"wudao_mcp","title":"Ceasefire headline boosts global risk appetite","published_at":"2026-06-12T10:00:00","sentiment":"positive","impact_score":0.86,"markets":["US","HK"],"tags":["geopolitics","risk_appetite"]}' \
  --text
```

`external_market_context_ingest.py` validates, de-duplicates, and appends accepted items to `/tmp/external_market_context_inputs.jsonl`. It only writes the external-context JSONL input file. It does not submit orders, change strategy, or write the v5 alert queue. Use `--dry-run` to validate producer payloads without writing, or `--input-file` / `--input-jsonl-file` when Wudao/Info Hub exports batches.

The output schema is `external_market_context_report_v1`. `status=MISSING` means no external context producer has written usable inputs, so Hermes must not claim news/macro awareness. `status=STALE` means external items exist but are older than the configured freshness window. `status=RISK` means fresh high-impact negative context exists and Hermes must write explicit risk notes before supporting exposure. `status=OK` means fresh context is available with no high-impact negative blocker. `status=FAIL` is reserved for hard parser/producer failures.

The report also separates freshness from source coverage. `summary.by_provider`, `summary.by_producer`, `summary.trusted_provider_item_count`, `summary.fallback_rss_item_count`, `summary.producer_fetch_failed_count`, `summary.macro_item_count`, `summary.capital_flow_item_count`, and `summary.watchlist_symbol_item_count` tell Hermes whether the context is only public fallback headlines or includes higher-quality structured sources. Public RSS and the current Info Hub bridge are useful context, but when `trusted_provider_item_count=0`, Hermes must not claim full event/macro/capital-flow awareness.

`hermes_review_packet.py` reads `/tmp/external_market_context_report.json` into top-level `external_market_context`. Hermes should use it alongside technical signals, portfolio risk, strategy evidence, and simulation performance. This is the fastest no-loss way to give Hermes the current-event layer: Wudao/Info Hub only need to write producer JSON/JSONL files; existing v5 alerts, dry-run intake, judgment audit, and execution gates remain unchanged.

`execution_readiness_report.py` also reads the report. Missing, stale, or risk status prevents `READY` and forces Hermes/operator review; parser `FAIL` hard-blocks readiness. This does not make external news an execution approval. `rt_order_intake.py` remains the authoritative execution gate.

Recommended read-only cron:

```bash
*/5 * * * * /usr/bin/python3 /root/external_market_context_producer.py --include-infohub --infohub-url http://127.0.0.1:8899 --output /tmp/external_market_context_inputs.json --text >> /tmp/external_market_context_producer.log 2>&1 && /usr/bin/python3 /root/external_market_context_report.py --output /tmp/external_market_context_report.json --text >> /tmp/external_market_context_report.log 2>&1
```

### Trusted source discovery

`scripts/trusted_source_discovery_report.py` is a read-only inventory of trusted context source wiring. It answers whether the server appears to have Wudao, Info Hub, broker, official macro, or fundamentals-vendor adapters configured or reachable. It is different from preflight:

- discovery checks adapter wiring, endpoint reachability, and input-file presence;
- preflight validates the actual external-context, sentiment, and fundamentals payload contents.

It writes only:

```bash
/tmp/trusted_source_discovery_report.json
```

The report never prints secret values. It reports environment variable names that are present, not their values, and its safety contract includes `read_only=true`, `submits_orders=false`, `changes_strategy=false`, `changes_alert_queue=false`, `changes_crontab=false`, `writes_ingest_files=false`, `repairs_data=false`, and `prints_secret_values=false`.

Default command:

```bash
/usr/bin/python3 /root/trusted_source_discovery_report.py --output /tmp/trusted_source_discovery_report.json --text
```

Status semantics:

- `OK`: the critical source capabilities have configured/reachable adapters ready for payload validation.
- `WARN`: at least one adapter exists, but one or more critical capabilities are missing or configured but unverified.
- `MISSING`: no trusted source adapter wiring was discovered.

Hermes should use discovery to ask the operator for the right missing adapter or payload export. Discovery does not prove data quality; a configured Wudao/broker/vendor adapter must still export JSON/JSONL and pass `trusted_source_preflight.py` before Hermes can cite it as trusted evidence.

Recommended read-only cron:

```bash
*/10 * * * * /usr/bin/python3 /root/trusted_source_discovery_report.py --output /tmp/trusted_source_discovery_report.json --text >> /tmp/trusted_source_discovery_report.log 2>&1
```

### Trusted source preflight

`scripts/trusted_source_preflight.py` is the read-only validator for structured source payloads before Hermes treats them as trusted evidence. It reads the existing external-context, market-sentiment, and fundamentals producer inputs:

```bash
/tmp/external_market_context_inputs.json
/tmp/external_market_context_inputs.jsonl
/tmp/market_sentiment_inputs.json
/tmp/market_sentiment_inputs.jsonl
/tmp/fundamentals_context_inputs.json
/tmp/fundamentals_context_inputs.jsonl
```

It writes only:

```bash
/tmp/trusted_source_preflight_report.json
```

The preflight does not append JSONL, edit strategy config, write the v5 alert queue, repair K-lines, edit crontab, or submit orders. Its safety contract is embedded under `source` with `read_only=true`, `submits_orders=false`, `changes_strategy=false`, `changes_alert_queue=false`, `changes_crontab=false`, `writes_ingest_files=false`, and `repairs_data=false`.

Status semantics:

- `OK`: payload shape is valid and minimum trusted structured coverage exists.
- `WARN`: payloads are readable but coverage is partial, stale, public fallback only, or missing key macro/capital-flow/volatility/fundamentals dimensions.
- `MISSING`: no usable external context, sentiment, or fundamentals payload is present yet.
- `FAIL`: schema or timestamp validation failed; operators should fix the payload before ingesting or citing it.

No-loss Wudao/broker integration workflow:

```bash
/usr/bin/python3 /root/trusted_source_preflight.py --output /tmp/trusted_source_preflight_report.json --text
/usr/bin/python3 /root/external_market_context_ingest.py --input-file <trusted_external_payload.json> --dry-run --text
/usr/bin/python3 /root/market_sentiment_ingest.py --input-file <trusted_sentiment_payload.json> --dry-run --text
/usr/bin/python3 /root/fundamentals_context_ingest.py --input-file <trusted_fundamentals_payload.json> --dry-run --text
```

Only after the preflight and ingest dry-run are clean should an operator run the append command for that payload. Appending external context, sentiment, or fundamentals still does not submit orders; it only updates the JSONL context inputs. `fundamentals_context_ingest.py` defaults to dry-run and requires `--append` before writing `/tmp/fundamentals_context_inputs.jsonl`. After append, refresh the downstream read-only reports:

```bash
/usr/bin/python3 /root/external_market_context_report.py --output /tmp/external_market_context_report.json --text
/usr/bin/python3 /root/market_sentiment_report.py --output /tmp/market_sentiment_report.json --text
/usr/bin/python3 /root/fundamentals_context_report.py --output /tmp/fundamentals_context_report.json --text
/usr/bin/python3 /root/trusted_source_preflight.py --output /tmp/trusted_source_preflight_report.json --text
/usr/bin/python3 /root/source_reliability_report.py --output /tmp/source_reliability_report.json --text
/usr/bin/python3 /root/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text
/usr/bin/python3 /root/hermes_review_packet.py --output /tmp/hermes_signal_review_packet.json --ephemeral-state
```

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/trusted_source_preflight_report.json` into top-level `trusted_source_preflight`.
- Hermes may read WARN/MISSING preflight reports as limitation context, but it must not claim trusted Wudao/broker/source coverage from public fallback payloads.
- `FAIL` means the payload should not be cited as evidence until repaired and revalidated.

Readiness and source reliability integration:

- `execution_readiness_report.py` reads `/tmp/trusted_source_preflight_report.json`; `FAIL` hard-blocks readiness, while `WARN`, `MISSING`, or stale status prevent `READY`.
- `source_reliability_report.py` includes the preflight as `trusted_source_preflight`; preflight WARN/MISSING degrades the source matrix and preflight FAIL fails it.

Recommended read-only cron:

```bash
*/10 * * * * /usr/bin/python3 /root/trusted_source_preflight.py --output /tmp/trusted_source_preflight_report.json --text >> /tmp/trusted_source_preflight.log 2>&1
```

### Event catalyst context

`scripts/event_catalyst_report.py` is the read-only bridge between external current-event inputs and the live v5 watchlist. It consumes:

```bash
/tmp/external_market_context_report.json
/root/rt_signal_watchlist.json
```

It writes:

```bash
/tmp/event_catalyst_report.json
```

The output schema is `event_catalyst_report_v1`. It filters fresh high-impact `news`, `macro`, `capital_flow`, and `event` items from the external context report, then keeps only items that match either:

- a watchlist symbol, such as `TSLA`;
- a watchlist market, such as `US` or `HK`.

Status semantics:

- `MISSING`: external context is missing or not in the expected schema.
- `STALE`: the upstream external context is stale.
- `RISK`: at least one fresh high-impact negative catalyst is linked to the watchlist.
- `OK`: catalyst scan completed and no negative watchlist-linked catalyst is present.
- `FAIL`: upstream external context failed, or a future parser failure is promoted to hard failure.

This report does not crawl the web, submit orders, change strategy settings, write `/tmp/rt_signal_alerts.jsonl`, or promote watchlists. It only helps Hermes answer: "Which current external events are relevant to the symbols or markets v5 is actually watching?"

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/event_catalyst_report.json` into top-level `event_catalysts`.
- Hermes must write explicit risk notes before supporting new exposure when `event_catalysts.status=RISK`.
- Missing/stale catalysts mean Hermes must state limited watchlist-event awareness.
- This does not approve execution; `rt_order_intake.py` remains the authoritative execute gate.

Readiness integration:

- `execution_readiness_report.py` reads `/tmp/event_catalyst_report.json`.
- `MISSING`, `STALE`, and `RISK` are warning gates that prevent `READY` but do not hard-block existing read-only jobs.
- `FAIL` is a blocking gate because the catalyst parser/upstream context is not trustworthy.

Recommended read-only cron:

```bash
*/5 * * * * /usr/bin/python3 /root/event_catalyst_report.py --output /tmp/event_catalyst_report.json --text >> /tmp/event_catalyst_report.log 2>&1
```

### Event catalyst review signals

`scripts/event_catalyst_signal_report.py` is the read-only event-driven signal layer. It consumes the watchlist-linked catalyst report plus the recent v5 alert queue:

```bash
/tmp/event_catalyst_report.json
/tmp/rt_signal_alerts.jsonl
```

It writes:

```bash
/tmp/event_catalyst_signal_report.json
```

The output schema is `event_catalyst_signal_report_v1`. It converts catalysts into `event_catalyst_signal_v1` review signals and links them to related v5 technical alerts by symbol or market. Positive and negative catalysts are split by related alert side, so one market event can produce a BUY-support/challenge review signal and a separate SELL-support/challenge review signal. This keeps Hermes from seeing BUY and SELL alerts mixed inside one approval challenge. Example review signal types:

- `SUPPORT_BUY_REVIEW`: positive symbol/market event aligns with a related BUY alert.
- `CHALLENGE_BUY_REVIEW`: negative symbol/market event contradicts a related BUY alert.
- `SUPPORT_SELL_REVIEW`: negative event aligns with a related SELL alert.
- `CHALLENGE_SELL_REVIEW`: positive event contradicts a related SELL alert.
- `POSITIVE_CATALYST_REVIEW`, `NEGATIVE_CATALYST_REVIEW`, `MIXED_CATALYST_REVIEW`, or `CONTEXT_CATALYST_REVIEW` when no directly related technical alert exists.

This is deliberately not an order signal:

- `source.submits_orders=false`;
- `source.writes_alert_queue=false`;
- every `signals[].execution_candidate=false`;
- every `signals[].eligible_for_order_intake=false`.

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/event_catalyst_signal_report.json` into top-level `event_catalyst_signals`.
- Hermes should use `related_v5_signal_ids[]` to support or challenge specific technical alerts.
- If the upstream event-catalyst report itself is `MISSING`, `STALE`, `RISK`, `FAIL`, or `INVALID`, each review item receives `event_catalyst_coverage_limit_requires_acknowledgement` in `context_digest.required_judgment_attention`; Hermes must not treat absence of `event_catalysts.candidates[]` as proof that no watchlist event risk exists.
- If the event-catalyst signal report is `MISSING`, `STALE`, `RISK`, `FAIL`, or `INVALID`, each review item receives `event_catalyst_signal_coverage_limit_requires_acknowledgement` in `context_digest.required_judgment_attention`. This is fail-visible context: alerts and packets still generate, but Hermes cannot use the absence of event-review signals as positive evidence unless it explicitly acknowledges the coverage limit in the judgment.
- A `CHALLENGE_BUY_REVIEW` signal must be structurally acknowledged through `event_catalyst_risk_acknowledged`, `event_catalyst_signal_ids[]`, and `event_catalyst_risk_notes[]` before Hermes approves or reduces a related BUY. Merely mentioning "news reviewed" is not enough for the audit.
- A `SUPPORT_BUY_REVIEW` signal used to support approve/reduce must be structurally acknowledged through `event_catalyst_support_acknowledged`, `event_catalyst_support_signal_ids[]`, and `event_catalyst_support_notes[]`. It is a support layer only and does not relax any readiness, data, technical, source, portfolio, or intake gate.
- This layer gives Hermes event awareness without changing v5 trigger generation or order intake.

Matching quality:

- by default, the report matches only the latest `strategy_config_id` + `watchlist_id` scope from the scanned v5 queue, so old strategy/watchlist alerts do not pollute current event review;
- related alerts must be within `EVENT_CATALYST_SIGNAL_ALERT_WINDOW_MINUTES`, default `240`, around the event `published_at`;
- each event is deduped and capped by `EVENT_CATALYST_SIGNAL_MAX_RELATED_ALERTS`, default `8`;
- `summary.related_v5_signal_count` counts unique related v5 signal ids after side splitting, not merely the number of event review-signal rows;
- `summary.alert_sample_scope` records the active scope and exclusion counts, and each `related_v5_alerts[]` row includes `event_delta_minutes` plus `relevance_reason`.

This is a no-loss integration for existing jobs: the current cron line still works because the new scope/window/limit behavior is defaulted inside `event_catalyst_signal_report.py`. Operators only need to add CLI flags such as `--sample-scope all`, `--alert-window-minutes <minutes>`, or `--max-related-alerts <n>` when deliberately changing the matching policy.

Readiness integration:

- `execution_readiness_report.py` reads `/tmp/event_catalyst_signal_report.json`.
- `MISSING`, `STALE`, or `RISK` prevent `READY` because event-driven review evidence is incomplete or actively contradicts new exposure.
- `FAIL` blocks readiness because the event-signal parser is not trustworthy.

Recommended read-only cron:

```bash
*/5 * * * * /usr/bin/python3 /root/event_catalyst_signal_report.py --output /tmp/event_catalyst_signal_report.json --text >> /tmp/event_catalyst_signal_report.log 2>&1
```

### Market sentiment context

`scripts/market_sentiment_report.py` is the read-only contract for quantified volatility, capital-flow, risk-appetite, and sentiment indicators. It exists so Hermes can reason about VIX-style fear gauges, northbound or ETF flows, macro risk appetite, funding pressure, and optional social sentiment without those inputs becoming automatic trade signals.

`scripts/market_index_context_producer.py` is a separate read-only producer for benchmark history. It fetches public Yahoo chart history for US and HK benchmarks/ETFs and writes `/tmp/market_index_context_inputs.json`, which `market_context_report.py` uses for 20/50-day native index confirmation when DB index tables are empty. This producer is intentionally separated from `market_sentiment_producer.py`: sentiment gives short-horizon risk appetite, while index context gives multi-week benchmark trend evidence.

It reads producer files:

```bash
/tmp/market_sentiment_inputs.json
/tmp/market_sentiment_inputs.jsonl
```

`scripts/market_sentiment_producer.py` is the default low-dependency producer for this contract. It fetches public Yahoo chart snapshots for VIX, SPY, QQQ, Hang Seng Index, and 2800.HK, then overwrites `/tmp/market_sentiment_inputs.json` with normalized `volatility` and `risk_appetite` indicators. It is intentionally read-only: it does not submit orders, change strategy settings, write v5 alerts, or replace real news/macro/event producers.

Minimal JSON input example:

```json
{
  "indicators": [
    {
      "id": "vix-20260612-1000",
      "indicator_type": "volatility",
      "name": "VIX",
      "source": "infohub",
      "observed_at": "2026-06-12T10:00:00",
      "markets": ["US"],
      "direction": "risk_on",
      "score": 0.35,
      "value": 16.2,
      "previous_value": 18.4,
      "unit": "index",
      "summary": "VIX eased and supports improved risk appetite."
    }
  ]
}
```

Supported `indicator_type` values are `volatility`, `capital_flow`, `breadth`, `risk_appetite`, `funding`, `social_sentiment`, and `macro`. `score` is normalized from `-1.0` to `1.0`, where negative values are risk-off and positive values are risk-on. `direction` should be `risk_on`, `risk_off`, `neutral`, `mixed`, or `unknown`.

Default command:

```bash
/usr/bin/python3 /root/market_sentiment_producer.py --output /tmp/market_sentiment_inputs.json --text
/usr/bin/python3 /root/market_sentiment_report.py --output /tmp/market_sentiment_report.json --text
```

Producer ingest helper:

```bash
/usr/bin/python3 /root/market_sentiment_ingest.py \
  --indicator-json '{"id":"vix-20260612-1000","indicator_type":"volatility","name":"VIX","source":"infohub","observed_at":"2026-06-12T10:00:00","markets":["US"],"direction":"risk_on","score":0.35,"value":16.2,"previous_value":18.4,"unit":"index","summary":"VIX eased and supports improved risk appetite."}' \
  --text
```

`market_sentiment_ingest.py` validates, de-duplicates, and appends accepted indicators to `/tmp/market_sentiment_inputs.jsonl`. It only writes the sentiment JSONL input file. It does not submit orders, change strategy, or write the v5 alert queue. Use `--dry-run` to validate producer payloads without writing, or `--input-file` / `--input-jsonl-file` when Wudao, Info Hub, or a broker/macro data exporter emits batches.

The output schema is `market_sentiment_report_v1`. `status=MISSING` means no quantified sentiment producer has written usable inputs. `status=STALE` means indicators exist but are outside the freshness window. `status=RISK` means fresh indicators point to risk-off sentiment. `status=OK` means fresh indicators are available without a risk-off blocker. `status=FAIL` is reserved for hard parser/producer failures.

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/market_sentiment_report.json` into top-level `market_sentiment`.
- Hermes should use it alongside `market_context`, `external_market_context`, `event_catalysts`, portfolio risk, and strategy evidence.
- Missing/stale sentiment means Hermes must state that quantified sentiment awareness is incomplete.
- Risk-off sentiment should reduce confidence for new BUY approvals, even when technical indicators are valid.

Readiness integration:

- `execution_readiness_report.py` reads `/tmp/market_sentiment_report.json`.
- `MISSING`, `STALE`, and `RISK` are warning gates that prevent `READY` but do not hard-block existing read-only jobs.
- `FAIL` is a blocking gate because the sentiment parser/upstream context is not trustworthy.

Recommended read-only cron:

```bash
*/5 * * * * /usr/bin/python3 /root/market_sentiment_producer.py --output /tmp/market_sentiment_inputs.json --text >> /tmp/market_sentiment_producer.log 2>&1 && /usr/bin/python3 /root/market_sentiment_report.py --output /tmp/market_sentiment_report.json --text >> /tmp/market_sentiment_report.log 2>&1
*/30 * * * * /usr/bin/python3 /root/market_index_context_producer.py --output /tmp/market_index_context_inputs.json --text >> /tmp/market_index_context_producer.log 2>&1
```

### Fundamentals context report

`scripts/fundamentals_context_report.py` is the read-only contract for giving Hermes valuation and fundamental awareness without letting fundamental data become an automatic trading signal. It consumes producer files:

```bash
/tmp/fundamentals_context_inputs.json
/tmp/fundamentals_context_inputs.jsonl
```

Expected item fields include `symbol`, `market`, `source`, `as_of`, `pe_ttm`, `pb`, `ps`, `roe_pct`, `revenue_growth_pct`, `earnings_growth_pct`, `dividend_yield_pct`, `debt_to_equity`, and `market_cap`. Wudao, Info Hub, broker exports, or manual operator appenders can write the same JSON/JSONL contract.

`scripts/fundamentals_context_producer.py` is the default low-dependency bootstrap producer for this contract. It reads `/root/rt_signal_watchlist.json` or `--symbols`, fetches Yahoo quoteSummary modules for valuation/profitability/growth/leverage fields, and overwrites `/tmp/fundamentals_context_inputs.json`. If Yahoo quoteSummary is unavailable, the producer falls back to Tencent quote snapshots for the failed symbols. Tencent fallback is deliberately partial: it currently writes symbol/name/market/currency and conservative quote-level `pe_ttm` only, leaves unverified metrics such as `market_cap`, `pb`, `ps`, ROE, growth, and leverage empty, and emits `fallback_provider_used:*:tencent_quote_snapshot_partial` warnings. Hermes must treat fallback items as limited valuation awareness, not as complete fundamental coverage.

The producer is intentionally read-only: it does not submit orders, change strategy settings, write v5 alerts, or replace higher-quality Wudao/Info Hub/broker fundamentals. Provider failures are emitted as producer warnings and should reduce Hermes confidence rather than crash the trading stack.

Producer example:

```bash
/usr/bin/python3 /root/fundamentals_context_producer.py --symbols 00700,AAPL --output /tmp/fundamentals_context_inputs.json --text
```

Broker/vendor/official fundamentals ingest:

```bash
/usr/bin/python3 /root/fundamentals_context_ingest.py --input-file <trusted_fundamentals_payload.json> --dry-run --text
/usr/bin/python3 /root/fundamentals_context_ingest.py --input-file <trusted_fundamentals_payload.json> --append --text
```

`fundamentals_context_ingest.py` validates `symbol`, `as_of`, `source`, timestamps, and numeric metric fields before appending accepted rows to `/tmp/fundamentals_context_inputs.jsonl`. It defaults to dry-run and only writes when `--append` is explicit. It does not submit orders, change strategy, write v5 alerts, edit cron, or repair K-lines. Use source/provider names such as `broker_fundamentals_snapshot`, `vendor_fundamentals_snapshot`, `official_filing`, `exchange_filing_snapshot`, or `wudao_fundamentals` so `trusted_source_preflight.py` and `source_reliability_report.py` can distinguish full trusted coverage from public fallback.

Default command:

```bash
/usr/bin/python3 /root/fundamentals_context_report.py --output /tmp/fundamentals_context_report.json --text
```

The output schema is `fundamentals_context_report_v1`. `status=MISSING` means no fundamentals producer has written usable inputs. `status=STALE` means all items are older than the configured age window. `status=RISK` means fresh items contain valuation/profitability/earnings/leverage flags such as `overvalued`, `negative_earnings`, `weak_profitability`, `earnings_decline`, `high_leverage`, or `partial_fundamentals`. `status=OK` means fresh context is available without those flags. `status=FAIL` is reserved for hard parser/producer failures.

The report now separates freshness from metric coverage. Each `items[]` entry includes `provider_symbol` and `fundamental_completeness` with `level`, metric counts, coverage ratio, and available/missing metric lists. `summary.by_source`, `summary.completeness_counts`, `summary.full_item_count`, `summary.partial_item_count`, `summary.fallback_item_count`, `summary.producer_fetch_failed_count`, and `summary.fallback_provider_used_count` tell Hermes whether the context is complete or only fallback-level. This is backward-compatible: existing readers can ignore the new fields, while Hermes should use them to avoid treating fresh Tencent fallback PE-only snapshots as complete fundamentals.

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/fundamentals_context_report.json` into top-level `fundamentals_context`.
- Hermes should use it alongside `market_sentiment`, `external_market_context`, `event_catalysts`, technical signals, portfolio risk, and strategy evidence.
- Missing/stale fundamentals means Hermes must state that fundamental awareness is incomplete before supporting a trade.
- Fresh but partial fundamentals, especially `source=tencent_quote_snapshot` fallback items or `valuation_flags` containing `partial_fundamentals`, mean Hermes may cite the available metric such as PE but must explicitly state that missing metrics such as PB, PS, ROE, growth, dividend, market cap, or leverage were not verified.
- `approve` and `reduce` judgments must set `context_review.fundamentals_context_reviewed=true`; `hermes_judgment_audit_report.py` fails approvals/reductions that omit this flag.
- For BUY approvals/reductions on a symbol whose fundamentals are partial/fallback, Hermes must also write `fundamentals_context_limit_acknowledged=true`, `fundamentals_context_symbols[]`, `fundamentals_context_missing_metrics[]`, and `fundamentals_context_notes[]`. The audit treats a checkbox-only `fundamentals_context_reviewed=true` as insufficient.

Readiness integration:

- `execution_readiness_report.py` reads `/tmp/fundamentals_context_report.json`.
- `MISSING`, `STALE`, and `RISK` are warning gates that prevent `READY` but do not hard-block existing read-only jobs.
- `FAIL` is a blocking gate because the fundamentals parser/upstream context is not trustworthy.

Recommended read-only cron:

```bash
*/30 * * * * /usr/bin/python3 /root/fundamentals_context_producer.py --output /tmp/fundamentals_context_inputs.json --text >> /tmp/fundamentals_context_producer.log 2>&1 && /usr/bin/python3 /root/fundamentals_context_report.py --output /tmp/fundamentals_context_report.json --text >> /tmp/fundamentals_context_report.log 2>&1
```

### Cron wiring audit

`scripts/cron_audit_report.py` is a read-only drift check between the expected v5/Hermes report jobs and the actual crontab. It does not install, edit, or remove cron entries. It writes:

```bash
/tmp/cron_audit_report.json
```

The output schema is `cron_audit_report_v1`. `status=OK` means all required read-only context/report jobs are present, the notify bridge is wired in local mode, optional Feishu delivery is either disabled or has environment-backed credentials, sent-state files are structurally valid, and no dangerous execution cron is enabled. `status=WARN` means required read-only jobs are missing, the notify bridge is missing or not in local mode, Feishu delivery is enabled without complete `FEISHU_APP_ID`/`FEISHU_APP_SECRET`/`FEISHU_CHAT_ID`, the sent-state files are malformed, or the intraday K-line batch producer plan / session override validator / daily-gap source diagnostics have not been installed yet; this usually explains stale readiness inputs and should be fixed from `config/hermes_v5_crontab.txt`. Each item in `missing_required_jobs` includes `recommended_cron`, the exact read-only line operators can install after review. The intraday K-line batch recommendation is still dry-run/report-only and writes only `/tmp/intraday_kline_batch.json`; it does not populate DB minute rows unless an operator separately reruns `intraday_kline_batch.py --apply --confirm-plan-hash <plan_hash>` after reviewing the plan. `status=FAIL` means a dangerous execution path such as `RT_ALERT_EXECUTION_MODE=alert-sim`, `RT_ALERT_EXECUTION_MODE=legacy-sim`, direct `rt_order_intake.py --mode execute`, or `quantmind_sim_trader.py` is enabled in live cron.

`alert_delivery` inside the report is also read-only. It checks `rt_alert_bridge.py` notify/local cron wiring, optional `RT_ALERT_SEND_FEISHU=1` setup, Feishu credential key presence from the environment or `/root/.quantmind_env`, and whether `/tmp/rt_signal_sent.json` plus `/tmp/rt_position_review_sent.json` are valid JSON lists. It redacts values and never sends messages, writes sent-state, edits crontab, or submits orders. Missing sent-state files are allowed as first-run state; malformed files warn because they can cause duplicate or suppressed notifications.

When jobs are missing, the report also emits `installation_plan` with schema `read_only_cron_installation_plan_v1`. This is a hash-stamped operator plan, not an installer:

- `proposal_hash` is the review identifier Hermes can cite;
- `install_lines[]` contains only recommended read-only cron lines that passed the local safety check;
- `rejected_lines[]` captures any recommended line that unexpectedly contains execution or apply tokens;
- `operator_contract.does_not_edit_crontab=true`, `submits_orders=false`, `uses_execute_mode=false`, `uses_apply_flags=false`, `enables_alert_sim=false`, and `enables_legacy_sim=false`;
- `manual_install_workflow` tells the operator to back up crontab first, manually install reviewed lines, then rerun cron audit, readiness refresh, and execution readiness.

Hermes must not treat `installation_plan.status=operator_review_required` as proof that cron has been fixed. Only a later `cron_audit.status=OK` with fresh timestamps proves the wiring is present.

Manual promotion tool:

```bash
/usr/bin/python3 /root/cron_install_promote.py --cron-audit-file /tmp/cron_audit_report.json --text
```

Dry-run is the default. Apply is explicit and hash-gated:

```bash
/usr/bin/python3 /root/cron_install_promote.py \
  --cron-audit-file /tmp/cron_audit_report.json \
  --apply \
  --confirm-proposal-hash <installation_plan.proposal_hash> \
  --text
```

`cron_install_promote.py` is intentionally narrower than hand-editing crontab:

- it reads only `cron_audit_report_v1.installation_plan.install_lines`;
- it rejects apply if the audit status is `FAIL`, if any line contains `--mode execute`, `--apply`, `alert-sim`, `legacy-sim`, or another unsafe execution token, or if the supplied hash does not match;
- it backs up the current crontab under `/tmp/crontab_backups/` before applying;
- it appends only missing reviewed read-only lines and leaves existing lines in place;
- it does not submit orders, restart services, change strategy config, promote watchlists, repair data, or enable simulation execution.

Post-apply verification remains mandatory:

```bash
/usr/bin/python3 /root/cron_audit_report.py --output /tmp/cron_audit_report.json --text
/usr/bin/python3 /root/readiness_refresh.py --skip-network-producers --output /tmp/readiness_refresh_report.json --text
/usr/bin/python3 /root/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text
```

Hermes may recommend the promotion command only as an operator action for read-only evidence wiring. It must still treat `alert-sim`, `legacy-sim`, direct `rt_order_intake.py --mode execute`, and hash-confirmed event-store apply cron lines as separate manual decisions outside this installer.

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/cron_audit_report.json` into top-level `cron_audit`.
- Hermes should treat missing read-only jobs as evidence-quality drift, not as trade approval.
- Hermes may cite `cron_audit.installation_plan.proposal_hash` and `install_lines[]` in an operator message, but must not claim the jobs are installed until a later audit proves it.
- Hermes must reject/hold if cron audit reports dangerous enabled execution jobs.

Readiness integration:

- `execution_readiness_report.py` reads `/tmp/cron_audit_report.json`.
- The `cron_wiring` gate includes `installation_plan` so the remediation hash is visible in the aggregate dashboard.
- `WARN` prevents `READY` because report freshness may be unreliable.
- `FAIL` hard-blocks readiness because execution could bypass the intended gated path.

Default command:

```bash
/usr/bin/python3 /root/cron_audit_report.py --output /tmp/cron_audit_report.json --text
```

Recommended read-only cron:

```bash
*/10 * * * * /usr/bin/python3 /root/cron_audit_report.py --output /tmp/cron_audit_report.json --text >> /tmp/cron_audit_report.log 2>&1
```

### Operator action queue

`scripts/operator_action_queue_report.py` is a read-only priority layer for Hermes/operator remediation. It reads the current execution readiness report, cron audit, cron promotion dry-run, Hermes packet, position-judgment audit, source reliability, simulation performance, and signal outcome reports, then emits `operator_action_queue_report_v1`.

The queue turns scattered WARN/BLOCKED evidence into explicit action items:

- `P0` advisory or safety work, such as high-urgency position reviews without Hermes judgments or failed simulation performance that should keep `alert-sim` disabled;
- `P1` wiring/evidence work, such as hash-gated notify cron promotion, stale readiness inputs, missing forward outcomes, or missing Hermes judgment-effect samples;
- `P1/P2` source-provider work, such as trusted fundamentals, trusted event/macro feeds, missing trusted-source capabilities, K-line source-granularity provenance proposals, or broker/vendor minute OHLCV.

The report itself sets `source.read_only=true`, `submits_orders=false`, `writes_judgments=false`, `changes_crontab=false`, `changes_portfolio=false`, and `changes_strategy=false`. Individual action rows describe the effect if an operator manually runs the recommended command. For example, `install_rt_alert_bridge_notify_cron` may include the hash-confirmed `cron_install_promote.py --apply --confirm-proposal-hash ...` command and marks `operator_effect.changes_crontab=true`, while still marking `uses_execute_mode=false`, `enables_alert_sim=false`, and `enables_legacy_sim=false`.

`hermes_review_packet.py` reads `/tmp/operator_action_queue_report.json` into top-level `operator_action_queue` when available. Hermes should use it as a remediation checklist, not as trade evidence and not as execution permission. A queue item that says `writes_judgments=true` means Hermes must write completed advisory JSONL itself; templates are not judgments. A queue item that says `changes_crontab=true` means the listed command still requires an explicit operator action and normal post-apply verification.

Cron-install queue items are safe against stale dry-run hashes. `operator_action_queue_report.py` compares `/tmp/cron_install_promotion_report.json.proposal_hash` with the current `/tmp/cron_audit_report.json.installation_plan.proposal_hash`. If they differ, are missing, or the promotion report is not a current dry-run, the queue emits only the dry-run regeneration command and sets `operator_effect.changes_crontab=false` with `cron_promotion_report_stale_or_mismatched` in `blockers`. Hermes must not tell an operator to apply a stale hash.

K-line source-granularity queue items are also remediation-only. If the item exposes `kline_source_granularity_report.py --apply --confirm-proposal-hash ...`, that command is limited to adding/backfilling `klines.source_granularity`; `operator_effect.does_not_change_ohlcv_prices_or_volumes=true`, `submits_orders=false`, and `changes_strategy=false`. Hermes must still require manual review of the proposal SQL and post-apply reruns before treating minute provenance as current.

Default command:

```bash
/usr/bin/python3 /root/operator_action_queue_report.py --output /tmp/operator_action_queue_report.json --text
```

### Source reliability report

`scripts/source_reliability_report.py` is a read-only source-quality matrix for Hermes. It does not fetch data, repair data, edit crontab, change strategy, or submit orders. It reads the already-produced evidence reports and summarizes whether each source layer is complete, degraded, stale, missing, or failed:

```bash
/tmp/data_health_report.json
/tmp/data_source_inventory_report.json
/tmp/kline_source_granularity_report.json
/tmp/market_context_report.json
/tmp/intraday_timeframe_quality_report.json
/tmp/external_market_context_report.json
/tmp/event_catalyst_report.json
/tmp/event_catalyst_signal_report.json
/tmp/market_sentiment_report.json
/tmp/fundamentals_context_report.json
/tmp/trusted_source_preflight_report.json
/tmp/trusted_source_discovery_report.json
/tmp/cron_audit_report.json
```

Default command:

```bash
/usr/bin/python3 /root/source_reliability_report.py --output /tmp/source_reliability_report.json --text
```

The output schema is `source_reliability_report_v1`. `status=OK` means every tracked source layer is fresh and not degraded by known coverage problems. `status=DEGRADED` means at least one source is fresh but weakened, for example market context with missing/incomplete native index confirmation, native index evidence that conflicts with stock-pool breadth, intraday context with stale or missing watchlist-symbol minute coverage, intraday timeframe quality with limited/missing/conflicting 5m/15m/30m/60m evidence, pending K-line source-granularity schema/backfill proposals, external context that only contains public fallback headlines, external provider fetch failures, missing capital-flow coverage, fallback fundamentals, partial metric coverage, a WARN/MISSING trusted-source preflight, missing or unverified trusted-source discovery capabilities, data-source inventory weaknesses such as missing context reports or incomplete K-line provenance, missing read-only cron jobs, missing K-line provenance, or repair-source K-lines. `status=STALE` means at least one report timestamp is too old. `status=MISSING` means a report is absent or unreadable. `status=FAIL` means a source layer or safety contract is not trustworthy, for example an invalid schema, failed report, failed data-source inventory, failed K-line source-granularity report, failed trusted-source preflight, failed source discovery, unsafe intraday timeframe-quality safety contract, or dangerous execution cron.

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/source_reliability_report.json` into top-level `source_reliability`.
- Hermes should use it to decide whether a fresh context report is actually complete. For example, a fresh market context report can still be `DEGRADED` if native HK/US index tables are empty or stale; a fresh external context report can still be `DEGRADED` if every item is public RSS/Info Hub bridge fallback and no trusted Wudao/broker/capital-flow provider is present; a fresh fundamentals report can still be `DEGRADED` if Yahoo failed and all items are Tencent fallback PE-only snapshots.
- Hermes must not treat this as an execution signal; it is evidence-quality context for confidence and rejection/hold reasoning.
- `approve` and `reduce` judgments must set `context_review.source_reliability_reviewed=true`.
- When `source_reliability.status` is not `OK`, `approve` and `reduce` judgments must also write `source_reliability_limit_acknowledged=true`, `source_reliability_components[]`, `source_reliability_reasons[]`, and `source_reliability_notes[]`. The audit treats a checkbox-only review as insufficient.

Readiness integration:

- `execution_readiness_report.py` reads `/tmp/source_reliability_report.json`; data-source inventory and K-line source-granularity proposals affect readiness through their components inside source reliability.
- `source_reliability.status=FAIL` hard-blocks readiness.
- `DEGRADED`, `STALE`, or `MISSING` prevent `READY` as warning gates because Hermes cannot claim institutionally reliable context awareness while source coverage is weak.
- For `intraday_context` and `intraday_timeframe_quality`, source reliability is intentionally conservative: stale/missing symbol coverage, partial 30m/60m windows, source-granularity gaps, and low-fidelity snapshot rows are degraded evidence, not proof that the daily bar system is invalid. Hermes must disclose the limitation and avoid using intraday data to raise confidence, but it must not use intraday availability to override daily data-health, strategy-evidence, simulation-performance, or execution-readiness blocks.

Recommended read-only cron:

```bash
*/10 * * * * /usr/bin/python3 /root/source_reliability_report.py --output /tmp/source_reliability_report.json --text >> /tmp/source_reliability_report.log 2>&1
```

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
- BUY approve/reduce judgments that ignore `native_index_context.alignment=conflicts_with_breadth`;
- approvals while strategy evidence is unresolved or below thresholds;
- approve/reduce judgments missing structured `context_review`, or missing any required `*_reviewed=true` flag;
- BUY approve/reduce judgments that rely on partial/fallback fundamentals without structured limitation acknowledgement;
- approve/reduce judgments made while source reliability is degraded, stale, missing, or failed without structured source-quality acknowledgement;
- expired judgments and duplicate signal judgments.

Example:

```bash
/usr/bin/python3 /root/hermes_judgment_audit_report.py --output /tmp/hermes_judgment_audit_report.json --text
```

Hermes packet integration:

- `hermes_review_packet.py` reads `/tmp/hermes_judgment_audit_report.json` into `judgment_audit` when the file exists.
- `execution_readiness_report.py` treats `judgment_audit.status=FAIL` as a hard block because un-auditable or contradictory Hermes judgments cannot support execute readiness.
- Missing audit does not block packet generation.
- Any audit recommendation beginning with `fix_or_reject_judgments`, `approve_reduce_judgments_require_structured_context_review`, `native_index_conflicts_require_explicit_breadth_and_index_discussion`, `partial_fundamentals_buy_approvals_require_structured_limitation_acknowledgement`, `source_reliability_degraded_approvals_require_structured_limitation_acknowledgement`, `intraday_context_challenges_require_structured_acknowledgement`, or `approvals_conflict_with_execution_gates` should be treated as a hard reason to keep `alert-sim` disabled.

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
- `audit_coverage`, including audit report schema/status/timestamp, whether the audit report is truncated, matched PASS/FAIL/missing event counts, and examples of missing or failed audit rows;
- safety flags confirming that it does not submit orders, change intake state, change strategy config, or restart services.

`audit_status=missing` is intentionally not treated as clean history. It means the judgment event could not be matched to an audit row, usually because the audit report is absent, stale, truncated, or keyed to a different packet/review time. The event-store report emits `judgment_events_missing_audit_rows:N` and, when applicable, `audit_report_truncated_unmatched_judgments:N`. Hermes and strategy-learning consumers should treat those rows as unaudited until a fresh judgment audit is regenerated and the event store is rerun. `audit_status=FAIL` rows are preserved with `audit_reasons` and `audit_json`; they are historical QA evidence, not approval evidence.

Apply mode is hash-gated:

```bash
/usr/bin/python3 /root/hermes_judgment_event_store.py \
  --apply \
  --confirm-schema-hash <schema_hash> \
  --output /tmp/hermes_judgment_event_store_report.json \
  --text
```

Apply mode only creates/updates the audit table and upserts judgment events on a stable `judgment_key`. It does not replace the JSONL judgment contract, does not make an approval executable, does not modify `/tmp/rt_order_intake_state.json`, and does not call the simulation API.

Hermes packet and readiness integration:

- `hermes_review_packet.py` reads `/tmp/hermes_judgment_event_store_report.json` into top-level `judgment_event_store` when the file exists.
- Hermes should continue writing trade judgments to `/tmp/hermes_trade_judgments.jsonl`; the event store is a persistence/audit layer behind that contract.
- Missing or dry-run event-store status does not block packet generation. It tells Hermes/operator that long-term judgment history still depends on JSONL retention.
- `execution_readiness_report.py` includes this report in `event_store_durability`. `dry_run` is a readiness warning, while failed/invalid reports, unsafe safety flags, or persisted `audit_status=FAIL` judgment events hard-block readiness.
- Missing `audit_coverage`, `audit_missing_event_count > 0`, or `audit_report_truncated=true` with unmatched events prevents `READY` as a warning. This is a no-loss integration rule: applied persistence is not treated as institution-grade history until every stored Hermes judgment is matched to the current judgment-audit report.

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
- `lineage_summary` with schema `rt_order_intake_lineage_summary_v1`, showing whether submitted intake decisions have extractable `order_id` values;
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

The audit table stores both the raw `order_result` JSON and indexed `order_id`/`order_ids` columns extracted from common API response shapes such as `order_result.order_id`, `order_result.id`, or nested `data/order/result` objects. This is only for audit joins: it lets later postmortem tooling connect `sim_trades.order_id` back to `rt_order_intake_events.signal_id` without parsing every JSONB decision row. If `lineage_summary.status=DEGRADED`, at least one submitted decision lacks an extractable order ID, so closed-trade learning must not treat that decision as fully traceable.

Hermes packet and readiness integration:

- `hermes_review_packet.py` reads `/tmp/rt_order_intake_event_store_report.json` into top-level `order_intake_event_store` when the file exists.
- Missing or dry-run intake event-store status does not block packet generation. It tells Hermes/operator that long-term intake-decision history still depends on JSON state retention.
- This closes the audit chain between alert review and simulated execution result: v5 alert -> packet -> Hermes judgment -> intake decision -> order result / rejection.
- `execution_readiness_report.py` includes this report in `event_store_durability`. `dry_run` is safe but not considered institution-grade persistence; `failed`, `blocked`, `invalid`, wrong schema, or unsafe safety flags block readiness.

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

Hermes packet now enriches `position_review.items[]` with advisory-only context:

- each item may include `context_digest.schema=hermes_position_review_context_digest_v1`;
- the digest matches the holding's symbol and market against `market_context`, `intraday_context`, `intraday_market_session_overrides`, `external_market_context`, `event_catalysts`, `event_catalyst_signals`, `market_sentiment`, `fundamentals_context`, `trusted_source_preflight`, and `source_reliability`;
- `context_digest.position_attention[]` highlights holding-specific issues such as negative external context, risk-off sentiment, partial fundamentals, stale/missing intraday coverage, incomplete market-session overrides, and degraded source reliability;
- the digest is `read_only=true`, `advisory_only=true`, and `submits_orders=false`; it does not change portfolio records, generate trade approvals, or call the simulation API.

This enrichment is deliberately in `hermes_review_packet.py`, not in `portfolio_report.py`. The portfolio report remains the accounting and position-risk source of truth; the packet adds LLM review evidence around that source of truth.

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
  "risk_notes": ["review again next session before adding new exposure"],
  "context_review": {
    "position_context_reviewed": true,
    "portfolio_risk_reviewed": true,
    "market_context_reviewed": true,
    "external_context_reviewed": true,
    "intraday_context_reviewed": true,
    "notes": ["negative event context and partial fundamentals were reviewed before choosing watch"]
  },
  "position_attention_acknowledged": true,
  "position_attention_codes": [
    "position_negative_external_context_requires_discussion",
    "position_source_reliability_limit_requires_discussion"
  ],
  "position_attention_notes": [
    "Negative external context and degraded source reliability were treated as reasons to avoid adding exposure and schedule a follow-up review."
  ],
  "position_attention_effects": [
    {
      "code": "position_negative_external_context_requires_discussion",
      "effect": "Negative symbol-level news raises gap and regulatory-risk uncertainty for the existing holding.",
      "decision_impact": "Do not add exposure; keep watch/reduce review open until the next context refresh."
    },
    {
      "code": "position_source_reliability_limit_requires_discussion",
      "effect": "The context source layer is degraded, so the report cannot claim complete event/fundamental awareness.",
      "decision_impact": "Use conservative watch advice and require follow-up instead of treating missing context as neutral."
    }
  ]
}
```

Valid decisions are `hold`, `watch`, `reduce`, `exit`, and `trail_stop`. For user portfolio items, keep the machine-readable `decision` to `hold` or `watch`; put manual reduce/exit advice in `risk_notes` so no downstream automation can confuse user advice with executable intent. For simulation items, `reduce`, `exit`, and `trail_stop` are still advisory and require a separate gated execution path if an operator later wants to act.

When the reviewed `position_review.items[].context_digest.position_attention[]` is non-empty, Hermes must include:

- `position_attention_acknowledged=true`;
- `position_attention_codes[]` copying every reviewed `position_attention[]` code;
- `position_attention_notes[]` explaining how those holding-specific risks changed hold/watch/reduce/exit/trail-stop advice;
- `position_attention_effects[]` with one object per reviewed code, each containing `code`, `effect`, and `decision_impact`.

This is deliberately stricter than `context_review.position_context_reviewed=true`. The checkbox proves Hermes opened the digest; the structured attention fields prove Hermes responded to the actual holding risks. `hermes_position_judgment_audit_report.py` fails missing evidence with `missing_position_attention_acknowledgement`, `position_attention_codes_missing_or_unmatched`, `position_attention_notes_missing`, `position_attention_effects_missing`, `position_attention_effects_missing_or_unmatched`, `position_attention_effect_detail_missing`, or `position_attention_effect_decision_impact_missing`.

Audit command:

```bash
/usr/bin/python3 /root/hermes_position_judgment_audit_report.py --output /tmp/hermes_position_judgment_audit_report.json --text
```

The audit checks:

- judgment schema and required advisory flags;
- exact `packet_id` and `review_id` linkage through `/tmp/hermes_review_packet_archive/`;
- symbol, portfolio, and role consistency with the reviewed `position_review` item;
- complete `context_review` flags when the reviewed `position_review` item includes `context_digest`;
- complete `position_attention` acknowledgement when the reviewed context digest highlights holding-specific risks;
- user portfolio action-token violations;
- high-urgency hold/watch decisions with weak rationale;
- coverage of high-urgency `position_review.items[]`;
- expired and duplicate review judgments.

The report emits top-level `status=OK` when no high-urgency position reviews are waiting for Hermes coverage and all observed position judgments pass the audit. It emits `status=WARN` when the latest packet contains high-urgency `position_review.items[]` without matching Hermes position judgments. This is not a contract violation, but it means the high-risk holding has not actually been reviewed, so `execution_readiness_report.py` keeps the system out of `READY`. The report emits `status=FAIL` when any position judgment violates the advisory contract, mismatches its archived packet item, expires, or duplicates a `review_id`. `execution_readiness_report.py` treats `status=FAIL` as a hard block because unsafe advisory judgments can confuse user-holding advice, simulation position review, and future operator decisions even though they do not submit orders.

Coverage output:

- `coverage.schema=hermes_position_judgment_coverage_v1`;
- `coverage.position_review_item_count`, `judged_review_count`, and `unjudged_review_count` summarize whether Hermes wrote judgments for the latest packet's position reviews;
- `coverage.high_urgency_review_count` and `unjudged_high_urgency_review_count` isolate urgent holdings that still need advisory review;
- `coverage.unjudged_high_urgency_examples[]` gives Hermes and the operator the exact `review_id`, role, portfolio, symbol, and recommended action to review;
- recommendation `write_position_judgments_for_high_urgency_reviews:N` means Hermes should append advisory-only judgments for those high-urgency reviews before treating the packet as fully assessed.

Hermes packet integration:

- `hermes_review_packet.py` exposes `position_judgment_contract` at the top level;
- `position_judgment_contract.append_jsonl_object.context_review` lists the required advisory context-review flags for enriched position items;
- `position_judgment_contract.append_jsonl_object.position_attention_*` lists the required structured attention acknowledgement fields for any non-empty `context_digest.position_attention[]`;
- each `position_review.items[]` also carries `position_judgment_template`, a draft helper that pre-fills `packet_id`, `review_id`, `portfolio_id`, `role`, `symbol`, and all required `position_attention_codes[]`/effect rows; it is marked `template_only=true` and `ready_to_append_without_hermes_review=false`, keeps `confidence`, `reviewed_at`, and `context_review` as placeholders, and must not be appended unchanged;
- `hermes_review_packet.py` reads `/tmp/hermes_position_judgment_audit_report.json` into `position_judgment_audit` when the file exists;
- missing position judgment audit does not block packet generation;
- `position_judgment_audit` warnings should keep the workflow in advisory review until high-urgency coverage is complete;
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
- `outcome_maturity`, explaining pending evidence with latest signal date, latest available K-line date, missing future-day counts, calendar and weekday-aware primary-horizon dates for pending signals, calendar-pending reason counts, and missing-symbol K-line counts;
- 1/3/5 trading-day signed close return by signal direction;
- win rate by horizon;
- stop/take-profit touch rates using daily high/low after the alert quote date;
- `first_hit_counts` from daily bars plus `effective_first_hit_counts`, `effective_target_first_rate_pct`, `effective_stop_first_rate_pct`, and `effective_unresolved_first_hit_rate_pct`, where minute-resolved `ambiguous_same_day` cases are counted as `intraday_target` or `intraday_stop`;
- trigger-level, symbol-level, and confirmed/unconfirmed performance;
- strategy-config and watchlist attribution via `strategy_config_id`, `strategy_config_version`, and `watchlist_id`;
- `intraday_sequence_summary`, which reports whether minute bars resolved any daily `ambiguous_same_day` stop/target ordering cases;
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

Important limitation: this report still uses daily bars for horizon returns, win rate, max favorable/adverse excursion, and stop/take-profit touch rates. When stop and target are both touched on the same daily bar, the daily result remains `first_hit=ambiguous_same_day`; the report then attempts a read-only minute-bar lookup for that symbol/date only. If `klines.interval='min'` rows can order the touch sequence, the outcome includes `intraday_sequence.status=RESOLVED` and `intraday_sequence.first_hit=target|stop`. If minute rows are missing, contradict the daily threshold touch, or touch both levels inside one minute bar, the result stays conservative with `MISSING`, `UNRESOLVED`, or `AMBIGUOUS` status.

This is a no-loss evidence upgrade: minute bars do not create resolved outcomes, do not change signed close returns, do not relax strategy-evidence gates, do not repair daily K-lines, and do not submit orders. Hermes may use `intraday_sequence_summary` and the effective first-hit rates to judge whether stop-first/target-first evidence is stronger or still ambiguous during strategy review and simulation post-mortems.

`outcome_maturity` is diagnostic only. It helps distinguish normal waiting for future daily K-lines from a K-line pipeline gap. The evaluator measures future daily K-line rows, not raw calendar days, so it now exposes both `earliest_primary_horizon_date_for_pending` and `earliest_primary_horizon_trading_date_for_pending`. `calendar_pending_reason_counts.waiting_for_next_trading_day` means the sample is immature, for example Friday alerts waiting for Monday daily bars. `missing_symbol_klines` or `kline_gap_or_missing_symbol` points to ingestion, active-universe, or symbol-mapping work. `missing_symbol_kline_diagnostics` attributes those symbols as `not_found_in_stocks_no_klines`, `not_found_in_stocks_has_klines`, `stock_found_no_day_klines`, or `stock_found_has_day_klines_before_signal_date`, so operators can decide whether to fix symbol mapping, active-universe coverage, or K-line ingestion. Each diagnostic row includes `affected_signal_count`, because many pending signals may collapse to a smaller number of unique broken symbols. Packet summary status counts are weighted by affected signal count. This extra maturity context does not relax `rt_order_intake.py` strategy-evidence gates, does not convert pending outcomes into resolved outcomes, and does not write repairs.

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

Hermes packet and readiness integration:

- `hermes_review_packet.py` reads `/tmp/rt_signal_outcome_event_store_report.json` into top-level `signal_outcome_event_store` when the file exists.
- Missing or dry-run outcome event-store status does not block packet generation. It tells Hermes/operator that long-term outcome evidence still depends on report file retention.
- Execute gates still read `/tmp/rt_signal_outcome_report.json`; the event store is for durable audit, cohort analysis, and later strategy/Hermes quality review.
- `execution_readiness_report.py` includes this report in `event_store_durability`. Outcome-store `dry_run` prevents `READY` because resolved-signal evidence would still be only file-retained, not durably queryable.

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

`rt_order_intake.py` now enforces the outcome report in execute mode. This means a future `alert-sim` rollout has five independent approvals before an order can be submitted:

- system health is not `FAIL`;
- aggregate execution readiness is `READY`;
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
- `audit_pass_judgment_effect`, the same cohort comparison after excluding judgments that did not pass `hermes_judgment_audit_report.py`;
- `judgment_audit_coverage`, showing how many joined judgments passed, failed, or were missing from the audit report;
- `context_review_quality`, measuring whether `approve/reduce` judgments included complete structured `context_review`;
- `context_review_effect`, comparing forward outcomes for complete-context `approve/reduce`, incomplete-context `approve/reduce`, `reject/hold`, and missing judgment cohorts;
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

`context_review_effect` is the bridge from "Hermes reviewed more context" to "Hermes improved outcomes." A complete `context_review` is only a process-control requirement until enough resolved outcomes show that complete-context approvals outperform rejected/held signals and incomplete-context approvals. If the complete-context cohort does not outperform after the minimum sample is met, Hermes should treat the prompt, evidence weighting, and approval criteria as suspect rather than assuming the LLM layer adds alpha.

The learning report uses the same required `context_review.*_reviewed=true` checklist as `hermes_judgment_audit_report.py`, including `intraday_context_reviewed`. This keeps Hermes self-review aligned with the stricter approval audit: approvals that did not open the intraday minute-bar context are counted as incomplete-context approvals. Structured intraday acknowledgements such as `intraday_context_acknowledged`, `intraday_context_status`, and `intraday_context_notes` are still enforced by the judgment audit when the packet raises an intraday challenge or stale/missing-data attention item; strategy learning then measures whether those audited complete-context approvals actually perform better over forward outcomes.

`audit_pass_judgment_effect` is the safer Hermes-alpha cohort for readiness and self-review. Raw `judgment_effect` remains in the report for backward compatibility and debugging, but any `approve/reduce` or `reject/hold` row whose judgment audit status is not `PASS` is excluded from `audit_pass_judgment_effect` and counted under the corresponding `excluded_*` cohort. This prevents a profitable but contract-invalid approval, for example one missing intraday/source/fundamentals acknowledgement, from being counted as evidence that Hermes adds value. `execution_readiness_report.py` prefers `audit_pass_judgment_effect` when it exists and falls back to raw `judgment_effect` only for older learning reports.

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

`hermes_review_packet.py` reads the report into top-level `strategy_learning` when available. It also emits an additive top-level `strategy_learning_brief` so Hermes does not need to search the full nested report for the current reliability state. The full `strategy_learning` object remains authoritative and unchanged for existing readers. In the brief, `judgment_effect` prefers the audit-pass cohort from `audit_pass_judgment_effect`; `raw_judgment_effect` is retained only as diagnostic context, and `judgment_audit_coverage` shows how many approvals or rejections were excluded.

The brief also emits `hermes_alpha_evidence` with schema `hermes_alpha_evidence_summary_v1`. This is a conservative machine-readable label for whether Hermes approval/reduction judgments have proven incremental value over rejected/held judgments:

- `SUPPORTIVE` requires audit-pass `approve/reduce` and `reject/hold` cohorts to each meet the minimum resolved sample, approval average signed return to be positive, and approval average return to exceed rejected/held average return.
- `INSUFFICIENT` means the audit-pass report is missing, raw-only, truncated, has too few resolved samples, or has audit-failed/missing approvals that prevent clean attribution.
- `NEGATIVE` means the audit-pass approval cohort is non-positive or does not outperform rejected/held signals after the minimum sample is met.

This summary is read-only and does not submit orders, promote strategies, apply watchlists, exclude evidence, or override `rt_order_intake.py` gates. Hermes should treat `INSUFFICIENT` or `NEGATIVE` as a reason to keep approvals conservative and continue collecting/reviewing evidence rather than claiming the LLM layer already adds alpha. Any `approve` or `reduce` judgment under `INSUFFICIENT` or `NEGATIVE` evidence must include the structured `hermes_alpha_evidence_*` acknowledgement fields described in the judgment contract, so audit reports can prove Hermes did not cite unproven LLM alpha as support.

`strategy_learning_brief` is read-only and contains:

- current `sample_scope.strategy_config_id` and `sample_scope.watchlist_id`;
- outcome evidence, including whether the minimum resolved sample is met;
- outcome maturity, including latest signal/K-line dates, missing future-day counts for pending signals, missing-symbol K-line diagnostic status counts, daily-gap repair context, and daily-gap source diagnostic category counts;
- overall, directional, and WATCH intake coverage;
- Hermes judgment effect for approved/reduced versus rejected/held cohorts, preferring audit-pass cohorts when present, plus raw diagnostic judgment-effect and audit coverage;
- context-review quality and forward outcome effect for complete-context approvals versus incomplete approvals and rejected/held judgments;
- `hermes_alpha_evidence`, a conservative audit-pass label for whether Hermes approvals have enough clean evidence to claim incremental value;
- intraday signal alignment summary from `strategy_learning.by_intraday_signal_alignment`, including support/challenge group counts, resolved samples, average signed forward return, win rate, and a Hermes note for whether samples are strong enough to consider a future hold/reduce rule;
- sizing blocker remediation status, covered/uncovered symbols, `watchlist_proposal_hash`, `current_watchlist_id`, and `proposed_watchlist_id`;
- the leading learning recommendations.

Hermes should use the brief as an attention guide: low directional intake coverage means learning evidence is incomplete; low overall coverage caused by WATCH rows is an observation-quality issue; outcome maturity explains whether zero resolved outcomes are expected waiting or a data pipeline issue; daily-gap source categories explain whether missing outcome evidence is repairable, provider-lagged, or really an active-universe/symbol-mapping defect; complete-context approval outcomes remain unproven until they beat rejected/held cohorts on resolved forward returns; audit-failed approvals are not Hermes-alpha evidence even if they later made money; intraday signal alignment is no-lookahead learning evidence and should be checked before treating same-session 5m/15m/30m/60m support or contradiction as useful; sizing blocker remediation is manual watchlist proposal context only. The brief must not apply watchlists, restart services, submit orders, exclude evidence, or override `rt_order_intake.py` execute gates.

`rt_signal_outcome_report.py` now also carries per-symbol missing K-line diagnostics with `minute_kline_count`, `latest_minute_date`, and `daily_refresh_gap` when a missing daily outcome symbol has newer minute data than daily data. When `/tmp/kline_daily_gap_repair.json` is available, each missing-symbol diagnostic is enriched with `daily_gap_repair_status=actionable`, `unresolved`, or `not_in_repair_plan`, plus the repair `plan_hash` and a compact action/unresolved reason. The top-level `kline_daily_gap_repair_context` summarizes how many outcome-blocking symbols are covered by a hash-confirmed action versus unresolved source/mapping issues.

When `/tmp/kline_gap_source_diagnostic_report.json` is available, `rt_signal_outcome_report.py` also enriches each missing-symbol diagnostic with `daily_gap_source_category`, `daily_gap_source_confidence`, `daily_gap_source_recommended_action`, and a compact `daily_gap_source_diagnostic`. The top-level `kline_gap_source_diagnostic_context` summarizes classified/unclassified missing symbols, category counts, confidence counts, and affected-signal counts. Recommendations such as `apply_reviewed_daily_gap_plan_for_outcome_symbols:N`, `review_unresolved_daily_gap_symbols_for_source_or_mapping:N`, and `review_active_universe_or_mapping_for_outcome_symbols:N` are diagnostic only. This does not apply DB repairs, does not deactivate stocks, does not change watchlists, does not exclude symbols from evidence, does not relax `rt_order_intake.py` evidence gates, and does not turn pending outcomes into resolved outcomes.

`rt_signal_outcome_report.py` also emits `intraday_signal_context` per evaluation and top-level `intraday_signal_context_summary`. This is no-lookahead minute evidence: for each v5 alert it reads only `klines.interval='min'` rows from the signal's own date at or before the alert `quote_time` or `generated_at`, then classifies session-to-signal, latest 5-minute, latest 15-minute, latest 30-minute, and latest 60-minute direction as `supports_signal`, `challenges_signal`, `conflicting_timeframes`, `neutral_or_insufficient`, or `unavailable_or_stale`. `strategy_learning_report.py` and `hermes_review_packet.py` normalize older labels such as `conflicting_intraday_context`, `insufficient_intraday_context`, and `missing_minute_rows_before_signal` into this canonical taxonomy when reading existing reports. The report also adds `by_intraday_signal_alignment` so Hermes can compare forward 1D/3D/5D returns by same-session confirmation or contradiction bucket. This is read-only learning evidence; it does not generate alerts, submit orders, relax readiness gates, or replace completed daily K-lines as the return authority.

For same-day target/stop ordering, `rt_signal_outcome_report.py` now requires full-OHLC minute path fidelity before resolving an `ambiguous_same_day` daily candle into `intraday_target` or `intraday_stop`. Broker/vendor-style minute rows may set `data_source` or `source_granularity` to values such as `broker_minute_ohlcv`, `vendor_minute_ohlcv`, or `minute_ohlcv`. Public snapshot rows such as `data_source=tencent_minute_query`, `source_granularity=minute_snapshot_price`, missing source provenance, or rows where OHLC fidelity is unverified are reported as `intraday_sequence.status=LOW_FIDELITY`. In that case the report may include `sampled_first_hit` for manual diagnosis, but `effective_first_hit` remains `ambiguous_intraday_low_fidelity`, the first-hit rates stay unresolved, and the recommendation `collect_full_ohlcv_minute_path_evidence:N` is emitted. This prevents Tencent-style one-price-per-minute snapshots from polluting strategy learning as if they were exchange-quality minute bars.

`source_reliability_report.py` also reads `/tmp/rt_signal_outcome_report.json` as a read-only `rt_signal_outcome` component. If `intraday_sequence_summary.low_fidelity_count`, `missing_count`, `ambiguous_count`, `unresolved_count`, or `primary_horizon_metric.effective_unresolved_first_hit_rate_pct >= 25` is present, the source matrix degrades with reasons such as `outcome_intraday_path_low_fidelity`, `outcome_intraday_path_missing_minute_rows`, and `outcome_intraday_path_high_unresolved_rate`. The component coverage includes compact path counts and `effective_first_hit_counts`. `hermes_review_packet.py` carries those counts into `context_digest.source_limits.source_reliability_problem_components[].intraday_path_fidelity` for `rt_signal_outcome`, so Hermes can cite the exact limitation in `source_reliability_reasons[]` before approving/reducing a signal.

This is the no-loss integration path for finer granularity data. `60m`, `30m`, `15m`, and `5m` evidence may be used as same-session context and learning features, while `1m` evidence should primarily resolve path sequencing and slippage diagnostics. None of those intervals replace daily K-lines as the return horizon authority, generate v5 alerts by themselves, submit simulation orders, relax order-intake gates, or rewrite strategy thresholds. Until a broker/vendor/official full-OHLCV source is proven and persisted with `source_granularity`, public snapshot minute rows stay advisory and must be acknowledged by Hermes as source-limited evidence.

`strategy_learning_report.py` carries the same `intraday_signal_alignment` into joined alert/judgment/intake/outcome rows and groups realized forward returns in `by_intraday_signal_alignment`. `hermes_review_packet.py` exposes a compact `strategy_learning_brief.intraday_signal_alignment` copy of those group metrics, while the full `strategy_learning.by_intraday_signal_alignment` remains authoritative for existing readers. Hermes should use this to learn whether 5m/15m/30m/60m contradictions actually hurt v5 outcomes before turning intraday context into a stronger hold/reduce rule. If `challenges_signal` has enough resolved samples and negative forward return, the report recommends `intraday_challenge_alignment_underperforms_consider_hermes_hold_rule`; if challenged signals are profitable, it recommends reviewing the intraday labeling or thresholds rather than blindly blocking them.

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
- closed-trade signal traceability from FIFO closed trades back to `/tmp/rt_order_intake_state.json`;
- recommendations such as keeping `alert-sim` disabled, repairing signal lineage, prioritizing high-risk position judgments, and inspecting worst closed symbols.
- a hash-stamped `remediation_plan` with schema `simulation_strategy_remediation_v1`, linking weak simulation evidence to manual review actions.

Default command:

```bash
/usr/bin/python3 /root/simulation_performance_report.py --output /tmp/simulation_performance_report.json --text
```

The output schema is `simulation_performance_report_v1`. `status=FAIL` means recent simulation behavior does not support new exposure: for example total simulation return is not positive, closed P&L is not positive, closed-trade win rate is too low, blocking simulation review notes are present, closed trades cannot be traced back to processed v5 intake/order decisions, or simulation portfolio risk is critical. `status=WARN` means the realized trade sample is acceptable but portfolio risk still requires manual/Hermes review. `status=OK` means the simulation performance attribution layer has no blocking or warning reason. This report is read-only: it does not submit orders, change strategy thresholds, repair positions, or apply watchlist/config proposals.

The traceability section has schema `simulation_closed_trade_signal_traceability_v1`. It reads only `/tmp/rt_order_intake_state.json` and matches FIFO closed-trade entry/exit `order_id` values to processed intake decisions with `order_result.order_id`/`id`. If recent closed trades exist but processed decisions are empty, the state file is missing, entry order IDs are absent, or entry order IDs do not match any processed signal, the report adds `closed_trade_signal_traceability_missing` and fails closed. Positive P&L without this linkage is not treated as upgrade evidence, because Hermes cannot prove which v5 signal, score, trigger, market context, source-quality state, or judgment produced the trade.

When simulation performance is weak, `remediation_plan.status=operator_review_required`. The plan is intentionally review-only and carries:

- `proposal_hash` as the stable review identifier;
- `manual_review_required=true` and `auto_applied=false`;
- an `operator_contract` proving it does not submit orders, change execution mode, change strategy config, change watchlists, change crontab, or repair positions;
- actions such as `keep_alert_sim_disabled`, `reject_or_hold_new_buy_by_default`, `repair_closed_trade_signal_lineage`, `require_position_judgments_for_high_priority_holdings`, `review_worst_closed_symbols_before_strategy_changes`, and `keep_strategy_changes_manual_and_shadow_only`.

Hermes may cite the remediation hash in a judgment or daily review, but it must not treat the plan as an applyable strategy proposal. Any real strategy config, watchlist, position-ledger, or execution-mode change still requires the separate hash-confirmed promotion or repair tools and a fresh readiness report.

`hermes_review_packet.py` reads this report into top-level `simulation_performance` when `/tmp/simulation_performance_report.json` exists. Hermes should use it as a hard critique layer before supporting new BUY exposure: positive paper signal outcomes are not enough when the simulation portfolio is losing money or recent closed trades are weak. When the report has `schema=simulation_performance_report_v1` and `status=FAIL`, `hermes_review_packet.py` marks new BUY review items as `eligible_for_approval=false`, adds `simulation_performance_fail`, the report `reason_codes`, and the `remediation_plan.proposal_hash` to `blocking_reasons`, and sets `recommended_judgment=reject_or_hold`. This packet-level gate is intentionally scoped to new BUY exposure; it does not block SELL/reduce review, does not change execution mode, does not submit orders, and does not mutate strategy, watchlist, portfolio, DB, or cron state.

For any `approve` or `reduce` judgment while `simulation_performance.status` is `WARN` or `FAIL`, Hermes must also provide structured evidence that it reviewed the realized simulation weakness:

- `simulation_performance_acknowledged=true`;
- `simulation_performance_status` matching the reviewed top-level report status;
- `simulation_performance_reason_codes[]` containing at least one reviewed `simulation_performance.reason_codes[]` value when reason codes are present;
- `simulation_performance_notes[]` explaining how realized simulation return, closed-trade P&L/win rate, open portfolio risk, or the remediation hash changed confidence, sizing, rejection, or hold logic.

`hermes_judgment_audit_report.py` fails missing or mismatched evidence with `missing_simulation_performance_acknowledgement`, `simulation_performance_status_missing`, `simulation_performance_status_mismatch`, `simulation_performance_reason_codes_missing_or_unmatched`, or `simulation_performance_notes_missing`. This keeps weak simulation feedback from becoming a passive dashboard field that Hermes can ignore.

`execution_readiness_report.py` also reads `/tmp/simulation_performance_report.json`. A `simulation_performance.status=FAIL` hard-blocks readiness, while `WARN` prevents `READY` until the operator/Hermes has reviewed the risk context. The readiness gate includes the `remediation_plan` so Hermes/operator can see the loss-recovery review hash without opening the full simulation report.

Recommended read-only cron:

```bash
*/30 * * * * /usr/bin/python3 /root/simulation_performance_report.py --output /tmp/simulation_performance_report.json --text >> /tmp/simulation_performance_report.log 2>&1
*/30 * * * * /usr/bin/python3 /root/simulation_postmortem_audit_report.py --output /tmp/simulation_postmortem_audit_report.json --text >> /tmp/simulation_postmortem_audit_report.log 2>&1
*/30 * * * * /usr/bin/python3 /root/simulation_postmortem_note_draft_report.py --output /tmp/simulation_postmortem_note_draft_report.json --text >> /tmp/simulation_postmortem_note_draft_report.log 2>&1
```

`scripts/simulation_postmortem_audit_report.py` closes the learning loop for failed simulation performance without changing strategy automatically. It reads `/tmp/simulation_performance_report.json` and `/tmp/simulation_postmortem_notes.jsonl`, then checks whether every negative `worst_closed_symbols[]` row and every high-priority `open_position_risk[]` row has a matching `simulation_trade_postmortem_note_v1`.

Each note is an append-only JSONL audit artifact. It must include the reviewed symbol, target type (`closed_trade` or `open_position`), failure category, market/intraday/event/fundamental/source-reliability context status, a concrete lesson, any proposed change, and a `promotion_gate` that keeps strategy/watchlist/config changes manual and hash-confirmed. The note must also set `read_only=true`, `submits_orders=false`, `changes_strategy=false`, `changes_portfolio=false`, and `auto_apply=false`.

`scripts/simulation_postmortem_note_draft_report.py` is a read-only draft helper for Hermes/operator. It reads the postmortem audit, simulation performance, and current market/intraday/news/event/sentiment/fundamentals/source-reliability context, then emits `/tmp/simulation_postmortem_note_draft_report.json` with one draft `simulation_trade_postmortem_note_v1` object per missing target. Drafts intentionally include `draft_only=true` and `<replace:...>` placeholders. They are not valid notes, not judgments, and not strategy changes. Hermes/operator must replace every placeholder, remove `draft_only`, append one completed JSON object per line to `/tmp/simulation_postmortem_notes.jsonl`, and rerun `simulation_postmortem_audit_report.py`. The audit rejects unreplaced placeholders and `draft_only=true`, so a raw draft cannot accidentally satisfy the readiness gate.

Audit status:

- `OK`: all required targets are covered by PASS notes, or no postmortem is required.
- `WARN`: required notes are missing.
- `FAIL`: at least one submitted note is incomplete, unsafe, unmatched, or claims side effects.

The report emits `note_contract.append_jsonl_object` so Hermes/operator can write valid notes without guessing the schema. `operator_action_queue_report.py` reads `/tmp/simulation_postmortem_audit_report.json` and `/tmp/simulation_postmortem_note_draft_report.json`; `WARN` or `FAIL` creates `write_or_repair_simulation_postmortem_notes`, asking Hermes/operator to complete or repair notes before treating loss-recovery work as reviewed. `execution_readiness_report.py` also reads the same audit report. Only `OK` or `PASS` passes readiness; `WARN`, `FAIL`, `MISSING`, stale, or invalid postmortem-audit evidence keeps the aggregate readiness report `BLOCKED` because simulation losses or high-risk open holdings have not been fully reviewed. This remains a review artifact only: it does not submit orders, mutate portfolios, alter strategy settings, or promote watchlists/configs.

### Execution readiness dashboard

`scripts/execution_readiness_report.py` is a read-only dashboard that combines the main safety evidence Hermes and the operator need before considering any future execute-mode change:

- system health;
- data health;
- market context freshness and regime/risk state;
- external news/macro/event/capital-flow context;
- watchlist-linked event catalyst context;
- event-driven catalyst review signals linked to v5 alerts;
- intraday minute-bar confirmation/contradiction context for the v5 watchlist;
- quantified market sentiment and capital-flow context;
- trusted-source preflight status for Wudao/broker/official/context payloads;
- resolved forward outcome sample;
- dry-run daily K-line gap repair status and unresolved source/mapping issues;
- daily K-line gap source/mapping/universe diagnostic status;
- directional intake coverage;
- simulation portfolio risk and reconciliation state;
- watchlist proposal remediation state;
- Hermes judgment audit status;
- Hermes advisory position judgment audit status;
- simulation performance attribution status;
- simulation postmortem audit status;
- cron wiring audit status;
- source reliability status;
- alert quality status.

Default command:

```bash
/usr/bin/python3 /root/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text
```

The output schema is `execution_readiness_report_v1`. `status=BLOCKED` means at least one hard gate is missing or failing, including stale or missing report timestamps, missing system health, missing data health, missing/invalid market context, unsafe or failed intraday K-line batch safety contract, failed intraday-context report status, failed external-context producer/parser status, failed event-catalyst parser/upstream status, failed market-sentiment parser/upstream status, failed fundamentals parser/upstream status, failed trusted-source preflight schema/status, failed source reliability matrix status, failed or unsafe daily-gap repair contract, unsafe daily-gap source diagnostic contract, unsafe alternate-provider probe contract, unsafe alternate-provider repair-plan contract, failed/invalid/unsafe event-store durability reports, Hermes judgment event-store rows with `audit_status=FAIL`, failed Hermes trade judgment audit, failed Hermes advisory position judgment audit, failed simulation performance attribution, missing/incomplete/unsafe simulation postmortem audit coverage, insufficient resolved outcomes, missing/non-positive average signed forward return, missing/weak win rate, weak target-vs-stop hit evidence, weak favorable/adverse excursion evidence, low directional intake coverage, insufficient Hermes judgment-effect evidence, missing simulation portfolio risk context, or failed simulation reconciliation. `status=WARN` means no hard gate failed, but manual review is still required, for example when market context is `risk_off`/high-risk, when intraday K-line batch is missing/stale/`ACTIONABLE`/`PARTIAL`/`UNRESOLVED`, when intraday context is missing/stale or has stale/missing watchlist-symbol coverage, when external news/macro/event context is missing/stale/risky, when watchlist-linked event catalysts are missing/stale/risky, when quantified market sentiment is missing/stale/risk-off, when fundamentals context is missing/stale/risky, when trusted-source preflight is `WARN`, `MISSING`, or stale, when source reliability is `DEGRADED`, `STALE`, or `MISSING`, when daily-gap repair is `ACTIONABLE`, `PARTIAL`, `UNRESOLVED`, or `WARN`, when daily-gap source diagnostic is `REVIEW`, `ACTION_REQUIRED`, `WARN`, or missing, when alternate-provider probe is `REVIEW`, `ACTION_REQUIRED`, `WARN`, or missing, when alternate-provider repair plan is `REVIEW`, `ACTION_REQUIRED`, `WARN`, or missing, when event-store durability reports are missing or still in safe `dry_run`, when Hermes judgment event-store audit coverage is missing/truncated or has unmatched judgment rows, when advisory position judgment audit is missing/unknown, when simulation performance attribution is missing/unknown/warning, or when a watchlist proposal covers sizing blockers but has not been reviewed/applied/restarted. `status=READY` is necessary context only; it does not enable bridge execution, does not submit orders, and does not override `rt_order_intake.py` execute gates or matching Hermes trade judgments.

By default, all critical input reports, including `/tmp/market_context_report.json`, `/tmp/external_market_context_report.json`, `/tmp/event_catalyst_report.json`, `/tmp/event_catalyst_signal_report.json`, `/tmp/market_sentiment_report.json`, `/tmp/fundamentals_context_report.json`, `/tmp/cron_audit_report.json`, `/tmp/source_reliability_report.json`, `/tmp/rt_alert_event_store_report.json`, `/tmp/hermes_judgment_event_store_report.json`, `/tmp/rt_order_intake_event_store_report.json`, `/tmp/rt_signal_outcome_event_store_report.json`, `/tmp/kline_daily_gap_repair.json`, `/tmp/kline_gap_source_diagnostic_report.json`, `/tmp/kline_gap_alternate_provider_probe.json`, `/tmp/kline_gap_alternate_provider_repair_plan.json`, `/tmp/watchlist_diff_report.json`, `/tmp/simulation_performance_report.json`, `/tmp/simulation_postmortem_audit_report.json`, and `/tmp/hermes_position_judgment_audit_report.json`, must have a recognized timestamp and be no older than 90 minutes. `/tmp/trusted_source_preflight_report.json` has its own gate so a new deployment without trusted payloads starts at `WARN` rather than a hard freshness block, while stale OK preflight still prevents `READY`. `/tmp/intraday_kline_batch.json` and `/tmp/intraday_context_report.json` are checked by their own `intraday_kline_batch` and `intraday_context` gates rather than the global hard freshness gate, because minute bars are supplemental timing evidence and an unapplied producer plan is not proof of DB coverage. The `report_freshness` gate includes `data.refresh_remediation` with schema `readiness_report_freshness_remediation_v1`: a read-only `/root/readiness_refresh.py --only ...` command, fallback full-refresh command, affected reports, and guarantees that the remediation does not submit orders, edit crontab, use `--apply`, or run execute mode. Hermes may cite that command for an operator, but must not treat stale evidence as approved.

Market context with `risk_off` regime or `high` risk level prevents `READY` and requires stricter manual/Hermes review; `rt_order_intake.py` remains the authoritative execute gate for market-regime exceptions. Intraday context with `MISSING`, stale report timestamp, or stale/missing watchlist-symbol coverage prevents `READY` as a warning because Hermes lacks current same-session timing/path evidence; `FAIL` hard-blocks readiness because the intraday report itself is broken. External context with `MISSING`, `STALE`, or `RISK` prevents `READY` because Hermes current-event awareness is incomplete or contains high-impact risk; `FAIL` hard-blocks readiness. Event catalysts with `MISSING`, `STALE`, or `RISK` prevents `READY` because Hermes watchlist-event awareness is incomplete or contains high-impact negative catalysts; `FAIL` hard-blocks readiness. Event catalyst signals with `MISSING`, `STALE`, or `RISK` prevent `READY` because event-driven support/challenge evidence is incomplete or contradicts related technical alerts; `FAIL` hard-blocks readiness. Market sentiment with `MISSING`, `STALE`, or `RISK` prevents `READY` because quantified volatility/capital-flow/risk-appetite awareness is incomplete or risk-off; `FAIL` hard-blocks readiness. Fundamentals context with `MISSING`, `STALE`, or `RISK` prevents `READY` because valuation/profitability/growth/leverage awareness is incomplete or risky. Trusted-source preflight with `WARN`, `MISSING`, or stale status prevents `READY` because Hermes cannot yet prove structured Wudao/broker/official source coverage; `FAIL` hard-blocks readiness because payloads are invalid and should not be cited. Source reliability with `DEGRADED`, `STALE`, or `MISSING` prevents `READY` because a downstream context report may be fresh while its provider, cron, provenance, or metric coverage is weak; `FAIL` hard-blocks readiness. Daily-gap repair with `ACTIONABLE`, `PARTIAL`, `UNRESOLVED`, or `WARN` prevents `READY` because daily K-line evidence still needs manual repair or source/mapping review; an unsafe apply contract hard-blocks readiness. Daily-gap source diagnostic with `REVIEW`, `ACTION_REQUIRED`, `WARN`, or `MISSING` prevents `READY` because unresolved symbols have not been classified cleanly enough for evidence trust; an unsafe diagnostic contract hard-blocks readiness. Alternate-provider probe with `REVIEW`, `ACTION_REQUIRED`, `WARN`, or `MISSING` prevents `READY` because unresolved source/provider disagreement has not been manually reviewed; an unsafe probe contract hard-blocks readiness. Alternate-provider repair plan with `REVIEW`, `ACTION_REQUIRED`, `WARN`, or `MISSING` prevents `READY` because alternate rows still require quality review and independent comparison before any DB repair; any non-null apply command or unsafe contract hard-blocks readiness. Cron audit with `WARN` prevents `READY` because required read-only context jobs are missing from the actual crontab and may explain stale reports; `FAIL` hard-blocks readiness because a dangerous execution cron such as `alert-sim`, `legacy-sim`, or direct execute mode is enabled. Event-store durability with missing or `dry_run` reports prevents `READY` because alert, judgment, intake, and outcome histories are not yet durably queryable; failed/blocked/invalid event-store reports, wrong schemas, unsafe safety contracts, or persisted audit-failed Hermes judgment rows hard-block readiness. Missing or truncated Hermes judgment event-store audit coverage prevents `READY` as a warning because applied history is not yet fully auditable. Fresh watchlist diff context is required because sizing-blocker remediation may depend on a specific hash-stamped manual watchlist proposal. The forward-evidence gate requires at least five resolved signals, positive average signed forward return, win rate above 50%, stop hit rate not exceeding target hit rate, stop hit rate not above 50%, and favorable/adverse excursion ratio above 1. The `outcome_path_quality` gate is warning-only: if effective stop-first rate exceeds target-first rate, if too many first-hit cases remain unresolved after minute lookup, or if any same-day first-hit cases are `ambiguous_intraday_low_fidelity`, readiness cannot be `READY` until Hermes/operator reviews whether the strategy is being stopped out before reward or whether full-OHLC minute path evidence is still missing. The Hermes judgment-effect gate also requires at least five resolved approved/reduced judgments, at least five resolved rejected/held judgments for comparison, positive approved/reduced average return, approved/reduced win rate above 50%, and approved/reduced average return above rejected/held average return. The advisory position-judgment audit gate requires `status=OK` or `PASS`; `status=FAIL` blocks readiness because user/simulation holding reviews would no longer be safely advisory and auditable. The simulation performance attribution gate requires `status=OK` or `PASS`; `status=FAIL` blocks readiness because the realized simulation portfolio evidence contradicts new exposure. The simulation postmortem audit gate requires `status=OK` or `PASS`; `WARN`, `FAIL`, `MISSING`, or stale audit output blocks readiness because required loss/high-risk holding lessons are not fully recorded in safe append-only notes. The simulation portfolio-performance gate requires positive total return versus initial capital and unrealized PnL not below -5% of cost. The simulation trade-review gate requires at least three closed simulation trades, positive estimated closed PnL, closed win rate above 50%, and no blocking review notes such as `recent_closed_trades_negative` or `loss_rate_above_60pct`. The thresholds are explicit CLI/env settings:

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

### Manual readiness refresh

`scripts/readiness_refresh.py` refreshes the main read-only evidence reports in dependency order, including data-source inventory, K-line source-granularity provenance proposals, trusted-source preflight, intraday K-line batch dry-run planning, intraday context, intraday timeframe quality, intraday market-session override validation, source reliability, daily-gap repair, universe hygiene, daily-gap source diagnostics, alternate-provider probes, alternate-provider repair-candidate plans, simulation performance, simulation postmortem audit, simulation postmortem note drafts, and the four event-store durability dry-run reports, then regenerates `/tmp/execution_readiness_report.json`, seeds `/tmp/hermes_signal_review_packet.json` once with `--no-archive`, regenerates `/tmp/operator_action_queue_report.json`, and finally regenerates `/tmp/hermes_signal_review_packet.json` again so the final packet carries the latest remediation queue. It is useful when live cron wiring is incomplete or after deploying a new read-only context producer.

It is intentionally not an execution bridge:

- it does not edit crontab;
- it does not submit orders;
- it does not use `--apply`, including for event-store durability reports;
- it does not run `rt_order_intake.py --mode execute`;
- `hermes_review_packet.py` is run with `--ephemeral-state`.

Default command:

```bash
/usr/bin/python3 /root/readiness_refresh.py --output /tmp/readiness_refresh_report.json --text
```

If public RSS/Yahoo producers are slow or unavailable, refresh only local reports and existing producer outputs:

```bash
/usr/bin/python3 /root/readiness_refresh.py --skip-network-producers --output /tmp/readiness_refresh_report.json --text
```

The output schema is `readiness_refresh_report_v1`. `status=OK` means every selected read-only refresh command exited successfully. `status=FAIL` means at least one selected refresh step failed; inspect `failed_steps[]`. `status=DRY_RUN` is available with `--dry-run` and only prints the planned commands.

Dependency order matters: `data_source_inventory_report.py` and `kline_source_granularity_report.py` run after data health and before `source_reliability_report.py`, so the source matrix can distinguish unavailable evidence, weak provenance, and pending provenance-only DB proposals. `trusted_source_preflight.py` runs after external context, market sentiment, and fundamentals inputs/reports have been refreshed; `intraday_kline_batch.py` runs before `intraday_context_report.py`, but it is still dry-run and does not populate DB minute rows without a separate hash-confirmed manual apply; `intraday_timeframe_quality_report.py` runs after `intraday_context_report.py` and before `source_reliability_report.py`, because it summarizes whether 5m/15m/30m/60m evidence is complete enough to confirm or only strong enough to cap confidence; daily-gap repair planning, universe hygiene, daily-gap source diagnostics, alternate-provider probes, and alternate-provider repair plans run before `rt_signal_outcome_report.py`, because outcome evidence annotates missing symbols with the latest repair/source classification; `rt_signal_outcome_report.py` then runs before `source_reliability_report.py`, because the source matrix also includes outcome path fidelity and missing-outcome diagnostics. `source_reliability_report.py` therefore sees current data health, data-source inventory, K-line source-granularity proposal state, market context, intraday context, intraday timeframe quality, external context, event catalysts, sentiment, fundamentals, trusted-source preflight/discovery, cron audit, and forward-outcome evidence. `hermes_judgment_audit_report.py` runs before `strategy_learning_report.py`, so learning uses same-refresh audit-pass cohorts instead of stale raw decisions. `simulation_postmortem_audit_report.py` runs after `simulation_performance_report.py` and before `execution_readiness_report.py`, so readiness is blocked when failed simulation performance has not been covered by safe postmortem notes. `simulation_postmortem_note_draft_report.py` runs after the audit and before `operator_action_queue_report.py`, so the queue can show current note drafts without treating drafts as completed notes. Finally `execution_readiness_report.py`, `hermes_review_packet.py --no-archive`, `operator_action_queue_report.py`, and the final `hermes_review_packet.py` run after source reliability, strategy learning, and postmortem audit. This prevents Hermes from receiving a newly generated packet that contains a stale source-reliability matrix, stale outcome diagnostics, stale judgment-audit learning summary, missing loss-review coverage, or stale remediation queue.

The full refresh external-context producer includes the local Info Hub bridge by default:

```bash
/usr/bin/python3 /root/external_market_context_producer.py --include-infohub --infohub-url http://127.0.0.1:8899 --output /tmp/external_market_context_inputs.json --text
```

Those items remain tagged as `infohub_public_rss_bridge` and are still counted as fallback source coverage, not trusted Wudao/broker context. Use `--skip-network-producers` when only refreshing reports from already-produced inputs.

### Strategy config proposal

`scripts/strategy_config_proposal.py` converts `strategy_review_report.py` output into a candidate `rt_signal_strategy_config_v1` file for human review.

Default command:

```bash
/usr/bin/python3 /root/strategy_config_proposal.py --output /tmp/rt_signal_strategy_config_proposal.json --text
```

Optional explicit review inputs:

```bash
/usr/bin/python3 /root/strategy_config_proposal.py \
  --simulation-performance-file /tmp/simulation_performance_report.json \
  --execution-readiness-file /tmp/execution_readiness_report.json \
  --strategy-learning-file /tmp/strategy_learning_report.json \
  --max-simulation-performance-age-minutes 90 \
  --output /tmp/rt_signal_strategy_config_proposal.json \
  --text
```

It proposes:

- `enabled=false` for triggers with `disable_execution_review`;
- tighter per-trigger `min_full_score` or `max_full_score` for `tighten_thresholds`;
- `review_mode=shadow_only_pending_sample` for `shadow_only`.

Important behavior:

- it reads the current `/root/rt_signal_strategy_config.json`;
- it reads `/tmp/simulation_performance_report.json` as review context when present;
- it reads `/tmp/execution_readiness_report.json` as a full-stack readiness guard;
- it reads `/tmp/strategy_learning_report.json` as the audit-pass Hermes learning guard;
- it writes only `/tmp/rt_signal_strategy_config_proposal.json`;
- it never overwrites the live strategy config;
- proposal output has `manual_review_required=true` and `auto_applied=false`;
- promotion requires the separate hash-confirmed `strategy_config_promote.py` flow and an explicit restart request.
- after a promoted config is loaded by `rt_signal_engine_v5.py`, `review_mode=shadow_only_pending_sample` is enforced as WATCH-only output: the original BUY/SELL stays in `candidate_signal_type`, `execution_candidate=false`, and Hermes receives the evidence for review without a directional trade candidate.

Simulation-performance integration is additive and lossless for v5:

- `proposed_config` remains a normalized `rt_signal_strategy_config_v1` object. Older review consumers can ignore unknown trigger fields, while v5 uses `review_mode` to enforce shadow-only trigger policies without changing order intake, DB rows, watchlists, crontab, or execution mode;
- proposal output now includes `simulation_performance_context` with effective `status`, raw `report_status`, compact portfolio/trade summary, `reason_codes`, recommendations, `remediation_plan.proposal_hash`, remediation `action_ids`, and `freshness`;
- proposal output also includes `execution_readiness_context` and `strategy_learning_context`. These are compact read-only summaries and do not change alert generation by themselves;
- `status=FAIL` adds `promotion_blockers[].code=simulation_performance_fail_blocks_strategy_promotion` and sets `promotion.blocked=true`;
- `status=WARN` adds `promotion_risk_warnings[].code=simulation_performance_warn_requires_operator_review` without blocking dry-run review;
- missing context adds `promotion_blockers[].code=simulation_performance_missing_blocks_strategy_promotion`;
- stale or invalid-timestamp context adds `promotion_blockers[].code=simulation_performance_stale_blocks_strategy_promotion`, even if the stale report's raw `report_status` is `OK`;
- unknown non-clean context adds `promotion_blockers[].code=simulation_performance_unknown_status_blocks_strategy_promotion`;
- execution readiness not equal to `READY`, or `ready_for_execute` not equal to `true`, adds `promotion_blockers[].code=execution_readiness_not_ready_blocks_strategy_promotion`;
- missing strategy learning adds `promotion_blockers[].code=strategy_learning_missing_blocks_strategy_promotion`;
- missing `audit_pass_judgment_effect` adds `promotion_blockers[].code=strategy_learning_audit_pass_effect_missing_blocks_strategy_promotion`; raw `judgment_effect` is diagnostic only and must not support promotion;
- missing/truncated judgment-audit coverage or nonzero audit failed/missing counts add judgment-audit promotion blockers;
- too-small audit-pass approved/reduced or rejected/held samples add `promotion_blockers[].code=strategy_learning_audit_pass_sample_too_small_blocks_strategy_promotion`;
- the default freshness window is 90 minutes, inherited from `EXECUTION_READINESS_MAX_REPORT_AGE_MINUTES` unless `STRATEGY_CONFIG_PROPOSAL_MAX_SIMULATION_PERFORMANCE_AGE_MINUTES` or `--max-simulation-performance-age-minutes` is set;
- none of these fields change alert generation, Hermes eligibility, thresholds, watchlists, crontab, or execution mode.

Hermes/operator interpretation:

- do not review threshold changes in isolation from the 100k HKD simulation ledger;
- if `promotion_blockers` is non-empty, treat the strategy proposal as context only and require later recovery evidence from simulation performance, forward outcomes, execution readiness, and audit-pass Hermes judgment-effect reports before promotion;
- cite `simulation_performance_context.remediation_plan.proposal_hash` when discussing loss-recovery actions;
- use `proposed_config` only as the candidate config payload after all blockers are clear and the promotion tool validates the hash.

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
- refuses apply when proposal `promotion_blockers` are present, including `simulation_performance_fail_blocks_strategy_promotion`;
- refuses apply when simulation-performance context is missing, stale, invalid-timestamp, or unknown via the corresponding `promotion_blockers`;
- refuses apply when execution readiness is not `READY`, strategy learning is missing, audit-pass Hermes judgment-effect evidence is missing/too small, or judgment-audit coverage is failed/missing/truncated via the corresponding `promotion_blockers`;
- backs up the target under `/tmp/rt_signal_strategy_config_backups/`;
- does not restart `rt_signal_engine_v5.service` unless `--restart-service` is present.

This preserves the existing no-loss integration path: old consumers can keep reading `proposed_config` and `proposal_hash`, while newer Hermes/promote consumers get the extra simulation, readiness, and audit-pass learning guards. The guards are fail-closed only at the manual promotion layer; proposal generation stays read-only and continues to produce reviewable diffs.

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
- Closed-trade learning now requires `sim_trades.order_id` to line up with `/tmp/rt_order_intake_state.json` processed decisions. The current server may still have many dry-runs and no processed execution records, so this gate can legitimately block strategy promotion until alert-sim execution is intentionally enabled and lineage is retained.
- The server can show `positions` rows lagging behind `sim_trades`; `portfolio_risk` now detects this, but it does not repair the backend position ledger.
- `sim_position_reconcile.py` can repair portfolio 8 positions from `sim_trades`, but it should stay hash-gated and manually reviewed until the backend API's own position update path is understood.
- `rt_signal_outcome_report.py` still uses daily K-lines for return and horizon authority. Minute bars now help resolve same-day stop/target ordering and classify no-lookahead signal-time 5m/15m/30m/60m context for learning, but full execution-quality replay remains incomplete.
- `market_context_report.py` uses stock-pool breadth because no reliable index/ETF K-lines were available in the current database.
- `hermes_judgment_audit_report.py` relies on retained packet archives under `/tmp/hermes_review_packet_archive`; keep an eye on retention if Hermes needs long-horizon judgment QA.
