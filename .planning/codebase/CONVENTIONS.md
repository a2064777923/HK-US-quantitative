# Coding Conventions

**Analysis Date:** 2026-06-19

## Naming Patterns

**Files:**
- Use lower snake case for Python scripts under `scripts/`: `scripts/rt_signal_engine_v5.py`, `scripts/hermes_review_packet.py`, `scripts/execution_readiness_report.py`, `scripts/watchlist_promote.py`.
- Use `*_report.py` for read-only report builders that emit JSON/text artifacts: `scripts/data_health_report.py`, `scripts/source_reliability_report.py`, `scripts/strategy_learning_report.py`.
- Use `*_promote.py` for hash-confirmed mutation helpers that default to dry-run and require explicit confirmation: `scripts/watchlist_promote.py`, `scripts/strategy_config_promote.py`, `scripts/cron_install_promote.py`.
- Use `*_event_store.py` for append/query/report adapters around JSONL or DB event history: `scripts/rt_alert_event_store.py`, `scripts/hermes_judgment_event_store.py`, `scripts/rt_order_intake_event_store.py`.
- Use `test_<module>.py` for tests co-located by source module in `tests/`: `tests/test_rt_signal_engine_v5.py`, `tests/test_hermes_review_packet.py`, `tests/test_watchlist_promote.py`.
- Keep historical research scripts under `backtest/` with descriptive names: `backtest/portfolio_backtest_realistic.py`, `backtest/segment_backtest.py`.

**Functions:**
- Use lower snake case for helpers and builders: `build_report()` in `scripts/watchlist_promote.py`, `load_json_file()` in `scripts/rt_order_intake.py`, `save_json_atomic()` in `scripts/hermes_review_packet.py`.
- Use `build_report(...)` as the primary pure-ish entry for report payload construction in report modules such as `scripts/data_health_report.py`, `scripts/alert_quality_report.py`, and `scripts/source_reliability_report.py`.
- Use `build_text_report(payload)` or similarly named text renderers for CLI-readable output in modules such as `scripts/cron_audit_report.py`, `scripts/watchlist_diff_report.py`, and `scripts/trusted_source_preflight.py`.
- Use `main(argv=None)` or `main()` for CLI entry points, then call it from `if __name__ == "__main__":` with `sys.exit(main())` or `raise SystemExit(main())`, as in `scripts/cron_install_promote.py`, `scripts/event_catalyst_report.py`, and `scripts/v5_local_replay_report.py`.
- Use `now_iso()` for timestamp fields in report modules such as `scripts/watchlist_promote.py`, `scripts/cron_install_promote.py`, and `scripts/rt_order_intake.py`.

**Variables:**
- Use lower snake case for local variables and payload fragments: `proposal_hash`, `validation_reasons`, `guard_files`, `watchlist_file` in `scripts/watchlist_promote.py` and `tests/test_watchlist_promote.py`.
- Use upper snake case for module-level runtime constants and environment-derived defaults: `ALERT_QUEUE_FILE`, `EXECUTION_READINESS_REPORT_FILE`, `PILOT_EXECUTION_ENABLED`, `WATCHLIST_FILE` in `scripts/rt_order_intake.py`, `scripts/hermes_review_packet.py`, and `scripts/rt_signal_engine_v5.py`.
- Use short numeric-loop names mainly in legacy backtest math (`c`, `h`, `l`, `v`, `pos`, `nv`) in `backtest/portfolio_backtest_realistic.py` and `backtest/portfolio_backtest_combined.py`; prefer descriptive names in maintained `scripts/` modules.
- Use domain status strings as uppercase literals in payloads: `READY`, `BLOCKED`, `WARN`, `OK`, `FAIL`, `BUY`, `SELL`, `WATCH` in `scripts/execution_readiness_report.py`, `scripts/rt_signal_engine_v5.py`, and `scripts/alert_quality_report.py`.

