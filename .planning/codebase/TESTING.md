# Testing Patterns

**Analysis Date:** 2026-06-19

## Test Framework

**Runner:**
- Python `unittest` from the standard library.
- Config: Not detected. There is no `pytest.ini`, `pyproject.toml`, `setup.cfg`, or `tox.ini`.
- Local verification wrapper: `scripts/local_verify.py`.
- Current test inventory: 78 files under `tests/`, 88 Python files under `scripts/`, and 4 Python files under `backtest/`.

**Assertion Library:**
- `unittest.TestCase` assertions: `assertEqual`, `assertTrue`, `assertFalse`, `assertIn`, `assertNotIn`, `assertAlmostEqual`, `assertRegex`, and mock call assertions in `tests/test_rt_signal_engine_v5.py`, `tests/test_watchlist_promote.py`, and `tests/test_feishu_notify.py`.

**Run Commands:**
```bash
python scripts/local_verify.py              # Compile scripts/tests, run unittest discovery, then git diff --check
python scripts/local_verify.py --skip-git   # Compile scripts/tests and run unittest discovery only
python -m unittest discover -s tests        # Run all unit tests
python -m compileall -q scripts tests       # Syntax/import smoke check
git diff --check                            # Whitespace check used by local_verify.py
```

## Test File Organization

**Location:**
- Automated tests live in `tests/`.
- Test modules map directly to source modules: `tests/test_rt_order_intake.py` tests `scripts/rt_order_intake.py`, `tests/test_watchlist_promote.py` tests `scripts/watchlist_promote.py`, and `tests/test_v5_local_replay_report.py` tests `scripts/v5_local_replay_report.py`.
- Historical research/backtest scripts live in `backtest/`; automated coverage focuses on current report, safety-gate, replay, and operator tooling under `scripts/`.

**Naming:**
- Use `test_<source_module>.py` for test files: `tests/test_data_health_report.py`, `tests/test_hermes_review_packet.py`, `tests/test_strategy_config_proposal.py`.
- Use `<Domain><Behavior>Tests(unittest.TestCase)` for test classes: `RtOrderIntakeTests` in `tests/test_rt_order_intake.py`, `WatchlistPromoteTests` in `tests/test_watchlist_promote.py`, and `DataHealthReportTests` in `tests/test_data_health_report.py`.
- Use `test_<behavior>_<expected_result>()` for test methods: `test_apply_requires_matching_hash()` in `tests/test_watchlist_promote.py`, `test_missing_credentials_do_not_call_token_endpoint()` in `tests/test_feishu_notify.py`, and `test_db_replay_default_end_date_uses_yesterday_to_avoid_intraday_daily_rows()` in `tests/test_v5_local_replay_report.py`.

**Structure:**
```text
tests/
├── test_rt_signal_engine_v5.py                 # v5 signal scoring, state, alert contracts
├── test_rt_order_intake.py                     # order-intake gates, state locks, execution safety
├── test_hermes_review_packet.py                # Hermes packet context and judgment contracts
├── test_watchlist_promote.py                   # hash-confirmed watchlist promotion safety
├── test_cron_install_promote.py                # cron install dry-run/apply safety
├── test_data_health_report.py                  # data-health report shape and market coverage
└── test_<module>.py                            # one test module per script module
```

## Test Structure

**Suite Organization:**
```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import watchlist_promote as promote


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class WatchlistPromoteTests(unittest.TestCase):
    def test_apply_requires_matching_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = root / "diff.json"
            target = root / "watchlist.json"
            # Arrange JSON inputs, call build_report(), assert payload contract.
            payload = promote.build_report(str(report), str(target), apply=True)

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("confirm_proposal_hash_required", payload["validation_reasons"])
```

