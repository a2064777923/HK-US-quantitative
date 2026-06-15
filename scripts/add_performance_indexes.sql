-- Performance indexes for QuantMind
-- Date: 2026-06-15
-- Purpose: Optimize slow queries in signal_engine_v4

-- 1. Covering index for daily klines (symbol + timestamp DESC)
-- Used by: latest_kline_date(), candidate_stocks_for_date()
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_klines_day_symbol_ts
ON klines (symbol, timestamp DESC) WHERE interval = 'day';

-- 2. Stocks table: active exchange filter
-- Used by: all queries that filter on is_active + exchange
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_stocks_active_exchange
ON stocks (exchange, is_active) WHERE is_active = true;

-- 3. Verify indexes exist
SELECT tablename, indexname FROM pg_indexes
WHERE tablename IN ('klines', 'stocks')
  AND indexname LIKE 'idx_%'
ORDER BY tablename, indexname;