**Types:**
- Most domain objects are plain dictionaries and lists rather than dataclasses: alerts in `scripts/rt_signal_engine_v5.py`, Hermes packets in `scripts/hermes_review_packet.py`, and report payloads in `scripts/source_reliability_report.py`.
- Use small classes when state or test doubles need behavior: `StateLockTimeout` in `scripts/rt_order_intake.py`, `FakeIndicators` in `tests/test_rt_signal_engine_v5.py`, and `FakeSession` in `tests/test_local_backtest_dataset.py`.
- JSON schema names live in payload fields, not Python classes: `schema` keys are asserted across `tests/test_hermes_trade_judgment_schema.py`, `tests/test_watchlist_promote.py`, and `tests/test_v5_local_replay_report.py`.

## Code Style

**Formatting:**
- No formatter is configured. There is no `pyproject.toml`, `setup.cfg`, `.flake8`, `ruff.toml`, `mypy.ini`, `.prettierrc`, or JavaScript lint config in the repo root; only `requirements.txt` exists for Python dependencies.
- Write modern maintained code in readable, multi-line Python with one import per line where practical, as in `scripts/watchlist_promote.py`, `scripts/rt_order_intake.py`, and `scripts/hermes_review_packet.py`.
- Treat compact one-line guards and comma-separated imports as legacy style limited mostly to `backtest/` and older scripts such as `scripts/signal_engine_v4.py`, `scripts/kline_batch.py`, and `backtest/segment_backtest.py`.
- Write JSON files with UTF-8 and `ensure_ascii=False` so Chinese trigger names and report text remain stable; examples include `scripts/rt_order_intake.py`, `scripts/watchlist_promote.py`, and `scripts/cron_install_promote.py`.
- Prefer atomic file writes for runtime state, reports, and promotion targets. Existing patterns use `tempfile.mkstemp()` in `scripts/rt_order_intake.py`, PID/timestamp temp paths in `scripts/hermes_review_packet.py`, and `.tmp` replacement in `scripts/watchlist_promote.py`.
- Keep raw market data, local databases, compressed data files, and secret-bearing files out of git per `.gitignore`.

**Linting:**
- No linting tool is configured. Use `scripts/local_verify.py` to run the current repository checks: `compileall`, `unittest discover`, and optionally `git diff --check`.
- Keep whitespace clean because `scripts/local_verify.py` includes `git diff --check` unless `--skip-git` is passed.
- Use `# pragma: no cover` sparingly for import-path/runtime compatibility branches, as in `scripts/rt_runtime_scope.py`, `scripts/us_realtime.py`, and `scripts/rt_signal_engine_v5.py`.

## Import Organization

**Order:**
1. Standard library imports first: `argparse`, `json`, `os`, `subprocess`, `sys`, `tempfile`, `datetime`, `pathlib`, `collections` in `scripts/rt_order_intake.py`, `scripts/hermes_review_packet.py`, and `tests/test_rt_order_intake.py`.
2. Optional or compatibility imports inside `try/except ImportError` blocks for script/package execution paths: `scripts/hermes_review_packet.py`, `scripts/watchlist_promote.py`, `scripts/rt_runtime_scope.py`.
3. Third-party imports only where needed: `requests` in `scripts/local_backtest_dataset.py`, `psycopg2` in `scripts/signal_engine_v4.py`, and packages listed in `requirements.txt`.
4. Local script imports use `from scripts import ...` in tests: `tests/test_data_health_report.py`, `tests/test_watchlist_promote.py`, and `tests/test_v5_local_replay_report.py`.

**Path Aliases:**
- No path alias system is configured. The repo relies on the root being importable so tests can use `from scripts import module` and runtime scripts can fall back from direct imports to package imports.
- There is no package-level `scripts/__init__.py`; namespace-package import behavior is sufficient for `python -m unittest discover -s tests`.

## Error Handling

