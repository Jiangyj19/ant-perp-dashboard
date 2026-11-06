"""HTTP authorization, input validation and atomic configuration audits."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from antmonitor.app import SESSION_MAX_AGE_SECONDS, create_app
from antmonitor.settings import ROOT, normalize_bark_settings


class AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = {
            "data_dir": self.directory.name, "config_path": str(ROOT / "config/strategy.yaml"),
            "dashboard_password": "", "okx_base_url": "https://www.okx.com",
            "hl_base_url": "https://api.hyperliquid.xyz", "enable_public_ws": False,
            "okx_api_key": "TEST-API-KEY-MUST-NOT-LEAK",
            "okx_api_secret": "TEST-API-SECRET-MUST-NOT-LEAK", "okx_passphrase": "TEST-PASSPHRASE-MUST-NOT-LEAK",
            "hl_address": "0x0123456789abcdef0123456789abcdef01234567",
            "bark_enabled": False, "bark_device_key": "TEST-BARK-KEY-MUST-NOT-LEAK",
            "telegram_bot_token": "TEST-TELEGRAM-SECRET-MUST-NOT-LEAK", "telegram_enabled": False,
        }

    def tearDown(self):
        self.directory.cleanup()

    def client(self, **kwargs):
        self.app = create_app(copy.deepcopy(self.settings), start_monitor=False)
        return TestClient(self.app, raise_server_exceptions=False, **kwargs)

    @staticmethod
    def change(client, config, **kwargs):
        return client.put("/api/config", json={"config": config, "actor": "测试复核人",
                                               "reason": "人工核对官方参数后的测试修改", **kwargs})

    def test_without_password_local_requests_work_and_remote_requests_are_denied(self):
        with self.client() as client:
            self.assertEqual(client.get("/api/status").status_code, 200)
            status = client.get("/api/auth/status").json()
            self.assertFalse(status["required"])
            self.assertTrue(status["local_only"])
            self.assertEqual(client.get("/api/status", headers={"Host": "attacker.invalid"}).status_code, 403)
        with self.client(client=("198.51.100.20", 12000)) as client:
            self.assertEqual(client.get("/api/status").status_code, 403)
            self.assertEqual(client.get("/").status_code, 403)

    def test_password_authentication_cookie_tamper_and_logout(self):
        self.settings["dashboard_password"] = "valid-test-password-1234"
        with self.client(client=("198.51.100.20", 12000)) as client:
            self.assertEqual(client.get("/api/status").status_code, 401)
            self.assertFalse(client.get("/api/auth/status").json()["authenticated"])
            self.assertEqual(client.post("/api/auth/login", json={"password": "wrong"}).status_code, 401)
            login = client.post("/api/auth/login", json={"password": self.settings["dashboard_password"]})
            self.assertEqual(login.status_code, 200)
            cookie = login.headers["set-cookie"].lower()
            self.assertIn("httponly", cookie)
            self.assertIn("samesite=strict", cookie)
            self.assertIn(f"max-age={SESSION_MAX_AGE_SECONDS}", cookie)
            self.assertEqual(client.get("/api/status").status_code, 200)
            token = client.cookies["ant_session"]
            client.cookies.set("ant_session", token + "tampered", domain="testserver.local", path="/")
            self.assertEqual(client.get("/api/status").status_code, 401)
            client.cookies.clear()
            client.post("/api/auth/login", json={"password": self.settings["dashboard_password"]})
            self.assertEqual(client.post("/api/auth/logout").status_code, 200)
            self.assertEqual(client.get("/api/status").status_code, 401)

    def test_authentication_cookie_survives_app_restart(self):
        self.settings["dashboard_password"] = "valid-test-password-1234"
        with self.client(client=("198.51.100.20", 12000)) as client:
            response = client.post("/api/auth/login", json={"password": self.settings["dashboard_password"]})
            self.assertEqual(response.status_code, 200)
            token = client.cookies["ant_session"]
        with self.client(client=("198.51.100.20", 12000)) as client:
            client.cookies.set("ant_session", token, domain="testserver.local", path="/")
            self.assertEqual(client.get("/api/status").status_code, 200)

    def test_cross_site_state_changes_are_rejected_before_mutation(self):
        with self.client() as client:
            config = client.get("/api/config").json()
            response = client.put("/api/config", headers={"Origin": "https://attacker.invalid"},
                                  json={"config": config, "actor": "operator", "reason": "valid reason"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(client.get("/api/config").json(), config)
            self.assertIsNone(self.app.state.store.get("config"))

    def test_whitespace_audit_identity_and_reason_are_rejected(self):
        with self.client() as client:
            config = client.get("/api/config").json()
            for fields in ({"actor": " "}, {"reason": "   "}):
                response = self.change(client, config, **fields)
                self.assertEqual(response.status_code, 422)
            self.assertIsNone(self.app.state.store.get("config"))

    def test_notification_test_requires_a_configured_channel(self):
        with self.client() as client:
            self.assertEqual(client.post("/api/alerts/test").status_code, 409)

    def test_bark_device_key_accepts_plain_key_or_copied_url(self):
        self.assertEqual(normalize_bark_settings("https://api.day.app", "abc123"), ("https://api.day.app", "abc123"))
        self.assertEqual(normalize_bark_settings("https://api.day.app", "https://api.day.app/abc123/title/body"),
                         ("https://api.day.app", "abc123"))

    def test_public_api_data_omits_credentials_and_full_wallet_address(self):
        with self.client() as client:
            for path in ("/api/status", "/api/config", "/api/snapshot", "/api/events"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, path)
                for key in ("okx_api_key", "okx_api_secret", "okx_passphrase", "bark_device_key", "telegram_bot_token", "hl_address"):
                    self.assertNotIn(self.settings[key], response.text, (path, key))
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertIn("…", client.get("/api/status").json()["hl"]["address_masked"])

    def test_configuration_save_records_old_new_actor_reason_and_source(self):
        with self.client() as client:
            original = client.get("/api/config").json()
            revised = copy.deepcopy(original)
            revised["execution"]["target_shares"] += 1
            response = self.change(client, revised)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/api/config").json(), revised)
            self.assertEqual(self.app.state.store.get("config"), revised)
            audit = next(row for row in client.get("/api/events").json() if row.get("title") == "监控配置人工更新")
            self.assertEqual(audit["old"], original)
            self.assertEqual(audit["new"], revised)
            self.assertEqual(audit["actor"], "测试复核人")
            self.assertEqual(audit["source"], revised["conversion"]["source"])
            self.assertTrue(audit["reason"])

    def test_failed_audit_insert_rolls_back_configuration_transaction(self):
        with self.client() as client:
            baseline = client.get("/api/config").json()
            self.assertEqual(self.change(client, baseline).status_code, 200)
            store = self.app.state.store
            prior_events = store.events()
            store.db.execute("CREATE TRIGGER reject_test_audit BEFORE INSERT ON events WHEN NEW.kind='rule_audit' BEGIN SELECT RAISE(ABORT, 'test audit failure'); END")
            store.db.commit()
            revised = copy.deepcopy(baseline)
            revised["execution"]["target_shares"] += 10
            self.assertEqual(self.change(client, revised).status_code, 500)
            self.assertEqual(store.get("config"), baseline)
            self.assertEqual(self.app.state.monitor.config, baseline)
            self.assertEqual(store.events(), prior_events)

    def test_threshold_only_save_cannot_acknowledge_pending_rule_change(self):
        with self.client() as client:
            config = client.get("/api/config").json()
            self.app.state.store.set("rule_unreviewed", True)
            config["thresholds"]["net_spread_min_pct"] += .1
            self.assertEqual(self.change(client, config).status_code, 200)
            self.assertIs(self.app.state.store.get("rule_unreviewed"), True)
            config["conversion"].update(confirmed_by="operator", source="https://www.okx.com/official-rules-review", last_reviewed="2026-11-06")
            self.assertEqual(self.change(client, config).status_code, 200)
            self.assertIs(self.app.state.store.get("rule_unreviewed"), True)
            self.assertEqual(self.change(client, config, review_rules=True).status_code, 200)
            self.assertIs(self.app.state.store.get("rule_unreviewed"), False)

    def test_illegal_and_unknown_configuration_is_rejected_without_audit_or_mutation(self):
        with self.client() as client:
            baseline = client.get("/api/config").json()
            invalid = []
            extra = copy.deepcopy(baseline)
            extra["api_secret"] = "should never be a strategy setting"
            invalid.append(extra)
            extra = copy.deepcopy(baseline)
            extra["strategy"]["api_secret"] = "unknown nested key"
            invalid.append(extra)
            extra = copy.deepcopy(baseline)
            extra["conversion"] = []
            invalid.append(extra)
            extra = copy.deepcopy(baseline)
            extra["strategy"]["target_leverage_okx"] = True
            invalid.append(extra)
            extra = copy.deepcopy(baseline)
            extra["strategy"]["hyperliquid_market_variant"] = "xyz"
            invalid.append(extra)
            extra = copy.deepcopy(baseline)
            extra["fees"]["okx_taker"] = -1
            invalid.append(extra)
            extra = copy.deepcopy(baseline)
            extra["rules"]["sources"] = ["not an object"]
            invalid.append(extra)
            extra = copy.deepcopy(baseline)
            extra["rules"]["sources"] = [{"url": "http://127.0.0.1/private", "enabled": True}]
            invalid.append(extra)
            extra = copy.deepcopy(baseline)
            extra["conversion"].update(status="exchange_confirmed", source=" ", confirmed_by=" ", last_reviewed=" ")
            invalid.append(extra)
            previous_events = self.app.state.store.events()
            for index, config in enumerate(invalid):
                with self.subTest(index=index):
                    response = self.change(client, config)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(client.get("/api/config").json(), baseline)
                    self.assertEqual(self.app.state.store.events(), previous_events)


if __name__ == "__main__":
    unittest.main()
