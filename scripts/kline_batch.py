#!/usr/bin/env python3
"""
K線批量更新 — 每隻股票一次SQL寫入所有K線（唔再逐條INSERT）
"""
import argparse, errno, subprocess, json, time, urllib.request, tempfile, os, sys
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python versions without zoneinfo
    ZoneInfo = None

DEFAULT_FETCH_COUNT = int(os.environ.get("KLINE_BATCH_FETCH_COUNT", "120"))
ALLOW_MULTI_SYMBOL_TRANSACTION = os.environ.get("KLINE_BATCH_ALLOW_MULTI_SYMBOL_TRANSACTION", "0") == "1"
LOCK_FILE = os.environ.get("KLINE_BATCH_LOCK_FILE", "/tmp/kline_batch.lock")
MIN_SUCCESS_RATE = float(os.environ.get("KLINE_BATCH_MIN_SUCCESS_RATE", "0.80"))
MARKET_TIMEZONE = os.environ.get("KLINE_BATCH_MARKET_TIMEZONE", "Asia/Hong_Kong")
US_DAILY_COMPLETE_TIME = os.environ.get("KLINE_BATCH_US_DAILY_COMPLETE_TIME", "05:15")
US_INTRADAY_BLOCK_START_TIME = os.environ.get("KLINE_BATCH_US_INTRADAY_BLOCK_START_TIME", "20:30")
ALLOW_US_INTRADAY_DAILY = os.environ.get("KLINE_BATCH_ALLOW_US_INTRADAY_DAILY", "0") == "1"


class AlreadyRunning(RuntimeError):
    pass


def lock_handle(handle):
    """Acquire a non-blocking platform lock for a held-open file handle."""
    try:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("another kline_batch instance holds the lock") from exc
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AlreadyRunning("another kline_batch instance holds the lock") from exc
            raise
        return "fcntl"
    except ImportError:
        pass

    try:
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise AlreadyRunning("another kline_batch instance holds the lock") from exc
        return "msvcrt"
    except ImportError:
        return "none"


def unlock_handle(handle, backend):
    if backend == "fcntl":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif backend == "msvcrt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class SingleInstanceLock:
    def __init__(self, path=LOCK_FILE, enabled=True):
        self.path = path
        self.enabled = enabled
        self.handle = None
        self.backend = "disabled"

    def __enter__(self):
        if not self.enabled:
            return self
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.handle = open(self.path, "a+", encoding="utf-8")
        try:
            self.backend = lock_handle(self.handle)
        except Exception:
            self.handle.close()
            self.handle = None
            raise
        if self.backend == "none":
            log("  警告: 當前平台不支援文件鎖，繼續執行")
            return self
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()} acquired_at={datetime.now().isoformat(timespec='seconds')}\n")
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return False
        try:
            if self.backend not in ("disabled", "none"):
                unlock_handle(self.handle, self.backend)
        finally:
            self.handle.close()
        return False