**Patterns:**
- Use fail-closed report payloads and explicit reason codes for safety gates. Examples include `validation_reasons` in `scripts/watchlist_promote.py`, `blocking_gates` in `scripts/execution_readiness_report.py`, and `reason_codes` in `scripts/simulation_performance_report.py`.
- Use `load_json_file(path, default)` helpers that return safe defaults on missing, unreadable, or invalid JSON: `scripts/rt_order_intake.py`, `scripts/watchlist_promote.py`, `scripts/cron_install_promote.py`.
- Return structured warnings instead of raising for expected missing inputs in report builders: `scripts/data_health_report.py`, `scripts/event_catalyst_signal_report.py`, and `scripts/trusted_source_preflight.py`.
- Raise explicit exceptions for unsafe operator actions or invalid identifiers: `RuntimeError` in `scripts/trade_update.py`, `ValueError` in `scripts/hermes_judgment_event_store.py`, and `StateLockTimeout` in `scripts/rt_order_intake.py`.
- Use broad `except Exception` only at integration boundaries such as network calls, subprocess calls, optional files, and compatibility imports; keep the fallback visible in payload warnings or console output.
- Avoid bare `except:` in maintained code. Existing bare handlers appear in legacy/backtest paths such as `backtest/segment_backtest.py`, `backtest/portfolio_backtest_realistic.py`, and `scripts/update_portfolio_prices.py`.
- For apply/promote helpers, default to dry-run and require hash confirmation before mutation: `scripts/watchlist_promote.py`, `scripts/strategy_config_promote.py`, and `scripts/cron_install_promote.py`.

## Logging

**Framework:** console output via `print()`.

**Patterns:**
- Use timestamped `log(msg)` helpers for long-running scripts and engines: `scripts/rt_signal_engine_v5.py`, `scripts/signal_engine_v4.py`, `scripts/update_portfolio_prices.py`, and `scripts/expand_batch.py`.
- Use `print(json.dumps(payload, ensure_ascii=False, indent=2))` for `--json` CLI output in report modules such as `scripts/cron_audit_report.py`, `scripts/source_reliability_report.py`, and `scripts/watchlist_diff_report.py`.
- Use `build_text_report(payload)` for human-readable `--text` output in report modules such as `scripts/execution_readiness_report.py`, `scripts/data_health_report.py`, and `scripts/strategy_learning_report.py`.
- Use stderr for operator-facing command errors where the script is interactive or mutating: `scripts/trade_update.py`, `scripts/us_realtime.py`, and `scripts/sim_trade.py`.
- Do not introduce the `logging` module unless a broader refactor standardizes it across `scripts/`; no central logger exists.

## Comments

**When to Comment:**
- Use module docstrings to state operational intent and safety boundaries, as in `scripts/rt_order_intake.py`, `scripts/hermes_review_packet.py`, and `scripts/cron_install_promote.py`.
- Use comments for domain constraints, market assumptions, and operator safety contracts, especially in `scripts/rt_signal_engine_v5.py`, `scripts/cron_audit_report.py`, and `README.md`.
- Prefer reason-code fields and schema names over explanatory inline comments when behavior must be consumed by tests or Hermes reports, as seen in `scripts/execution_readiness_report.py` and `scripts/hermes_judgment_audit_report.py`.
- Keep comments near non-obvious compatibility branches such as import fallbacks and platform-specific file locks in `scripts/rt_order_intake.py`.

**JSDoc/TSDoc:**
- Not applicable. The repository is Python-only for source and tests.
- Python docstrings are used on modules and selected larger functions; helper functions often rely on clear names and tests instead of docstrings.

## Function Design

**Size:** 
- Prefer small parsing, validation, scoring, and compaction helpers such as `as_float()` in `scripts/rt_order_intake.py`, `compact_source_reliability_context()` in `scripts/watchlist_promote.py`, and `missing_config_keys()` in `scripts/feishu_notify.py`.
- Keep report orchestration in `build_report(...)` functions and test those directly, as in `scripts/watchlist_promote.py`, `scripts/v5_local_replay_report.py`, and `scripts/data_health_report.py`.
- Large domain modules exist and should be changed surgically: `scripts/rt_signal_engine_v5.py`, `scripts/hermes_review_packet.py`, `scripts/rt_signal_outcome_report.py`, and `scripts/data_health_report.py`.