**Patterns:**
- Put fixture builders at module top level: `fresh_alert()` in `tests/test_rt_order_intake.py`, `alert()` in `tests/test_hermes_review_packet.py`, and `trend_rows()` in `tests/test_v5_local_replay_report.py`.
- Test `build_report(...)` and pure helpers directly instead of shelling out to the CLI where possible: `tests/test_watchlist_promote.py`, `tests/test_data_health_report.py`, and `tests/test_strategy_learning_report.py`.
- Use `main([...])` tests only when verifying CLI file output or text mode, as in `tests/test_v5_local_replay_report.py`.
- Use `tempfile.TemporaryDirectory()` with `pathlib.Path` or `os.path.join` for transient JSON/CSV/state files: `tests/test_feishu_notify.py`, `tests/test_rt_order_intake.py`, and `tests/test_watchlist_promote.py`.
- Assert exact `schema`, `status`, booleans, reason codes, and counts rather than only truthiness; this is consistent across `tests/test_execution_readiness_report.py`, `tests/test_source_reliability_report.py`, and `tests/test_operator_action_queue_report.py`.
- Keep tests deterministic by injecting `datetime` values or patching module `datetime` where date logic matters, as in `tests/test_v5_local_replay_report.py` and `tests/test_data_health_report.py`.

## Mocking

**Framework:** `unittest.mock`.

**Patterns:**
```python
from unittest.mock import patch

with patch.object(intake, "health_gate", return_value=(True, {"status": "OK"})), \
     patch.object(intake, "execution_readiness_gate", return_value=(True, {"status": "PASS"})), \
     patch.object(intake, "submit_order", return_value={"order_id": "ok"}) as submit:
    result = intake.process_alert(alert, "execute", state, state_file, judgment_file)

self.assertEqual(result["status"], "submitted")
submit.assert_called_once()
```

**What to Mock:**
- Database and subprocess access: patch `db`, `psql`, or command wrappers in `scripts/rt_signal_engine_v5.py`, `scripts/data_health_report.py`, `scripts/system_health_check.py`, and `scripts/sim_position_reconcile.py`.
- Network clients and HTTP calls: patch `urllib.request.urlopen`, provider fetchers, or session objects in `scripts/feishu_notify.py`, `scripts/external_market_context_producer.py`, `scripts/fundamentals_context_producer.py`, and `scripts/local_backtest_dataset.py`.
- Runtime gates and external report readers: patch gate helpers in `scripts/rt_order_intake.py`, report inputs in `scripts/hermes_review_packet.py`, and cron installers in `scripts/cron_install_promote.py`.
- Environment variables by saving/restoring `os.environ` or using module reload helpers, as in `tests/test_feishu_notify.py` and `tests/test_rt_alert_bridge.py`.
- Time-sensitive functions with explicit `datetime` values or `patch.object(module, "datetime", wraps=module.datetime)`, as in `tests/test_v5_local_replay_report.py`.

**What NOT to Mock:**
- Pure payload builders, schema validators, scoring helpers, hash helpers, and compaction functions. Test these directly in memory, as in `tests/test_watchlist_diff_report.py`, `tests/test_strategy_review_report.py`, and `tests/test_alert_quality_report.py`.
- Atomic write behavior when the purpose of the test is file safety. `tests/test_rt_order_intake.py` checks `save_json_atomic()` uses a unique temp file.
- Promotion safety reason generation. Let `scripts/watchlist_promote.py`, `scripts/strategy_config_promote.py`, and `scripts/cron_install_promote.py` produce real blockers from fixture payloads.

## Fixtures and Factories

**Test Data:**
```python
def fresh_alert(signal_id="sig-1", symbol="00700"):
    return {
        "signal_id": signal_id,
        "symbol": symbol,
        "signal_type": "BUY",
        "trigger": "unit-test",
        "confirmed": True,
        "execution_candidate": True,
        "full_score": 0.7,
        "entry_price": 300,
        "stop_loss": 290,
        "take_profit": 330,
        "rr_ratio": 3.0,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
```

