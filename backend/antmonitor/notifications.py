"""Outbound risk alerts, with persistent cooldown, escalation and recovery."""
import math
import re
import time

import httpx


class Notifier:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.client = httpx.AsyncClient(timeout=10, follow_redirects=False)

    @staticmethod
    def _severity_rank_name(value):
        return {"info": 0, "warning": 10, "critical": 20}.get(str(value or "warning").lower(), 10)

    async def send(self, alert):
        s = self.settings
        stamp = alert.get("timestamp")
        try:
            stamp_text = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(stamp) / 1000)) if stamp is not None else "待核实"
        except (ValueError, TypeError, OverflowError, OSError):
            stamp_text = "待核实"
        # Only calculated monitoring values; no key, wallet or raw account payload.
        body = "\n".join([
            str(alert.get("message", ""))[:1200],
            f"市场：{alert.get('exchange') or 'ANT 策略'}",
            f"数值：{alert.get('value')}；阈值：{alert.get('threshold')}",
            f"公式：{alert.get('formula', '状态检查')}",
            f"来源：{alert.get('source', '监控服务')}",
            f"数据 UTC：{stamp_text}",
            "检查交易所账户、网络和规则参数；本服务不执行交易。",
        ])
        for key in ("okx_api_key", "okx_api_secret", "okx_passphrase", "telegram_bot_token", "bark_device_key", "hl_address"):
            value = s.get(key)
            if value:
                body = body.replace(str(value), "[已脱敏]")
        body = re.sub(r"0x[0-9a-fA-F]{40}", lambda match: match.group(0)[:6] + "…" + match.group(0)[-4:], body)
        results = {}
        if s.get("bark_enabled") and s.get("bark_device_key"):
            if self._severity_rank_name(alert.get("severity")) < self._severity_rank_name(s.get("bark_min_severity", "warning")):
                results["bark"] = "skipped"
            else:
                critical = s.get("bark_critical") and alert.get("severity") == "critical"
                payload = {
                    "device_key": s["bark_device_key"], "title": f"ANT · {alert.get('title', '风险提示')}",
                    "body": body, "group": "ANT 套利监控", "isArchive": 1,
                    "level": "critical" if critical else "timeSensitive",
                }
                if critical:
                    payload["volume"] = s.get("bark_volume", 5)
                    payload["sound"] = "alarm"
                try:
                    response = await self.client.post(s["bark_server"] + "/push", json=payload)
                    response.raise_for_status()
                    if response.json().get("code") != 200:
                        raise ValueError("push rejected")
                    results["bark"] = "sent"
                except Exception:
                    results["bark"] = "failed"
        if s.get("telegram_enabled") and s.get("telegram_bot_token") and s.get("telegram_chat_id"):
            try:
                response = await self.client.post("https://api.telegram.org/bot" + s["telegram_bot_token"] + "/sendMessage", json={
                    "chat_id": s["telegram_chat_id"], "text": f"ANT · {alert.get('title', '风险提示')}\n{body}",
                })
                response.raise_for_status()
                if response.json().get("ok") is not True:
                    raise ValueError("push rejected")
                results["telegram"] = "sent"
            except Exception:
                results["telegram"] = "failed"
        return results

    @staticmethod
    def _key(alert):
        code = str(alert.get("code"))
        # One underlying liquidation risk progresses through several thresholds.
        # A downgrade is still an active risk, not a recovery notification.
        if code.startswith("liquidation_"):
            code = "liquidation_buffer"
        return code + ":" + str(alert.get("exchange"))

    @staticmethod
    def _rank(alert):
        rank = Notifier._severity_rank_name(alert.get("severity"))
        return rank + {"liquidation_warning": 1, "liquidation_orange": 2,
                       "liquidation_critical": 3, "liquidation_severe": 4,
                       "liquidation_anomaly": 5}.get(alert.get("code"), 0)

    def _can_recover(self, alert, metrics, snapshots, recovery_checks):
        """Absence is not evidence: require a current, known source metric."""
        code, exchange = alert.get("code"), alert.get("exchange")
        if code in recovery_checks:
            return recovery_checks[code] is True
        risk = metrics.get("risk", {})
        freshness = risk.get("freshness", {}).get(exchange, {})
        position_fresh = freshness.get("position_realtime") is True
        market_fresh = freshness.get("market_realtime") is True
        leg = risk.get("legs", {}).get(exchange, {})
        hedge = metrics.get("hedge", {})
        held_leg = hedge.get("legs", {}).get(exchange, {})
        flat = position_fresh and held_leg.get("side") == "flat" and held_leg.get("raw_quantity") == 0
        spread = metrics.get("spread", {})

        def known(value):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

        if code == "conversion_unconfirmed":
            return risk.get("rules_confirmed") is True
        if code == "fx_unverified":
            return risk.get("fx", {}).get("usable") is True
        if code == "units_unconfirmed":
            mapping = held_leg.get("mapping", {})
            return market_fresh and mapping.get("quantity_ready") is True and mapping.get("price_ready") is True
        if code == "data_stale":
            return market_fresh and position_fresh and freshness.get("status") in {"normal", "warning"}
        if code == "funding_data_stale":
            funding_leg = metrics.get("funding", {}).get("legs", {}).get(exchange, {})
            age = funding_leg.get("funding_age_seconds")
            return market_fresh and known(funding_leg.get("rate")) and known(age) and age <= 15
        if code == "funding_cost":
            return known(metrics.get("funding", {}).get("net_per_8h_usdt"))
        if code == "hedge_mismatch":
            both_flat = hedge.get("status") == "flat" and all(
                risk.get("freshness", {}).get(key, {}).get("position_realtime") is True for key in ("okx", "hl"))
            return known(hedge.get("mismatch_pct")) or both_flat
        if code in {"net_spread_low", "SPREAD_MOVE"}:
            field = "net_spread_pct" if code == "net_spread_low" else "gross_spread_pct"
            return spread.get("realtime") is True and known(spread.get(field))
        if str(code).startswith("liquidation_"):
            return flat or (position_fresh and market_fresh and known(leg.get("liquidation_buffer_pct")))
        if code == "margin_balance_low":
            return position_fresh and known(leg.get("balance_coverage_ratio"))
        if code == "available_balance_negative":
            age = freshness.get("account_age_seconds")
            return known(leg.get("available_balance")) and known(age) and age <= 60
        if code == "leverage_mismatch":
            return flat or (position_fresh and known(leg.get("leverage")) and known(leg.get("target_leverage")))
        if code == "margin_mode_mismatch":
            return flat or (position_fresh and leg.get("margin_mode") is not None and leg.get("target_margin_mode") is not None)
        if code == "direction_wrong":
            return flat or (position_fresh and held_leg.get("side") == {"okx": "short", "hl": "long"}.get(exchange))
        if code == "position_inconsistent":
            raw = held_leg.get("raw_quantity")
            return flat or (position_fresh and held_leg.get("side") in {"long", "short"} and known(raw) and raw != 0)
        if code == "market_mismatch":
            return market_fresh and leg.get("market_valid") is True
        if code in {"entry_depth_insufficient", "close_depth_insufficient"}:
            group = "entry_legs" if code == "entry_depth_insufficient" else "close_legs"
            return spread.get(group, {}).get(exchange, {}).get("complete") is True
        if code == "RULE_REVIEW_REQUIRED":
            return self.store.get("rule_unreviewed") is False
        if code == "POSITION_CHANGE":
            # A historical position-change event does not reverse merely because
            # its display/cooldown window elapsed. It requires an explicit ack.
            return False
        return False

    async def process(self, alerts, cooldown_seconds, metrics=None, snapshots=None, recovery_checks=None):
        previous = self.store.get("alert_states", {})
        current = {}
        now = int(time.time() * 1000)
        metrics, snapshots, recovery_checks = metrics or {}, snapshots or {}, recovery_checks or {}
        grouped = {}
        for alert in alerts:
            key = self._key(alert)
            if key not in grouped or self._rank(alert) >= self._rank(grouped[key]):
                grouped[key] = alert
        for alert in grouped.values():
            key = self._key(alert)
            old = previous.get(key)
            severity = self._rank(alert)
            sent_at = old.get("sent_at", 0) if old else 0
            should_send = not old or severity > old.get("rank", 0) or now - sent_at >= cooldown_seconds * 1000
            state = {"rank": severity, "sent_at": sent_at, "alert": alert}
            if should_send:
                result = await self.send(alert)
                self.store.event("alert", {**alert, "delivery": result})
                # Failed delivery remains due for a bounded 30-second retry, not a full cooldown.
                state["sent_at"] = now if "failed" not in result.values() else now - cooldown_seconds * 1000 + 30000
            current[key] = state
        for key, old in previous.items():
            if key not in current:
                original = old["alert"]
                if not self._can_recover(original, metrics, snapshots, recovery_checks):
                    # Retain unresolved state across outages/restarts. Do not
                    # resend an old condition while its source is unknown.
                    current[key] = old
                    continue
                if now < old.get("recovery_retry_at", 0):
                    current[key] = old
                    continue
                recovery = {**original, "severity": "info", "title": "恢复 · " + original.get("title", "告警"),
                            "message": "此前触发的条件已解除，请结合当前数据新鲜度核对。", "timestamp": now, "value": "recovered"}
                result = await self.send(recovery)
                self.store.event("recovery", {**recovery, "delivery": result})
                if "failed" in result.values():
                    current[key] = {**old, "recovery_retry_at": now + 30000}
        self.store.set("alert_states", current)

    async def close(self):
        await self.client.aclose()