def db(sql):
    r = subprocess.run(
        ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-t', '-A', '-c', sql],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip()

def db_batch(sql_file):
    """Execute SQL file inside container"""
    # Copy file to container then execute
    r = subprocess.run(
        ['docker', 'cp', sql_file, 'quantmind-db:/tmp/batch.sql'],
        capture_output=True, timeout=10
    )
    if r.returncode != 0:
        return type("Result", (), {"returncode": r.returncode, "stdout": "", "stderr": r.stderr.decode("utf-8", "ignore")})()
    r2 = subprocess.run(
        ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-f', '/tmp/batch.sql'],
        capture_output=True, text=True, timeout=60
    )
    return r2

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def market_now(now=None):
    if now is not None:
        if getattr(now, "tzinfo", None) is not None:
            return now.astimezone(market_tzinfo()).replace(tzinfo=None)
        return now
    return datetime.now(market_tzinfo()).replace(tzinfo=None)


def market_tzinfo():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(MARKET_TIMEZONE)
        except Exception:
            pass
    return None


def parse_hhmm_minutes(value):
    try:
        hour, minute = str(value).split(":", 1)
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return 0


def minutes_since_midnight(value):
    return value.hour * 60 + value.minute


def us_daily_fetch_block(now=None):
    """Avoid writing current-session US daily rows before the close is complete."""
    if ALLOW_US_INTRADAY_DAILY:
        return False, ""
    current = market_now(now)
    minute = minutes_since_midnight(current)
    block_start = parse_hhmm_minutes(US_INTRADAY_BLOCK_START_TIME)
    complete = parse_hhmm_minutes(US_DAILY_COMPLETE_TIME)
    if minute >= block_start or minute < complete:
        return True, (
            "us_current_session_daily_fetch_blocked:"
            f"safe_after={US_DAILY_COMPLETE_TIME},block_start={US_INTRADAY_BLOCK_START_TIME}"
        )
    return False, ""


def market_refresh_ok(ok, fail, min_success_rate=None):
    if min_success_rate is None:
        min_success_rate = MIN_SUCCESS_RATE
    total = int(ok or 0) + int(fail or 0)
    if total <= 0:
        return False, 0.0
    success_rate = ok / total
    return ok > 0 and success_rate >= min_success_rate, success_rate

def fetch_kline(code, market="hk", count=DEFAULT_FETCH_COUNT):
    param = f"{market}{code},day,,,{count},qfq"
    url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={param}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.qq.com"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        if data.get("code") != 0: return None
        key = f"{market}{code}"
        sd = data.get("data",{}).get(key,{})
        return sd.get("qfqday") or sd.get("day") or []
    except:
        return None

def valid_ohlc(o, c, h, l):
    values = (o, c, h, l)
    return all(v is not None and v > 0 for v in values) and h >= l

def build_sql_inserts(symbol, klines, source):
    """Build batch INSERT SQL for all klines of one stock"""
    rows = []
    for k in klines:
        if len(k) < 6: continue
        dt = k[0]
        try:
            o, c, h, l = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            if not valid_ohlc(o, c, h, l):
                log(f"  跳過異常K線 {symbol} {dt}: o={o} c={c} h={h} l={l}")
                continue
            dt = str(dt)[:10]
            v = float(k[5]) if k[5] else 0
            a = c * v
            chg = ((c - o) / o * 100) if o > 0 else 0
            rows.append(f"('{symbol}','day','{dt}',{o},{h},{l},{c},{v},{a},{chg:.4f},'{source}',NOW())")
        except:
            continue
    if not rows: return None
    values = ",".join(rows)
    return f"""INSERT INTO klines (symbol,interval,timestamp,open_price,high_price,low_price,close_price,volume,amount,change_percent,data_source,created_at)
               VALUES {values}
               ON CONFLICT (symbol, interval, timestamp) DO UPDATE SET
                   open_price = EXCLUDED.open_price,
                   high_price = EXCLUDED.high_price,
                   low_price = EXCLUDED.low_price,
                   close_price = EXCLUDED.close_price,
                   volume = EXCLUDED.volume,
                   amount = EXCLUDED.amount,
                   change_percent = EXCLUDED.change_percent,
                   data_source = EXCLUDED.data_source,
                   created_at = NOW();"""

def process_batch(symbols, market, source):
    """Process a batch of symbols, writing SQL file per 10 stocks"""
    ok, fail = 0, 0
    sql_buf = []
    batch_size = 10
    
    for i, sym in enumerate(symbols):
        klines = fetch_kline(sym, market)
        if not klines:
            fail += 1
            continue
        
        sql = build_sql_inserts(sym, klines, source)
        if sql:
            sql_buf.append(sql)
        else:
            fail += 1
            continue
        
        # Write batch every N stocks
        if len(sql_buf) >= batch_size:
            write_ok, write_fail = _flush_batch(sql_buf)
            ok += write_ok
            fail += write_fail
            sql_buf = []
        
        if (i+1) % 50 == 0:
            log(f"  進度: {i+1}/{len(symbols)} (ok={ok} fail={fail})")
        time.sleep(0.2)
    
    # Flush remaining
    if sql_buf:
        write_ok, write_fail = _flush_batch(sql_buf)
        ok += write_ok
        fail += write_fail
    
    return ok, fail

def process_us_symbols(symbols):
    ok, fail = 0, 0
    for sym in symbols:
        found = False
        for suffix in [".OQ", ".N", ""]:
            klines = fetch_kline(f"{sym}{suffix}", "us")
            if not klines:
                continue
            found = True
            sql = build_sql_inserts(sym, klines, "tencent_us")
            if sql:
                write_ok, write_fail = _flush_batch([sql])
                ok += write_ok
                fail += write_fail
            else:
                fail += 1
            break
        if not found:
            fail += 1
        time.sleep(0.3)
    return ok, fail

def _flush_batch(sql_buf):
    """Write SQL buffer to temp file and execute"""
    if not sql_buf:
        return 0, 0
    if len(sql_buf) > 1 and not ALLOW_MULTI_SYMBOL_TRANSACTION:
        ok, fail = 0, 0
        for statement in sql_buf:
            one_ok, one_fail = _flush_batch([statement])
            ok += one_ok
            fail += one_fail
        return ok, fail
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
        f.write("BEGIN;\n")
        for s in sql_buf:
            f.write(s + "\n")
        f.write("COMMIT;\n")
        tmp = f.name
    try:
        result = db_batch(tmp)
    finally:
        os.unlink(tmp)
    if result.returncode == 0:
        return len(sql_buf), 0

    log(f"  批量寫入失敗，逐隻重試: {str(result.stderr).strip()[-300:]}")
    ok, fail = 0, 0
    for statement in sql_buf:
        symbol = statement.split("VALUES ('", 1)[1].split("'", 1)[0] if "VALUES ('" in statement else "unknown"
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
            f.write("BEGIN;\n")
            f.write(statement + "\n")
            f.write("COMMIT;\n")
            tmp = f.name
        try:
            single = db_batch(tmp)
        finally:
            os.unlink(tmp)
        if single.returncode == 0:
            ok += 1
        else:
            fail += 1
            log(f"  寫入失敗 {symbol}: {str(single.stderr).strip()[-300:]}")
    return ok, fail

def run_update(market="all", now=None):
    market = str(market or "all").lower()
    if market not in ("all", "hk", "us"):
        raise ValueError(f"unsupported market: {market}")
    log("=" * 50)
    log(f"K線批量更新 market={market}")

    hk_fail = 0
    us_fail = 0
    market_results = []
    
    # 港股
    if market in ("all", "hk"):
        syms = db("SELECT symbol FROM stocks WHERE is_active=true AND exchange='HKEX' ORDER BY symbol")
        hk_list = [s.strip() for s in syms.split('\n') if s.strip()]
        log(f"港股: {len(hk_list)} 隻")
        hk_ok, hk_fail = process_batch(hk_list, "hk", "tencent")
        log(f"港股完成: {hk_ok} ok, {hk_fail} fail")
        hk_continue, hk_success_rate = market_refresh_ok(hk_ok, hk_fail)
        market_results.append(("HK", hk_continue, hk_success_rate, hk_ok, hk_fail, ""))
    
    # 美股
    if market in ("all", "us"):
        blocked, block_reason = us_daily_fetch_block(now)
        if blocked:
            log(f"美股日線刷新跳過: {block_reason}")
            market_results.append(("US", False, 0.0, 0, 0, block_reason))
        else:
            syms = db("SELECT symbol FROM stocks WHERE is_active=true AND exchange IN ('NASDAQ','NYSE') ORDER BY symbol")
            us_list = [s.strip() for s in syms.split('\n') if s.strip()]
            log(f"美股: {len(us_list)} 隻")
            us_ok, us_fail = process_us_symbols(us_list)
            log(f"美股完成: {us_ok} ok, {us_fail} fail")
            us_continue, us_success_rate = market_refresh_ok(us_ok, us_fail)
            market_results.append(("US", us_continue, us_success_rate, us_ok, us_fail, ""))
    
    total = db("SELECT count(DISTINCT symbol) FROM klines WHERE interval='day'")
    latest = db("SELECT max(timestamp) FROM klines WHERE interval='day'")
    log(f"=== 總計: {total} 隻, 最新: {latest} ===")
    for market_name, ok_to_continue, success_rate, ok_count, fail_count, reason in market_results:
        status = "OK" if ok_to_continue else "FAIL"
        reason_suffix = f" reason={reason}" if reason else ""
        log(
            f"{market_name}刷新門檻: {status} success_rate={success_rate:.2%} "
            f"ok={ok_count} fail={fail_count} min={MIN_SUCCESS_RATE:.0%}{reason_suffix}"
        )
    return all(result[1] for result in market_results)

def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=("all", "hk", "us"), default=os.environ.get("KLINE_BATCH_MARKET", "all"))
    parser.add_argument("--no-lock", action="store_true", help="disable single-instance lock for tests/debug")
    parser.add_argument("--lock-file", default=LOCK_FILE)
    return parser.parse_args(argv)

def main(argv=None):
    args = parse_args(argv)
    lock_enabled = not args.no_lock and os.environ.get("KLINE_BATCH_DISABLE_LOCK", "0") != "1"
    try:
        with SingleInstanceLock(args.lock_file, enabled=lock_enabled):
            ok = run_update(args.market)
    except AlreadyRunning as exc:
        log(f"已有 K線批量更新在執行，跳過本輪: {exc}")
        return 0
    return 0 if ok else 2

if __name__ == '__main__':
    sys.exit(main())
