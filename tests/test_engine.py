"""Calculation invariants, including conservative unknown/stale behavior."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from antmonitor.engine import calculate


NOW = 1_800_000_000_000


def fixture():
    config = {
        "strategy": {"okx_instrument": "ANTHROPIC-USDT-SWAP", "hyperliquid_coin": "ANT",
                     "hyperliquid_market_variant": "io", "target_leverage_okx": 4, "target_leverage_hl": 4},
        "conversion": {"okx_shares_per_contract": 2, "hl_io_shares_per_contract": 5,
                       "status": "exchange_confirmed", "units_confirmed": True,
                       "confirmed_by": "operator", "source": "https://official.example/rules"},
        "fx": {"usdc_usdt": 1, "verified": True, "timestamp": NOW, "source": "https://official.example/fx"},
        "fees": {"okx_taker": 0.001, "hl_taker": 0.002},
        "execution": {"target_shares": 100},
    }
    snapshots = {}
    for exchange, instrument, side, quantity, mark, entry, currency in (
        ("okx", "ANTHROPIC-USDT-SWAP", "short", -100, 120, 125, "USDT"),
        ("hl", "io:ANT", "long", 40, 250, 240, "USDC"),
    ):
        snapshots[exchange] = {
            "id": exchange, "status": "live", "received_at": NOW,
            "metadata": {"instrument": instrument, "variant": "io" if exchange == "hl" else None,
                         "verified": True, "contracts_per_raw_unit": 1,
                         "price_per_contract_multiplier": 1, "source": "https://official.example/meta"},
            "market": {"mark": mark, "bid": 119 if exchange == "okx" else 249,
                       "ask": 121 if exchange == "okx" else 251,
                       "bids": [[119, 25], [118, 1000]] if exchange == "okx" else [[249, 10], [248, 1000]],
                       "asks": [[121, 30], [122, 1000]] if exchange == "okx" else [[251, 10], [252, 1000]],
                       "quote_currency": currency, "funding_rate": .001 if exchange == "okx" else .0002,
                       "funding_interval_hours": 8 if exchange == "okx" else 1,
                       "timestamp": NOW, "received_at": NOW, "source": "https://official.example/market"},
            "position": {"side": side, "raw_quantity": quantity, "entry_price": entry,
                         "notional": 12000 if exchange == "okx" else 10000,
                         "unrealized_pnl": -20 if exchange == "okx" else 30,
                         "realized_pnl": 999999, "leverage": 4, "margin_mode": "isolated",
                         "liquidation_price": 180 if exchange == "okx" else 100,
                         "initial_margin": 3000, "maintenance_margin": 100,
                         "timestamp": NOW, "source": "https://official.example/position"},
            "account": {"margin_balance": 5000, "available_balance": 2000,
                        "maintenance_margin": 100, "timestamp": NOW},
            "ledger": {"complete": True, "realized_pnl": 100 if exchange == "okx" else 50,
                       "funding_pnl": 12 if exchange == "okx" else -5,
                       "fees_paid": 3 if exchange == "okx" else 2,
                       "funding_24h": 3 if exchange == "okx" else -1,
                       "funding_7d": 12 if exchange == "okx" else -5,
                       "source": "https://official.example/ledger"},
        }
    return snapshots, config


class EngineTests(unittest.TestCase):
    def calculate(self, snapshots=None, config=None):
        default_snapshots, default_config = fixture()
        return calculate(default_snapshots if snapshots is None else snapshots,
                         default_config if config is None else config, NOW)

    def codes(self, result):
        return {alert["code"] for alert in result["alerts"]}

    def test_confirmed_hedge_uses_actual_raw_quantity_and_explicit_side(self):
        result = self.calculate()
        self.assertEqual(result["hedge"]["okx_shares"], 200)
        self.assertEqual(result["hedge"]["hl_shares"], 200)
        self.assertEqual(result["hedge"]["share_difference"], 0)
        self.assertEqual(result["hedge"]["mismatch_pct"], 0)
        self.assertTrue(result["hedge"]["balanced"])
        self.assertEqual(result["hedge"]["status"], "balanced")
        snapshots, config = fixture()
        snapshots["okx"]["position"]["raw_quantity"] = 100
        self.assertEqual(self.calculate(snapshots, config)["hedge"]["okx_shares"], 200)

    def test_report_sample_arithmetic_is_calculated_instead_of_copied(self):
        snapshots, config = fixture()
        snapshots["okx"]["position"]["raw_quantity"] = 1440.42
        snapshots["hl"]["position"]["raw_quantity"] = 108.508
        config["conversion"].update(okx_shares_per_contract=1.275, hl_io_shares_per_contract=16.93)
        hedge = self.calculate(snapshots, config)["hedge"]
        self.assertAlmostEqual(hedge["okx_shares"], 1836.5355)
        self.assertAlmostEqual(hedge["hl_shares"], 1837.04044)
        self.assertAlmostEqual(hedge["share_difference"], 108.508 * 16.93 - 1440.42 * 1.275)
        self.assertNotAlmostEqual(hedge["okx_shares"], 1836.53, places=3)

    def test_provisional_rules_never_produce_balanced_verdict(self):
        snapshots, config = fixture()
        config["conversion"]["status"] = "provisional_not_exchange_confirmed"
        hedge = self.calculate(snapshots, config)["hedge"]
        self.assertEqual(hedge["mismatch_pct"], 0)
        self.assertTrue(hedge["provisional"])
        self.assertIsNone(hedge["balanced"])
        self.assertEqual(hedge["status"], "provisional")

    def test_units_require_both_metadata_and_operator_and_do_not_use_notional(self):
        for mutation in ("operator", "metadata", "quantity_multiplier", "price_multiplier", "conflicting_manual"):
            with self.subTest(mutation=mutation):
                snapshots, config = fixture()
                if mutation == "operator":
                    config["conversion"]["units_confirmed"] = False
                elif mutation == "metadata":
                    snapshots["okx"]["metadata"]["verified"] = False
                elif mutation == "quantity_multiplier":
                    snapshots["okx"]["metadata"]["contracts_per_raw_unit"] = None
                elif mutation == "price_multiplier":
                    snapshots["okx"]["metadata"]["price_per_contract_multiplier"] = None
                else:
                    config["conversion"]["okx_contracts_per_raw_unit"] = 10
                result = self.calculate(snapshots, config)
                if mutation != "price_multiplier":
                    self.assertIsNone(result["hedge"]["okx_shares"])
                self.assertIsNone(result["hedge"]["balanced"])
                self.assertIsNone(result["spread"]["gross_spread_pct"] if mutation != "quantity_multiplier" else result["spread"]["depth_spread_pct"])
                self.assertIn("units_unconfirmed", self.codes(result))

    def test_distinct_quantity_and_price_multipliers_apply_to_vwap_pnl_and_fees(self):
        snapshots, config = fixture()
        snapshots["okx"]["metadata"].update(contracts_per_raw_unit=2, price_per_contract_multiplier=3)
        result = self.calculate(snapshots, config)
        self.assertEqual(result["hedge"]["okx_shares"], 400)
        self.assertEqual(result["spread"]["okx_share_price"], 180)
        self.assertEqual(result["spread"]["entry_legs"]["okx"]["requested_raw"], 25)
        self.assertAlmostEqual(result["spread"]["entry_legs"]["okx"]["fees"], 25 * 2 * 3 * 119 * .001)
        self.assertAlmostEqual(result["spread"]["close_legs"]["okx"]["pnl"], 330 * 6)

    def test_mark_spread_and_depth_use_the_correct_sides_and_full_target_size(self):
        spread = self.calculate()["spread"]
        self.assertEqual(spread["okx_share_price"], 60)
        self.assertEqual(spread["hl_share_price"], 50)
        self.assertAlmostEqual(spread["gross_spread_pct"], 20)
        self.assertAlmostEqual(spread["top_of_book_pct"], (59.5 / 50.2 - 1) * 100)
        self.assertAlmostEqual(spread["entry_legs"]["okx"]["vwap"], 118.5)
        self.assertAlmostEqual(spread["entry_legs"]["hl"]["vwap"], 251.5)
        self.assertAlmostEqual(spread["depth_spread_pct"], (59.25 / 50.3 - 1) * 100)
        self.assertAlmostEqual(spread["estimated_fees_usdt"], 15.985)
        self.assertAlmostEqual(spread["estimated_slippage_usdt"], 35)
        self.assertAlmostEqual(spread["net_spread_pct"], (59.25 - 50.3 - 15.985 / 100) / 50.3 * 100)

    def test_capitalization_view_reports_equity_basis_convergence(self):
        cap = self.calculate()["capitalization"]
        self.assertAlmostEqual(cap["hl_share_price_usdt"], 50)
        self.assertAlmostEqual(cap["fd_share_price_usdt"], 24)
        self.assertAlmostEqual(cap["total_share_price_usdt"], 60)
        self.assertAlmostEqual(cap["fd_discount_pct"], -52)
        self.assertAlmostEqual(cap["remaining_gap_pct"], 20)
        self.assertEqual(cap["rows"][0]["label"], "OKX · io")

    def test_ant_scale_capitalization_uses_okx_fully_diluted_factor(self):
        snapshots, config = fixture()
        config["conversion"].update(okx_shares_per_contract=1.275, hl_io_shares_per_contract=16.93)
        snapshots["okx"]["market"]["mark"] = 144.82
        snapshots["hl"]["market"]["mark"] = 1679.3
        cap = self.calculate(snapshots, config)["capitalization"]
        self.assertAlmostEqual(cap["factors"]["okx_fully_diluted"], 1.693)
        self.assertAlmostEqual(cap["hl_share_price_usdt"], 1679.3 / 16.93)
        self.assertAlmostEqual(cap["fd_share_price_usdt"], 144.82 / 1.693)
        self.assertAlmostEqual(cap["total_share_price_usdt"], 144.82 / 1.275)

    def test_close_vwap_covers_current_positions_and_fees_are_not_double_counted(self):
        spread = self.calculate()["spread"]
        self.assertEqual(spread["close_legs"]["okx"]["requested_raw"], 100)
        self.assertEqual(spread["close_legs"]["hl"]["requested_raw"], 40)
        self.assertAlmostEqual(spread["close_legs"]["okx"]["vwap"], 121.7)
        self.assertAlmostEqual(spread["close_legs"]["hl"]["vwap"], 248.25)
        self.assertAlmostEqual(spread["close_pnl_usdt"], 660)
        self.assertAlmostEqual(spread["close_fees_usdt"], 32.03)
        self.assertAlmostEqual(spread["close_slippage_usdt"], 240)
        self.assertAlmostEqual(spread["estimated_exit_cost_usdt"], 272.03)
        self.assertAlmostEqual(spread["close_net_pnl_usdt"], 627.97)
        self.assertAlmostEqual(spread["theoretical_profit_usdt"], 2000)

    def test_insufficient_depth_stays_unknown_and_reports_available_quantity(self):
        snapshots, config = fixture()
        snapshots["okx"]["market"]["bids"] = [[119, 1]]
        snapshots["hl"]["market"]["bids"] = [[249, 1]]
        result = self.calculate(snapshots, config)
        self.assertIsNotNone(result["spread"]["top_of_book_pct"])
        self.assertIsNone(result["spread"]["depth_spread_pct"])
        self.assertIsNone(result["spread"]["net_spread_pct"])
        self.assertIsNone(result["spread"]["close_net_pnl_usdt"])
        self.assertEqual(result["spread"]["entry_legs"]["okx"]["available_raw"], 1)
        self.assertIn("entry_depth_insufficient", self.codes(result))
        self.assertIn("close_depth_insufficient", self.codes(result))

    def test_missing_or_invalid_fees_block_net_but_preserve_gross(self):
        for fee in (None, -1, float("nan")):
            snapshots, config = fixture()
            config["fees"]["okx_taker"] = fee
            result = self.calculate(snapshots, config)
            self.assertIsNotNone(result["spread"]["depth_spread_pct"])
            self.assertIsNone(result["spread"]["net_spread_pct"])
            self.assertIsNone(result["spread"]["close_net_pnl_usdt"])

    def test_actual_1h_and_8h_funding_cycles_and_both_rate_signs(self):
        result = self.calculate()
        self.assertEqual(result["funding"]["legs"]["okx"]["per_interval"], 12)
        self.assertEqual(result["funding"]["legs"]["hl"]["per_interval"], -2)
        self.assertEqual(result["funding"]["net_per_8h_usdt"], -4)
        snapshots, config = fixture()
        for exchange in snapshots:
            snapshots[exchange]["market"]["funding_rate"] *= -1
        self.assertEqual(self.calculate(snapshots, config)["funding"]["net_per_8h_usdt"], 4)

    def test_missing_funding_interval_never_assumes_eight_hours(self):
        snapshots, config = fixture()
        snapshots["hl"]["market"]["funding_interval_hours"] = None
        result = self.calculate(snapshots, config)
        self.assertIsNone(result["funding"]["legs"]["hl"]["per_interval"])
        self.assertIsNone(result["funding"]["net_per_8h_usdt"])

    def test_funding_warning_uses_normalized_eight_hour_cost(self):
        snapshots, config = fixture()
        snapshots["hl"]["market"]["funding_rate"] = .003
        result = self.calculate(snapshots, config)
        self.assertEqual(result["funding"]["net_per_8h_usdt"], -228)
        alert = next(alert for alert in result["alerts"] if alert["code"] == "funding_cost")
        self.assertEqual(alert["severity"], "critical")
        self.assertIn("八小时", alert["title"])

    def test_fx_is_required_verified_positive_and_not_older_than_sixty_seconds(self):
        for change in ({"verified": False}, {"usdc_usdt": None}, {"usdc_usdt": -1}, {"timestamp": NOW - 60001}):
            snapshots, config = fixture()
            config["fx"].update(change)
            result = self.calculate(snapshots, config)
            self.assertIsNone(result["spread"]["gross_spread_pct"])
            self.assertIsNone(result["pnl"]["unrealized_usdt"])
            self.assertIsNone(result["pnl"]["total_usdt"])
            self.assertIsNone(result["pnl"]["current_total_usdt"])
            self.assertIsNone(result["funding"]["net_per_8h_usdt"])
            self.assertEqual(result["funding"]["legs"]["hl"]["per_interval"], -2)
            self.assertEqual(result["pnl"]["legs"]["okx"]["unrealized_usdt"], -20)
        snapshots, config = fixture()
        config["fx"]["usdc_usdt"] = 1.02
        result = self.calculate(snapshots, config)
        self.assertAlmostEqual(result["pnl"]["unrealized_usdt"], 10.6)
        self.assertAlmostEqual(result["spread"]["gross_spread_pct"], (60 / 51 - 1) * 100)

    def test_pnl_only_totals_complete_separate_components_not_position_aggregate(self):
        pnl = self.calculate()["pnl"]
        self.assertEqual(pnl["unrealized_usdt"], 10)
        self.assertEqual(pnl["realized_usdt"], 150)
        self.assertEqual(pnl["funding_usdt"], 7)
        self.assertEqual(pnl["fees_usdt"], 5)
        self.assertEqual(pnl["total_usdt"], 162)
        self.assertEqual(pnl["current_total_usdt"], 162)
        self.assertEqual(pnl["current_total_mode"], "complete_strategy")
        self.assertEqual(pnl["legs"]["okx"]["current_total_usdt"], 89)
        self.assertEqual(pnl["legs"]["hl"]["current_total_usdt"], 73)
        self.assertEqual(pnl["legs"]["okx"]["current_total_mode"], "complete_exchange")
        for key in ("realized_pnl", "funding_pnl", "fees_paid", "complete"):
            snapshots, config = fixture()
            snapshots["okx"]["ledger"].pop(key)
            pnl = self.calculate(snapshots, config)["pnl"]
            self.assertIsNone(pnl["total_usdt"])
            self.assertEqual(pnl["current_total_usdt"], 10)
            self.assertEqual(pnl["current_total_mode"], "current_unrealized")
            self.assertEqual(pnl["legs"]["okx"]["current_total_usdt"], -20)
            self.assertEqual(pnl["legs"]["hl"]["current_total_usdt"], 73)

    def test_real_zero_ledger_totals_are_valid(self):
        snapshots, config = fixture()
        for snapshot in snapshots.values():
            snapshot["position"]["unrealized_pnl"] = 0
            snapshot["ledger"].update(realized_pnl=0, funding_pnl=0, fees_paid=0)
        self.assertEqual(self.calculate(snapshots, config)["pnl"]["total_usdt"], 0)
        self.assertEqual(self.calculate(snapshots, config)["pnl"]["current_total_usdt"], 0)

    def test_market_and_position_staleness_alert_at_15_and_hard_block_at_60(self):
        for part in ("market", "position"):
            for age in (16, 61):
                with self.subTest(part=part, age=age):
                    snapshots, config = fixture()
                    snapshots["okx"][part]["timestamp"] = NOW - age * 1000
                    result = self.calculate(snapshots, config)
                    self.assertIn("data_stale", self.codes(result))
                    if age > 60:
                        self.assertIsNone(result["hedge"]["mismatch_pct"])
                        self.assertFalse(result["hedge"]["realtime"])
                        self.assertIsNone(result["spread"]["close_pnl_usdt"])
                        if part == "market":
                            self.assertIsNone(result["spread"]["gross_spread_pct"])
                    else:
                        self.assertIsNotNone(result["hedge"]["mismatch_pct"])

    def test_api_failure_keeps_input_but_stops_live_calculation(self):
        snapshots, config = fixture()
        original_mark = snapshots["okx"]["market"]["mark"]
        snapshots["okx"]["status"] = "error"
        result = self.calculate(snapshots, config)
        self.assertEqual(snapshots["okx"]["market"]["mark"], original_mark)
        self.assertIsNone(result["spread"]["gross_spread_pct"])
        self.assertIsNone(result["hedge"]["balanced"])

    def test_public_market_remains_usable_without_private_credentials(self):
        snapshots, config = fixture()
        snapshots["okx"].update(status="unconfigured", public_status="live", private_status="unconfigured",
                                position=None, account=None)
        result = self.calculate(snapshots, config)
        self.assertAlmostEqual(result["spread"]["gross_spread_pct"], 20)
        self.assertIsNone(result["hedge"]["okx_shares"])
        self.assertIsNone(result["hedge"]["balanced"])
        self.assertTrue(result["risk"]["freshness"]["okx"]["market_realtime"])
        self.assertFalse(result["risk"]["freshness"]["okx"]["position_realtime"])
        self.assertFalse(any(alert["code"] == "data_stale" and alert["exchange"] == "okx" for alert in result["alerts"]))

    def test_fresh_order_book_does_not_hide_stale_funding_rate(self):
        snapshots, config = fixture()
        snapshots["hl"]["market"]["field_timestamps"] = {"funding_rate": NOW - 61000}
        result = self.calculate(snapshots, config)
        self.assertIsNotNone(result["spread"]["gross_spread_pct"])
        self.assertIsNone(result["funding"]["legs"]["hl"]["rate"])
        self.assertIsNone(result["funding"]["net_per_8h_usdt"])
        self.assertIn("funding_data_stale", self.codes(result))

    def test_unsorted_order_book_is_simulated_in_best_price_order(self):
        snapshots, config = fixture()
        snapshots["okx"]["market"]["bids"].reverse()
        snapshots["hl"]["market"]["asks"].reverse()
        result = self.calculate(snapshots, config)
        self.assertAlmostEqual(result["spread"]["entry_legs"]["okx"]["vwap"], 118.5)
        self.assertAlmostEqual(result["spread"]["entry_legs"]["hl"]["vwap"], 251.5)

    def test_wrong_side_variant_and_unknown_variant_never_return_balanced(self):
        for mutation in ("direction", "xyz", "para", "unknown_variant", "wrong_instrument"):
            snapshots, config = fixture()
            if mutation == "direction":
                snapshots["okx"]["position"]["side"] = "long"
            elif mutation == "wrong_instrument":
                snapshots["okx"]["metadata"]["instrument"] = "BTC-USDT-SWAP"
            else:
                snapshots["hl"]["metadata"]["variant"] = None if mutation == "unknown_variant" else mutation
            result = self.calculate(snapshots, config)
            self.assertIsNone(result["hedge"]["balanced"])
            if mutation == "unknown_variant":
                self.assertEqual(result["hedge"]["status"], "unknown")
            else:
                self.assertEqual(result["risk"]["status"], "critical")

    def test_flat_is_explicit_zero_not_zero_percent_balanced(self):
        snapshots, config = fixture()
        for snapshot in snapshots.values():
            snapshot["position"].update(side="flat", raw_quantity=0, notional=0,
                                        entry_price=None, leverage=None, unrealized_pnl=0)
        result = self.calculate(snapshots, config)
        self.assertEqual(result["hedge"]["status"], "flat")
        self.assertEqual(result["hedge"]["okx_shares"], 0)
        self.assertIsNone(result["hedge"]["mismatch_pct"])
        self.assertIsNone(result["hedge"]["balanced"])
        self.assertEqual(result["spread"]["close_pnl_usdt"], 0)
        self.assertNotIn("direction_wrong", self.codes(result))
        snapshots["okx"]["position"] = None
        self.assertIsNone(self.calculate(snapshots, config)["hedge"]["okx_shares"])

    def test_liquidation_loss_progress_alert_and_anomalous_liquidation(self):
        snapshots, config = fixture()
        snapshots["hl"]["position"].update(entry_price=300, unrealized_pnl=-2000, liquidation_price=199)
        result = self.calculate(snapshots, config)
        self.assertAlmostEqual(result["risk"]["legs"]["hl"]["liquidation_buffer_pct"], 20.4)
        self.assertAlmostEqual(result["risk"]["legs"]["hl"]["liquidation_loss_progress_pct"], 2000 / (2000 + 2040) * 100)
        self.assertNotIn("liquidation_loss_progress", self.codes(result))

        snapshots["hl"]["position"]["liquidation_price"] = 200
        result = self.calculate(snapshots, config)
        self.assertAlmostEqual(result["risk"]["legs"]["hl"]["liquidation_loss_progress_pct"], 50)
        item = next(alert for alert in result["alerts"] if alert["code"] == "liquidation_loss_progress")
        self.assertEqual(item["severity"], "critical")
        self.assertEqual(item["threshold"], 50)

        snapshots["hl"]["position"]["liquidation_price"] = 250 * (1 + 0.05)
        result = self.calculate(snapshots, config)
        self.assertAlmostEqual(result["risk"]["legs"]["hl"]["liquidation_buffer_pct"], -5)
        item = next(alert for alert in result["alerts"] if alert["code"] == "liquidation_anomaly")
        self.assertEqual(item["severity"], "critical")

    def test_independent_balance_and_leverage_risks(self):
        snapshots, config = fixture()
        snapshots["okx"]["account"]["margin_balance"] = 150
        snapshots["hl"]["account"]["margin_balance"] = 9999999
        snapshots["hl"]["position"]["leverage"] = 5
        result = self.calculate(snapshots, config)
        self.assertIn("margin_balance_low", self.codes(result))
        self.assertIn("leverage_mismatch", self.codes(result))
        self.assertEqual(result["risk"]["legs"]["okx"]["balance_coverage_ratio"], 1.5)

    def test_missing_or_nonfinite_data_is_json_safe_and_alerts_have_provenance(self):
        snapshots, config = fixture()
        snapshots["okx"]["market"]["mark"] = float("nan")
        snapshots["hl"]["position"]["notional"] = float("inf")
        config["fees"]["hl_taker"] = float("nan")
        result = self.calculate(snapshots, config)
        self.assertIsNone(result["spread"]["gross_spread_pct"])
        self.assertIsNone(result["funding"]["net_per_8h_usdt"])
        json.dumps(result, allow_nan=False)
        for alert in self.calculate({}, {})["alerts"]:
            self.assertEqual(set(alert), {"code", "severity", "exchange", "title", "message", "value",
                                          "threshold", "formula", "source", "timestamp"})
            self.assertTrue(alert["formula"])
            self.assertTrue(alert["source"])


if __name__ == "__main__":
    unittest.main()
