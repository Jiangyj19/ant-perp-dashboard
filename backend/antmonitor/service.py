import asyncio
import copy
import hashlib
import time

import httpx

from .engine import calculate
from .exchanges import HyperliquidAdapter, OKXAdapter
from .notifications import Notifier
from .rules import RuleWatcher


def now_ms():
    return int(time.time() * 1000)


class Monitor:
    def __init__(self, settings, store, config):
        self.settings, self.store, self.config = settings, store, config
        self.adapters = {"okx": OKXAdapter(settings), "hl": HyperliquidAdapter(settings)}
        self.snapshots = {}
        self.metrics = calculate({}, config, now_ms())
        self.notifier = Notifier(settings, store)
        self.rule_watcher = RuleWatcher(store)
        self.client = httpx.AsyncClient(timeout=8)
        self.tasks = []
        self.fx = {"usdc_usdt": None, "verified": False, "timestamp": None, "source": ""}
        self.last_sample = 0
        self.last_prune = 0
        self.last_positions = store.get("position_signatures", {})

    async def start(self):
        for adapter in self.adapters.values():
            if hasattr(adapter, "configure"):
                adapter.configure(self.config)
            if hasattr(adapter, "start"):
                result = adapter.start()
                if hasattr(result, "__await__"):
                    await result
        self.tasks = [asyncio.create_task(self.poll_loop()), asyncio.create_task(self.fx_loop()), asyncio.create_task(self.rule_loop())]

    async def fx_loop(self):
        while True:
            try:
                response = await self.client.get(self.settings["okx_base_url"] + "/api/v5/market/ticker", params={"instId": "USDC-USDT"})
                response.raise_for_status()
                payload = response.json()
                row = payload["data"][0]
                bid, ask, ts = float(row["bidPx"]), float(row["askPx"]), int(row["ts"])
                if payload.get("code") == "0" and 0.5 < bid <= ask < 1.5 and abs(now_ms() - ts) < 60000:
                    self.fx = {"usdc_usdt": (bid + ask) / 2, "verified": True, "timestamp": ts,
                               "source": self.settings["okx_base_url"] + "/api/v5/market/ticker?instId=USDC-USDT"}
            except Exception:
                pass  # Keep source timestamp. Engine expires FX after 60 seconds.
            await asyncio.sleep(5)

    async def rule_loop(self):
        while True:
            try:
                await self.rule_watcher.poll(self.config)
            except Exception:
                self.store.event("system", {"severity": "warning", "title": "公告监控暂时不可用", "message": "检查官方来源与网络，换算参数未被修改。"})
            await asyncio.sleep(30)

    async def fetch(self, key, adapter):
        try:
            result = await asyncio.wait_for(adapter.poll(), timeout=20)
            if not isinstance(result, dict):
                raise ValueError("invalid adapter response")
        except Exception:
            result = {"id": key, "status": "error", "errors": ["交易所 API 请求失败，请检查网络、凭证和限频。"]}
        raw = result.pop("_raw", None)
        if raw:
            self.store.raw(now_ms(), key, raw)
        old = self.snapshots.get(key, {})
        # Preserve actual old values and original timestamps if a request fails.
        if result.get("status") in {"error", "stale"}:
            for field in ("market", "position", "account", "metadata", "ledger", "received_at"):
                if result.get(field) is None and old.get(field) is not None:
                    result[field] = old[field]
        self.snapshots[key] = result

    def effective_snapshots(self):
        snapshots = copy.deepcopy(self.snapshots)
        conv = self.config["conversion"]
        for key, snap in snapshots.items():
            meta = snap.get("metadata") or {}
            # Config may confirm a mapping only on top of a live, exact instrument specification.
            if conv.get("units_confirmed") and meta.get("instrument") and meta.get("specification_verified", meta.get("verified", False)):
                meta["contracts_per_raw_unit"] = conv.get(key + "_contracts_per_raw_unit")
                meta["price_per_contract_multiplier"] = conv.get(key + "_price_per_contract_multiplier")
                meta["verified"] = True
                meta["mapping_source"] = conv.get("source")
            snap["metadata"] = meta
        return snapshots

    async def poll_loop(self):
        while True:
            started = time.monotonic()
            try:
                await asyncio.gather(*(self.fetch(key, adapter) for key, adapter in self.adapters.items()))
                config = {**self.config, "fx": self.fx}
                current = self.effective_snapshots()
                timestamp = now_ms()
                self.metrics = calculate(current, config, timestamp)
                alerts = self.metrics.get("alerts", [])
                if self.store.get("rule_unreviewed", False):
                    alerts.append({"code": "RULE_REVIEW_REQUIRED", "severity": "critical", "exchange": None,
                        "title": "官方来源变化待复核", "message": "已保存公告原文。请人工核对并在规则配置保存复核记录。",
                        "value": "unreviewed", "threshold": "reviewed", "formula": "官方来源哈希变化且未人工确认", "source": "配置的官方公告页", "timestamp": timestamp})
                # Detect actual raw quantity changes; no comparison of UI notional values.
                for key, snapshot in current.items():
                    position = snapshot.get("position")
                    if position is not None and snapshot.get("status") == "live":
                        signature = [position.get("side"), position.get("raw_quantity")]
                        previous = self.last_positions.get(key)
                        if previous is not None and previous != signature:
                            self.store.event("position_change", {"severity": "warning", "title": "原始持仓数量或方向变化", "exchange": key,
                                "old": previous, "new": signature, "source": position.get("source"), "message": "请核对是否为手工调整或平台结算。"})
                            self.store.set("pending_position_change:" + key, {"timestamp": timestamp, "old": previous, "new": signature})
                        self.last_positions[key] = signature
                    pending = self.store.get("pending_position_change:" + key)
                    if pending and timestamp - pending["timestamp"] < self.config["thresholds"]["cooldown_seconds"] * 1000:
                        alerts.append({"code": "POSITION_CHANGE", "severity": "warning", "exchange": key, "title": "持仓发生变化",
                            "message": "原始数量或方向与上次快照不同，请核对交易所账户。", "value": pending["new"], "threshold": pending["old"],
                            "formula": "本次 [side, raw_quantity] ≠ 上次快照", "source": (position or {}).get("source"), "timestamp": pending["timestamp"]})
                self.store.set("position_signatures", self.last_positions)
                history = self.store.history(1)
                current_spread = self.metrics.get("spread", {}).get("gross_spread_pct")
                baseline = next((row for row in history if timestamp - row["timestamp"] <= 300000 and row.get("gross_spread_pct") is not None), None)
                if current_spread is not None and baseline and timestamp - baseline["timestamp"] >= 240000:
                    delta = current_spread - baseline["gross_spread_pct"]
                    if abs(delta) > 2:
                        alerts.append({"code": "SPREAD_MOVE", "severity": "warning", "exchange": None, "title": "价差快速变化",
                            "message": "近 5 分钟价差变化超过 2 个百分点，请检查报价与规则风险。", "value": delta, "threshold": 2,
                            "formula": "当前毛价差百分比 − 约5分钟前毛价差百分比", "source": "OKX / Hyperliquid 官方行情", "timestamp": timestamp})
                await self.notifier.process(alerts, self.config["thresholds"]["cooldown_seconds"], metrics=self.metrics, snapshots=current)
                if timestamp - self.last_sample >= 15000:
                    self.store.sample(timestamp, {
                        "gross_spread_pct": current_spread,
                        "net_spread_pct": self.metrics.get("spread", {}).get("net_spread_pct"),
                        "mismatch_pct": self.metrics.get("hedge", {}).get("mismatch_pct"),
                        "total_pnl": self.metrics.get("pnl", {}).get("total_usdt"),
                        "funding_pnl": self.metrics.get("funding", {}).get("cumulative_usdt"),
                        "okx_mark": (current.get("okx", {}).get("market") or {}).get("mark"),
                        "hl_mark": (current.get("hl", {}).get("market") or {}).get("mark"),
                    })
                    self.last_sample = timestamp
                if timestamp - self.last_prune > 3600000:
                    self.store.prune()
                    self.last_prune = timestamp
            except asyncio.CancelledError:
                raise
            except Exception:
                self.store.event("system", {"severity": "warning", "title": "监控轮询异常", "message": "保留原始时间戳，当前数据不保证新鲜；下一周期自动重试。"})
            await asyncio.sleep(max(0.1, self.settings["poll_seconds"] - (time.monotonic() - started)))

    def snapshot(self):
        # Recalculate freshness on EVERY browser request, even if polling is stalled.
        effective_config = {**copy.deepcopy(self.config), "fx": copy.deepcopy(self.fx)}
        snapshots = self.effective_snapshots()
        metrics = calculate(snapshots, effective_config, now_ms())
        extra_codes = {"RULE_REVIEW_REQUIRED", "POSITION_CHANGE", "SPREAD_MOVE"}
        extra_alerts = [alert for alert in self.metrics.get("alerts", []) if alert.get("code") in extra_codes]
        metrics["alerts"] = [*metrics.get("alerts", []), *extra_alerts]
        return {"generated_at": now_ms(), "exchanges": snapshots, "config": effective_config, "metrics": metrics,
                "alerts": metrics["alerts"], "events": self.store.events(50), "history": self.store.history(24), "readonly": True}

    def configure(self, config):
        old = self.config
        self.config = config
        if old["strategy"] != config["strategy"]:
            self.snapshots.clear()
        for adapter in self.adapters.values():
            if hasattr(adapter, "configure"):
                adapter.configure(config)
        self.rule_watcher.last_run = 0

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(adapter.close() for adapter in self.adapters.values()), self.notifier.close(), self.rule_watcher.close(), self.client.aclose(), return_exceptions=True)
