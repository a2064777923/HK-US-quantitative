import json
import os
import tempfile
import unittest

from scripts import factor_contract_alignment_report as report


V5_SOURCE = """
BUY_CONFIRMATION_MIN_SCORE = 0.45
SELL_CONFIRMATION_MAX_SCORE = -0.45
VOLUME_ANOMALY_RATIO = 3.0
MOMENTUM_THRESHOLD_PCT = 5.0
MIN_SIGNAL_HISTORY_BARS = 30
def alert_timeframe_metadata():
    return {"timeframe_scope": "completed_daily_ohlcv_with_realtime_quote", "realtime_input": "single_quote_temporary_bar"}
class IncrementalIndicator:
    def score_volume_ratio(self): pass
    def get_score(self):
        ma5=ma10=ma20=1
        if c > ma5 > ma10 > ma20: score += 0.8
        if self.rsi_14 > 70: score -= 0.3
        if self.rsi_14 < 30: score += 0.3
        if self.macd_hist > 0 and self.macd_dif > 0: score += 0.3
        bb_upper, bb_lower = signal_bollinger_bands(self)
        if c < prior_close: reasons.append("放量下跌")
        base_close = lookback_close(closes, 5)
class TriggerEngine:
    def is_confirmed(self): pass
    def risk_geometry(self): pass
    def risk_reward_ratio(self): pass
    def min_rr_ratio(self): pass
    def check(self):
        triggered.append(("RSI超賣", "", "BUY"))
        execution_candidate = True
        atr_stop_multiple = 2.0
        atr_take_profit_multiple = 3.0
        emit_unconfirmed_directional_as_watch()
"""


BACKTEST_SOURCE = """
BUY=0.65; SELL=0.35; SLIP=0.002; SCAN=5
def rsi(c,p=14): pass
def score(closes, highs, lows, vols):
    ma5=ma10=ma20=1
    if c>ma5>ma10>ma20: t=0.8
    m=sum(closes[-25:-5])/20
    s=(ma20-m)/m
    r = rsi(closes)
    if r>70: m-=0.3
    def ema(d,p): pass
    ef=ema(closes,12); es=ema(closes,26)
    hist=[1]
    w=closes[-20:]; std=1
    vr=vols[-1]/a20
    if vr>1.5 and c>closes[-2]: v+=0.2
def ch_stop(h,l,c,mult=2): pass
if sc >= BUY: pass
"""


class FactorContractAlignmentReportTests(unittest.TestCase):
    def test_build_report_flags_partial_v5_alignment_and_no_promotion(self):
        payload = report.build_report(V5_SOURCE, BACKTEST_SOURCE, BACKTEST_SOURCE)

        self.assertEqual(payload["schema"], "factor_contract_alignment_report_v1")
        self.assertEqual(payload["summary"]["overall_status"], "PARTIAL_ALIGNMENT_REQUIRES_CAUTION")
        self.assertFalse(payload["summary"]["promotion_ready"])
        self.assertEqual(payload["hermes_contract"]["contract"], "research_alignment_context_only")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        codes = [item["code"] for item in payload["checks"]]
        self.assertIn("portfolio_backtest_realistic:score_thresholds_drift", codes)
        self.assertIn("portfolio_backtest_realistic:trigger_model_drift", codes)
        self.assertIn("portfolio_backtest_realistic:risk_execution_contract_drift", codes)
        self.assertIn("portfolio_backtest_realistic:data_basis_drift", codes)
        self.assertIn("duplicated_backtest_score_implementations", codes)

    def test_extract_contracts_capture_key_factor_families(self):
        v5 = report.extract_v5_contract(V5_SOURCE)
        backtest = report.extract_backtest_contract("bt", BACKTEST_SOURCE)

        self.assertTrue(v5["factors"]["momentum_5d"])
        self.assertTrue(v5["factors"]["directional_volume"])
        self.assertFalse(backtest["factors"]["momentum_5d"])
        self.assertFalse(backtest["risk_model"]["execution_candidate"])
        self.assertEqual(v5["thresholds"]["buy_confirmation_min_score"], 0.45)
        self.assertEqual(backtest["thresholds"]["buy_score"], 0.65)

    def test_main_writes_report_from_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            v5 = os.path.join(tmp, "v5.py")
            realistic = os.path.join(tmp, "realistic.py")
            combined = os.path.join(tmp, "combined.py")
            output = os.path.join(tmp, "report.json")
            for path, text in ((v5, V5_SOURCE), (realistic, BACKTEST_SOURCE), (combined, BACKTEST_SOURCE)):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)

            rc = report.main(
                [
                    "--v5-file",
                    v5,
                    "--realistic-file",
                    realistic,
                    "--combined-file",
                    combined,
                    "--output",
                    output,
                ]
            )

            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(output))
            with open(output, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["source"]["source_files"]["v5_file"], os.path.abspath(v5))
            self.assertEqual(payload["summary"]["hermes_use"], "research_alignment_context_only")


if __name__ == "__main__":
    unittest.main()
