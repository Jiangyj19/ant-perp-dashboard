"""Read-only service integration and notification state transitions, no network."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from antmonitor.notifications import Notifier
from antmonitor.service import Monitor
from antmonitor.settings import ROOT, read_config, validate_config
from antmonitor.storage import Store


NOW = 1_800_000_000_000


class MemoryStore:
    def __init__(self):
        self.values, self.events_saved, self.raw_saved = {}, [], []

    def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    def set(self, key, value):
        self.values[key] = copy.deepcopy(value)

    def event(self, kind, value):
        self.events_saved.append((kind, copy.deepcopy(value)))

    def raw(self, timestamp, exchange, value):
        self.raw_saved.append((timestamp, exchange, value))

    def events(self, limit=50):
        return []

    def history(self, hours=24):
        return []


def warning(code="funding_cost", severity="warning", exchange=None):
    return {"code": code, "severity": severity, "exchange": exchange,
            "title": "风险提示", "message": "请核对账户", "value": 55,
            "threshold": 50, "formula": "x > threshold", "source": "official API", "timestamp": NOW}


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.notifier = Notifier({}, self.store)
        self.notifier.send = AsyncMock(return_value={"bark": "sent"})

    async def asyncTearDown(self):
        await self.notifier.close()

    async def process(self, alerts, offset=0, **kwargs):
        with patch("antmonitor.notifications.time.time", return_value=(NOW + offset) / 1000):
            await self.notifier.process(alerts, 600, **kwargs)

    async def test_cooldown_and_immediate_escalation(self):
        await self.process([warning()])
        await self.process([warning()], 10000)
        self.assertEqual(self.notifier.send.await_count, 1)
        await self.process([warning(severity="critical")], 11000)
        self.assertEqual(self.notifier.send.await_count, 2)
        await self.process([warning(severity="critical")], 612000)
        self.assertEqual(self.notifier.send.await_count, 3)

    async def test_unknown_disappearing_metric_is_not_recovery_and_state_survives(self):
        await self.process([warning()])
        await self.process([], 10000, metrics={"funding": {"net_per_8h_usdt": None}})
        await self.process([], 20000)
        self.assertEqual(self.notifier.send.await_count, 1)
        self.assertIn("funding_cost:None", self.store.get("alert_states"))
        await self.process([], 30000, metrics={"funding": {"net_per_8h_usdt": 0}})
        self.assertEqual(self.notifier.send.await_count, 2)
        self.assertEqual(self.store.events_saved[-1][0], "recovery")
        self.assertEqual(self.store.get("alert_states"), {})

    async def test_liquidation_threshold_escalations_and_downgrades_share_one_state(self):
        for index, code in enumerate(("liquidation_warning", "liquidation_orange",
                                      "liquidation_critical", "liquidation_severe")):
            await self.process([warning(code, "critical" if index >= 2 else "warning", "hl")], index * 1000)
        self.assertEqual(self.notifier.send.await_count, 4)
        self.assertEqual(len(self.store.get("alert_states")), 1)
        await self.process([warning("liquidation_orange", "warning", "hl")], 5000)
        self.assertEqual(self.notifier.send.await_count, 4)
        self.assertFalse(any(kind == "recovery" for kind, _ in self.store.events_saved))
        await self.process([], 6000, metrics={"risk": {"legs": {"hl": {"liquidation_buffer_pct": None}}}})
        self.assertEqual(self.notifier.send.await_count, 4)
        await self.process([], 7000, metrics={"risk": {
            "legs": {"hl": {"liquidation_buffer_pct": 35}},
            "freshness": {"hl": {"position_realtime": True, "market_realtime": True}}}})
        self.assertEqual(self.notifier.send.await_count, 5)
        self.assertEqual(self.store.events_saved[-1][0], "recovery")

    async def test_duplicate_conditions_keep_highest_risk(self):
        await self.process([warning(severity="critical"), warning(severity="warning")])
        self.assertEqual(self.notifier.send.await_count, 1)
        self.assertEqual(self.notifier.send.call_args.args[0]["severity"], "critical")

    async def test_failed_delivery_retries_after_thirty_seconds(self):
        self.notifier.send.return_value = {"bark": "failed"}
        await self.process([warning()])
        await self.process([warning()], 10000)
        self.assertEqual(self.notifier.send.await_count, 1)
        await self.process([warning()], 30000)
        self.assertEqual(self.notifier.send.await_count, 2)

    async def test_failed_recovery_is_persisted_and_retry_is_bounded(self):
        await self.process([warning()])
        self.notifier.send.return_value = {"bark": "failed"}
        healthy = {"funding": {"net_per_8h_usdt": 10}}
        await self.process([], 10000, metrics=healthy)
        await self.process([], 20000, metrics=healthy)
        self.assertEqual(self.notifier.send.await_count, 2)
        self.assertTrue(self.store.get("alert_states"))
        self.notifier.send.return_value = {"bark": "sent"}
        await self.process([], 40000, metrics=healthy)
        self.assertEqual(self.notifier.send.await_count, 3)
        self.assertEqual(self.store.get("alert_states"), {})

    async def test_unknown_timestamp_does_not_abort_real_notification_formatting(self):
        notifier = Notifier({"bark_enabled": True, "bark_device_key": "test-key", "bark_server": "https://api.day.app"}, self.store)
        notifier.client.post = AsyncMock(return_value=httpx.Response(200, json={"code": 200}, request=httpx.Request("POST", "https://api.day.app/push")))
        try:
            result = await notifier.send({**warning(), "timestamp": None})
            self.assertEqual(result, {"bark": "sent"})
            payload = notifier.client.post.call_args.kwargs["json"]
            self.assertIn("数据 UTC：待核实", payload["body"])
            self.assertNotIn("test-key", payload["body"])
        finally:
            await notifier.close()

    async def test_bark_default_minimum_skips_info_notifications(self):
        notifier = Notifier({"bark_enabled": True, "bark_device_key": "test-key", "bark_server": "https://api.day.app",
                             "bark_min_severity": "warning"}, self.store)
        notifier.client.post = AsyncMock()
        try:
            result = await notifier.send({**warning(severity="info"), "title": "信息提示"})
            self.assertEqual(result, {"bark": "skipped"})
            notifier.client.post.assert_not_called()
        finally:
            await notifier.close()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.config = read_config(ROOT / "config/strategy.yaml")
        self.monitor = Monitor({"enable_public_ws": False}, self.store, self.config)

    async def asyncTearDown(self):
        await self.monitor.close()

    async def test_failure_keeps_last_data_and_original_source_timestamps(self):
        original = {"id": "okx", "status": "live", "received_at": NOW,
                    "market": {"mark": 120, "timestamp": NOW},
                    "position": {"raw_quantity": 10, "timestamp": NOW},
                    "account": {"available_balance": 100, "timestamp": NOW},
                    "metadata": {"instrument": "ANTHROPIC-USDT-SWAP"}, "ledger": {"complete": False}}
        self.monitor.snapshots["okx"] = original
        adapter = AsyncMock()
        adapter.poll.side_effect = RuntimeError("do-not-log-this-secret")
        await self.monitor.fetch("okx", adapter)
        cached = self.monitor.snapshots["okx"]
        self.assertEqual(cached["status"], "error")
        self.assertEqual(cached["received_at"], NOW)
        self.assertEqual(cached["market"]["timestamp"], NOW)
        self.assertEqual(cached["position"]["raw_quantity"], 10)
        self.assertNotIn("do-not-log-this-secret", str(cached))

    async def test_private_raw_responses_are_persisted_but_not_public(self):
        adapter = AsyncMock()
        adapter.poll.return_value = {"id": "okx", "status": "live", "market": {"timestamp": NOW}, "_raw": {"balance": {"sensitive_account_field": "test"}}}
        await self.monitor.fetch("okx", adapter)
        self.assertEqual(len(self.store.raw_saved), 1)
        self.assertNotIn("_raw", self.monitor.snapshots["okx"])
        with patch("antmonitor.service.now_ms", return_value=NOW):
            self.assertNotIn("sensitive_account_field", str(self.monitor.snapshot()))

    async def test_mapping_confirmation_does_not_mutate_original_metadata(self):
        self.config["conversion"].update(units_confirmed=True, okx_contracts_per_raw_unit=.01,
                                         okx_price_per_contract_multiplier=1)
        self.monitor.snapshots["okx"] = {"metadata": {"instrument": "ANTHROPIC-USDT-SWAP", "specification_verified": True, "verified": False}}
        effective = self.monitor.effective_snapshots()
        self.assertEqual(effective["okx"]["metadata"]["contracts_per_raw_unit"], .01)
        self.assertNotIn("contracts_per_raw_unit", self.monitor.snapshots["okx"]["metadata"])
        self.monitor.snapshots["okx"]["metadata"]["specification_verified"] = False
        self.assertNotIn("contracts_per_raw_unit", self.monitor.effective_snapshots()["okx"]["metadata"])

    async def test_api_snapshot_recalculates_staleness_if_polling_stops(self):
        self.monitor.snapshots["okx"] = {"status": "live", "market": {"mark": 120, "timestamp": NOW}, "position": {"timestamp": NOW}}
        with patch("antmonitor.service.now_ms", return_value=NOW + 61000):
            snapshot = self.monitor.snapshot()
        self.assertFalse(snapshot["metrics"]["risk"]["freshness"]["okx"]["market_realtime"])
        self.assertFalse(snapshot["metrics"]["hedge"]["realtime"])


class PersistenceTests(unittest.TestCase):
    def test_audit_history_and_state_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.set("alert_states", {"funding_cost:None": {"rank": 10, "sent_at": NOW}})
            store.event("rule_audit", {"actor": "operator", "old": 1.275, "new": 1.28, "source": "official", "reason": "manual review"})
            store.close()
            store = Store(directory)
            try:
                self.assertEqual(store.get("alert_states")["funding_cost:None"]["rank"], 10)
                self.assertEqual(store.events()[0]["actor"], "operator")
                self.assertEqual(store.events()[0]["old"], 1.275)
            finally:
                store.close()

    def test_config_confirmation_requires_source_actor_date_and_unit_multipliers(self):
        config = read_config(ROOT / "config/strategy.yaml")
        config["conversion"]["units_confirmed"] = True
        with self.assertRaises(ValueError):
            validate_config(config)


if __name__ == "__main__":
    unittest.main()