**Parameters:** 
- Pass file paths and payload overrides into `build_report(...)` rather than reading globals inside tests. Existing examples include `scripts/watchlist_promote.py` and `scripts/cron_install_promote.py`.
- Keep CLI defaults wired to module constants derived from environment variables, then let argparse override them; examples include `scripts/watchlist_diff_report.py`, `scripts/trusted_source_preflight.py`, and `scripts/v5_local_replay_report.py`.
- Use primitive values and dictionaries for payload-oriented helpers. Avoid introducing broad config objects unless they replace existing repeated parameter groups in a tested module.

**Return Values:** 
- Return dictionaries with a `schema`, `status`, source metadata, counts, checks, recommendations, and reason codes for reports and promotion helpers: `scripts/execution_readiness_report.py`, `scripts/source_reliability_report.py`, and `scripts/watchlist_promote.py`.
- Return `(payload, warnings)` or `(ok, detail)` tuples where callers need both data and diagnostics: `scripts/data_health_report.py`, `scripts/rt_order_intake.py`, and `scripts/v5_local_replay_report.py`.
- Return process status codes from `main()` and call `sys.exit(main())` for CLIs that may fail validation, as in `scripts/cron_install_promote.py` and `scripts/local_verify.py`.

## Module Design

**Exports:** 
- There is no explicit public API list. Modules expose functions/classes directly and tests import the script module under `scripts/`.
- Use module-level constants for environment-controlled paths and thresholds: `scripts/hermes_review_packet.py`, `scripts/rt_order_intake.py`, and `scripts/rt_signal_engine_v5.py`.
- Keep core behavior importable without running the CLI. Top-level code should define constants/functions/classes only; CLI work belongs in `main()` under the entry guard.
- Keep mutation paths separated from read-only report paths. For example, `scripts/watchlist_diff_report.py` proposes changes, while `scripts/watchlist_promote.py` applies only after safety checks.

**Barrel Files:** 
- None. Do not add barrel modules unless the repo adopts a real package layout.
- Tests should continue importing concrete modules directly, for example `from scripts import rt_order_intake as intake` in `tests/test_rt_order_intake.py`.

## Domain Conventions

**Safety contracts:**
- Report payloads should explicitly state read-only or mutation behavior with booleans such as `submits_orders`, `read_only`, `auto_applied`, `manual_review_required`, and `dry_run_by_default`; examples are asserted in `tests/test_watchlist_promote.py`, `tests/test_cron_install_promote.py`, and `tests/test_v5_local_replay_report.py`.
- Execution-related modules must stay fail-closed by default. Preserve gates in `scripts/rt_order_intake.py`, `scripts/rt_alert_bridge.py`, and `scripts/execution_readiness_report.py`.
- Hash-confirmed promotion flows must preserve proposal hashes and validation reasons. Follow `scripts/watchlist_promote.py`, `scripts/strategy_config_promote.py`, and `scripts/cron_install_promote.py`.

**Environment configuration:**
- Use env var names only in code, tests, and docs; never commit secret values. Sensitive keys include `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_CHAT_ID`, `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, and `QM_API_PASSWORD`.
- Runtime file paths are env-driven and usually default to `/tmp/...` report artifacts or `/root/...` server config files; examples are in `scripts/hermes_review_packet.py`, `scripts/rt_order_intake.py`, and `scripts/watchlist_diff_report.py`.
- Secret files are not read during mapping and should not be committed. `.gitignore` covers `config/.env`, `config/secrets.json`, `*.pem`, and `*.key`.

**Data/report contracts:**
- Preserve `schema` names and versioned payload shapes; many tests assert exact schema strings in `tests/test_hermes_review_packet.py`, `tests/test_signal_engine_v4_schema.py`, and `tests/test_source_reliability_report.py`.
- Use compact report artifacts for production/Hermes workflows and keep raw data local per `README.md` and `.gitignore`.
- Prefer structured `checks`, `warnings`, `recommendations`, `blocking_reasons`, and `reason_codes` arrays over prose-only errors.

---

*Convention analysis: 2026-06-19*
