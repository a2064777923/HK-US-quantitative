#!/usr/bin/env python3
"""Collect Tencent snapshot-like minute rows into the K-line table.

Tencent's public minute endpoint is not a full exchange-grade OHLCV source.
Rows written by this collector are therefore labelled as
``source_granularity='minute_snapshot_price'`` when the DB schema supports it.
"""

import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime

DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
DATA_SOURCE = "tencent_min"
SOURCE_GRANULARITY = "minute_snapshot_price"


def db(sql, timeout=30):
    result = subprocess.run(
        [
            "docker",
            "exec",
            DB_CONTAINER,
            "psql",
            "-U",
            DB_USER,
            "-d",
            DB_NAME,
            "-t",
            "-A",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def sql_quote(value):
    return str(value).replace("'", "''")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def table_columns(table):
    raw = db(
        f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = '{sql_quote(table)}'
        """
    )
    return {line.strip() for line in raw.splitlines() if line.strip()}


def fetch_minute_data(symbol, market="hk"):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={market}{symbol}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
        return data.get("data", {}).get(f"{market}{symbol}", {}).get("data", {}).get("data", [])
    except Exception:
        return []


def quote_literal(value):
    return f"'{sql_quote(value)}'"


def row_values(symbol, point, date_str, prev_price, include_source_granularity):
    parts = str(point).split()
    if len(parts) < 2:
        return None, prev_price
    minute = parts[0]
    try:
        price = float(parts[1])
    except ValueError:
        return None, prev_price
    if price <= 0 or len(minute) < 4:
        return None, prev_price

    hour, minute_part = minute[:2], minute[2:4]
    timestamp = f"{date_str} {hour}:{minute_part}:00"
    volume = 0.0
    if len(parts) >= 3:
        try:
            volume = float(parts[2])
        except ValueError:
            volume = 0.0
    change_pct = (price / prev_price - 1) * 100 if prev_price and prev_price > 0 else 0.0
    values = [
        quote_literal(symbol),
        "'min'",
        quote_literal(timestamp),
        f"{price}",
        f"{price}",
        f"{price}",
        f"{price}",
        f"{volume}",
        f"{price * volume}",
        f"{change_pct:.4f}",
        quote_literal(DATA_SOURCE),
    ]
    if include_source_granularity:
        values.append(quote_literal(SOURCE_GRANULARITY))
    values.append("NOW()")
    return "(" + ",".join(values) + ")", price


def save_minute_klines(symbol, points, date_str, kline_columns=None):
    if not points:
        return 0
    kline_columns = set(kline_columns or table_columns("klines"))
    include_source_granularity = "source_granularity" in kline_columns
    rows = []
    prev_price = None
    for point in points:
        row, prev_price = row_values(symbol, point, date_str, prev_price, include_source_granularity)
        if row:
            rows.append(row)
    if not rows:
        return 0

    provenance_column = ",source_granularity" if include_source_granularity else ""
    provenance_update = (
        ", source_granularity = EXCLUDED.source_granularity"
        if include_source_granularity
        else ""
    )
    sql = f"""
        INSERT INTO klines (
            symbol,interval,timestamp,open_price,high_price,low_price,close_price,
            volume,amount,change_percent,data_source{provenance_column},created_at
        )
        VALUES {','.join(rows)}
        ON CONFLICT (symbol, interval, timestamp) DO UPDATE
        SET open_price = EXCLUDED.open_price,
            high_price = EXCLUDED.high_price,
            low_price = EXCLUDED.low_price,
            close_price = EXCLUDED.close_price,
            volume = EXCLUDED.volume,
            amount = EXCLUDED.amount,
            change_percent = EXCLUDED.change_percent,
            data_source = EXCLUDED.data_source{provenance_update},
            created_at = NOW();
    """
    path = "/tmp/min_klines.sql"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"BEGIN;\n{sql}\nCOMMIT;\n")
    subprocess.run(["docker", "cp", path, f"{DB_CONTAINER}:/tmp/min_klines.sql"], capture_output=True, timeout=30)
    subprocess.run(
        ["docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-f", "/tmp/min_klines.sql"],
        capture_output=True,
        timeout=30,
    )
    return len(rows)


def load_active_stocks():
    raw = db(
        """
        SELECT symbol, exchange
        FROM stocks
        WHERE is_active = true
          AND exchange IN ('HKEX','NASDAQ','NYSE')
        ORDER BY symbol
        """,
        timeout=60,
    )
    stocks = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) >= 2:
            stocks.append((parts[0], parts[1]))
    return stocks


def main():
    log("=" * 50)
    log("分鐘K線數據收集")
    stocks = load_active_stocks()
    log(f"股票池: {len(stocks)} 隻")
    today = datetime.now().strftime("%Y-%m-%d")
    total_saved = 0
    kline_columns = table_columns("klines")
    if "source_granularity" in kline_columns:
        log(f"source_granularity={SOURCE_GRANULARITY}")

    for idx, (symbol, exchange) in enumerate(stocks, start=1):
        market = "hk" if exchange == "HKEX" else "us"
        points = fetch_minute_data(symbol, market)
        if points:
            total_saved += save_minute_klines(symbol, points, today, kline_columns=kline_columns)
        if idx % 50 == 0:
            log(f"  進度: {idx}/{len(stocks)} (已存{total_saved}條)")
        time.sleep(0.15)

    log(f"完成: {len(stocks)} 隻, 共存 {total_saved} 條分鐘K線")
    count = db("SELECT count(*) FROM klines WHERE interval='min'", timeout=60)
    log(f"DB分鐘K線總數: {count}")


if __name__ == "__main__":
    main()