**Location:**
- Fixtures are usually top-level helper functions inside the relevant test file: `tests/test_rt_order_intake.py`, `tests/test_hermes_review_packet.py`, `tests/test_strategy_config_proposal.py`.
- File fixtures are written into `tempfile.TemporaryDirectory()` inside each test, not stored in a shared fixtures directory.
- Repo config fixtures are read directly when testing real config contracts, for example `config/rt_signal_strategy_config.json` in `tests/test_rt_signal_engine_v5.py`.
- Test-created CSV rows use local helper writers such as `write_rows()` in `tests/test_v5_local_replay_report.py`.

## Coverage

**Requirements:** None enforced.

**View Coverage:**
```bash
# No coverage command exists.
```

- No coverage package is listed in `requirements.txt`.
- `# pragma: no cover` appears only on compatibility/runtime branches in files such as `scripts/rt_runtime_scope.py`, `scripts/read_positions.py`, and `scripts/rt_signal_engine_v5.py`.
- Coverage priority is contract depth rather than percentage. The suite asserts safety gates, schema stability, reason codes, report compaction, dry-run behavior, and atomic writes across `tests/`.

## Test Types

**Unit Tests:**
- Primary test type. Most tests call module functions with in-memory dictionaries and assert exact payloads.
- Examples: scoring and state tests in `tests/test_rt_signal_engine_v5.py`, report logic in `tests/test_alert_quality_report.py`, and helper contracts in `tests/test_us_universe_filter.py`.

**Integration Tests:**
- Lightweight integration-style tests combine multiple local modules with mocked external edges.
- Examples: `tests/test_hermes_review_packet.py` combines alert, readiness, context, and Hermes contract payloads; `tests/test_watchlist_promote.py` combines diff reports, readiness reports, source reliability, simulation performance, and strategy learning fixtures.
- Subprocess, network, DB, crontab, and broker boundaries are mocked or replaced with temp files; tests should not require live Docker, broker APIs, Feishu, Alpaca, or production `/tmp` files.

**E2E Tests:**
- Not used as browser or deployed-system tests.
- The closest end-to-end checks are local CLI/report tests such as `tests/test_v5_local_replay_report.py::test_main_writes_report_and_text_mode` and repository verification via `scripts/local_verify.py`.

## Common Patterns

**Async Testing:**
```python
# Not applicable. The source and tests are synchronous.
# Concurrency-sensitive state is tested through locks and atomic files, not asyncio.
```

**Error Testing:**
```python
def test_missing_credentials_do_not_call_token_endpoint(self):
    feishu = self.load_module(FEISHU_ENV_FILE="/tmp/does-not-exist")

    with patch.object(feishu.urllib.request, "urlopen") as urlopen:
        self.assertIsNone(feishu.get_tenant_token())

    urlopen.assert_not_called()
```

- Test fail-closed behavior by asserting blocker reason codes, not just failure status: `tests/test_rt_order_intake.py`, `tests/test_execution_readiness_report.py`, and `tests/test_watchlist_promote.py`.
- Test missing/corrupt files with temp paths and invalid JSON: `tests/test_rt_signal_engine_v5.py`, `tests/test_feishu_notify.py`, and `tests/test_external_market_context_report.py`.
- Test provider failures by patching provider functions to raise exceptions and asserting degraded report payloads: `tests/test_external_market_context_producer.py` and `tests/test_fundamentals_context_producer.py`.
- Test unsafe mutation paths by ensuring apply actions require matching hashes and backups: `tests/test_cron_install_promote.py`, `tests/test_watchlist_promote.py`, and `tests/test_strategy_config_promote.py`.

## Current Verification Findings

**Local verification:**
- Command run during mapping: `python scripts\local_verify.py --skip-git`.
- Result: passed.
- Steps executed by `scripts/local_verify.py`: `python -m compileall -q scripts tests` and `python -m unittest discover -s tests`.
- Test result during mapping: 1,023 tests run in 16.691 seconds, status `OK`.

**Repository release check:**
- Use `python scripts/local_verify.py` before release to include `git diff --check`.
- README also documents `python -m unittest discover -s tests` and `git diff --check` under `README.md`.

---

*Testing analysis: 2026-06-19*
