"""Read-only official exchange adapters. Credentials and response bodies never enter errors.

Quantity and price mappings to the report's reference contract deliberately stay unknown.
The application must remove ``_raw`` before returning any snapshot to a browser.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import math
import re
import time
import zlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets

OKX_DOC = "https://www.okx.com/docs-v5/en/"
HL_DOC = "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals"
HL_INFO_DOC = "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint"
OKX_BASES = {
    "https://www.okx.com": "wss://ws.okx.com/ws/v5/public",
    "https://openapi.okx.com": "wss://ws.okx.com/ws/v5/public",
    "https://us.okx.com": None,
    "https://eea.okx.com": None,
}
HL_BASES = {"https://api.hyperliquid.xyz": "wss://api.hyperliquid.xyz/ws"}
DAY_MS = 86_400_000


def now_ms() -> int:
    return int(time.time() * 1000)


def number(value: Any) -> float | None:
    """Preserve missing/invalid values rather than silently manufacturing a zero."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def timestamp(value: Any) -> int | None:
    result = number(value)
    return int(result) if result is not None and result > 0 else None


def positive(value: Any) -> float | None:
    result = number(value)
    return result if result is not None and result > 0 else None


def okx_sign(secret: str, timestamp_iso: str, method: str, request_path: str, body: str = "") -> str:
    """Official timestamp + UPPERCASE method + path including query + exact body."""
    message = (timestamp_iso + method.upper() + request_path + body).encode()
    return base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).decode()


def _levels(rows: list, dict_rows: bool = False) -> list[list[float]]:
    if not isinstance(rows, (list, tuple)):
        return []
    out = []
    for row in rows:
        if dict_rows and isinstance(row, dict):
            px, sz = positive(row.get("px")), positive(row.get("sz"))
        elif not dict_rows and isinstance(row, (list, tuple)) and len(row) >= 2:
            px, sz = positive(row[0]), positive(row[1])
        else:
            continue
        if px is not None and sz is not None:
            out.append([px, sz])
    return out


def _first(payload: Any) -> dict:
    rows = payload.get("data") if isinstance(payload, dict) else None
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}


def _empty_ledger(source: str) -> dict:
    return {"funding_pnl": None, "funding_24h": None, "funding_7d": None,
            "fees_paid": None, "realized_pnl": None, "complete": False, "source": source,
            "funding_records": [], "fee_records": [], "period_complete": False,
            "period_start": None, "period_end": None,
            "reason": "尚未读取完整账单；累计策略起点尚未确认。"}


def _blank(exchange: str, instrument: str, currency: str, source: str) -> dict:
    return {
        "id": exchange, "status": "unconfigured", "errors": [], "received_at": None,
        "public_status": "pending", "private_status": "unconfigured",
        "metadata": {"instrument": instrument, "variant": None, "quantity_unit": None,
                     "contract_value": None, "contract_multiplier": None,
                     "contracts_per_raw_unit": None, "price_per_contract_multiplier": None,
                     "verified": False, "source": source, "reason": "等待官方市场元数据。", "raw": {}},
        "market": {**dict.fromkeys(["last", "mark", "oracle", "bid", "ask", "volume_24h",
                    "open_interest", "funding_rate", "funding_interval_hours", "next_funding_at",
                    "timestamp", "received_at"]), "quote_currency": currency, "bids": [], "asks": [],
                   "source": source, "transport": "rest", "field_timestamps": {}},
        "position": None, "account": None, "ledger": _empty_ledger(source), "_raw": {},
    }


class ExchangeError(Exception):
    """Only messages created here, never HTTP bodies or credential-bearing reprs."""


class _Adapter:
    def __init__(self, settings: dict, exchange: str):
        self.settings = dict(settings)
        self.exchange = exchange
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(8, connect=5), follow_redirects=False,
                                        headers={"User-Agent": "ANTReadOnlyMonitor/1.0"})
        self._cache: dict[str, dict] = {}
        self._errors: list[str] = []
        self._ws: dict[str, dict] = {}
        self._ws_task: asyncio.Task | None = None
        self._closed = False
        self.ws_enabled = settings.get("websocket_enabled", settings.get("enable_public_ws", True))
        self._ws_connected = False
        self._ws_error: str | None = None
        self._poll_lock = asyncio.Lock()
        self._weight_lock = asyncio.Lock()
        self._weight_events: list[tuple[int, int]] = []

    async def _json(self, method: str, path: str, *, params: dict | None = None,
                    body: dict | None = None, private: bool = False) -> Any:
        if self.exchange == "okx":
            allowed = {"/api/v5/public/instruments", "/api/v5/market/ticker", "/api/v5/market/books",
                       "/api/v5/public/mark-price", "/api/v5/market/index-tickers",
                       "/api/v5/public/funding-rate", "/api/v5/public/funding-rate-history",
                       "/api/v5/public/open-interest", "/api/v5/account/positions",
                       "/api/v5/account/balance", "/api/v5/account/bills",
                       "/api/v5/account/config", "/api/v5/account/max-withdrawal"}
            if method != "GET" or path not in allowed:
                raise ExchangeError("只读适配器拒绝未允许的端点")
        elif method != "POST" or path != "/info" or not body or body.get("type") not in {
            "perpDexs", "meta", "metaAndAssetCtxs", "l2Book", "recentTrades", "allMids",
            "clearinghouseState", "userFunding", "userFillsByTime", "fundingHistory", "spotMeta",
        }:
            raise ExchangeError("只读适配器拒绝未允许的端点")
        if self.exchange == "hl":
            # Reserve documented per-request weight plus the maximum response surcharge.
            kind = body["type"]
            weight = 2 if kind in {"l2Book", "clearinghouseState", "allMids"} else 20
            weight += {"userFunding": 25, "userFillsByTime": 100, "recentTrades": 5}.get(kind, 0)
            async with self._weight_lock:
                current = now_ms()
                self._weight_events = [(ts, w) for ts, w in self._weight_events if ts > current - 60_000]
                if sum(w for _, w in self._weight_events) + weight > 1000:
                    err = ExchangeError("本地官方 API 权重预算已满，正在退避")
                    err.retry_after = 60
                    raise err
                self._weight_events.append((current, weight))
        request_path = path + ("?" + urlencode(params) if params else "")
        headers = self._private_headers(request_path) if private else {}
        try:
            response = await self.client.request(method, self.base_url + request_path,
                                                 json=body if body is not None else None, headers=headers)
        except httpx.TimeoutException:
            raise ExchangeError("接口请求超时") from None
        except httpx.HTTPError:
            raise ExchangeError("接口网络连接失败") from None
        if response.status_code == 429:
            err = ExchangeError("接口限频（HTTP 429）")
            retry = number(response.headers.get("Retry-After"))
            err.retry_after = min(120, max(1, retry)) if retry is not None else 30
            raise err
        if response.status_code >= 300:
            raise ExchangeError(f"接口 HTTP {response.status_code}")
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise ExchangeError("接口返回非 JSON 数据") from None
        if self.exchange == "okx":
            if not isinstance(payload, dict) or str(payload.get("code")) != "0":
                code = str(payload.get("code", "")) if isinstance(payload, dict) else ""
                safe = code if re.fullmatch(r"[0-9]{1,8}", code) else "unknown"
                raise ExchangeError(f"OKX 接口代码 {safe}（请核对区域、权限与系统时间）")
            if not isinstance(payload.get("data"), list):
                raise ExchangeError("OKX 返回数据结构异常")
        elif not isinstance(payload, (dict, list)):
            raise ExchangeError("Hyperliquid 返回数据结构异常")
        return payload

    async def _fetch(self, name: str, ttl: float, method: str, path: str, **kwargs) -> dict | None:
        """Per-endpoint TTL and bounded exponential backoff; never refresh old timestamps on errors."""
        current = now_ms()
        entry = self._cache.get(name, {})
        if entry.get("retry_at", 0) > current:
            self._errors.append(f"{name}：{entry.get('error', '接口退避中')}")
            return entry if "payload" in entry else None
        if "payload" in entry and current - entry["received_at"] < ttl * 1000:
            return entry
        try:
            payload = await self._json(method, path, **kwargs)
            entry = {"payload": payload, "received_at": now_ms(), "failures": 0}
            self._cache[name] = entry
            return entry
        except ExchangeError as exc:
            failures = entry.get("failures", 0) + 1
            delay = max(min(60, 2 ** min(failures, 6)), getattr(exc, "retry_after", 0))
            entry.update(failures=failures, retry_at=current + delay * 1000, error=str(exc))
            self._cache[name] = entry
            self._errors.append(f"{name}：{exc}")
            return entry if "payload" in entry else None

    def _entry(self, name: str) -> dict | None:
        rest, ws = self._cache.get(name), self._ws.get(name)
        if rest and "payload" not in rest:
            rest = None
        if ws and (not rest or ws["received_at"] > rest["received_at"]):
            return ws
        return rest

    async def start(self) -> None:
        if self.ws_enabled and self.ws_url and not self._closed and (not self._ws_task or self._ws_task.done()):
            self._ws_task = asyncio.create_task(self._ws_loop(), name=f"{self.exchange}-public-ws")

    async def close(self) -> None:
        self._closed = True
        if self._ws_task:
            self._ws_task.cancel()
            await asyncio.gather(self._ws_task, return_exceptions=True)
        await self.client.aclose()

    def _reset_market(self) -> None:
        self._cache.clear()
        self._ws.clear()
        if self._ws_task:
            self._ws_task.cancel()
        self._ws_task = None

    def _finish(self, snap: dict, configured: bool) -> dict:
        current = now_ms()
        market = snap["market"]
        age = current - (market.get("timestamp") or 0)
        live = snap["metadata"].get("verified") and market.get("mark") is not None and age <= 15_000
        snap["received_at"] = market.get("received_at")
        required = {"mark", "book", "instrument"} if self.exchange == "okx" else {"context", "book", "dexs", "spot_meta"}
        failed = any(self._cache.get(k, {}).get("failures", 0) > 0 for k in required)
        if failed:
            live = False
        snap["public_status"] = "live" if live else ("stale" if market.get("mark") is not None else "error")
        snap["status"] = "live" if live and configured else ("unconfigured" if live else snap["public_status"])
        if configured and snap.get("private_status") == "error":
            snap["status"] = "error"
        snap["errors"] = list(dict.fromkeys(self._errors))
        snap["connection"] = {"websocket_connected": self._ws_connected,
                              "websocket_error": self._ws_error,
                              "rest_validation_seconds": 15 if self._ws_connected else 3}
        return snap


class OKXAdapter(_Adapter):
    def __init__(self, settings: dict):
        self.base_url = str(settings.get("okx_base_url") or "https://www.okx.com").rstrip("/")
        if self.base_url not in OKX_BASES:
            raise ValueError("OKX_BASE_URL 必须为允许的 OKX 官方区域域名")
        super().__init__(settings, "okx")
        self.ws_url = OKX_BASES[self.base_url]
        self.instrument = settings.get("okx_instrument") or "ANTHROPIC-USDT-SWAP"
        self.api_key = settings.get("okx_api_key") or ""
        self.api_secret = settings.get("okx_api_secret") or ""
        self.passphrase = settings.get("okx_passphrase") or ""
        self._book_state: dict | None = None
        self._ledger = _empty_ledger(OKX_DOC + "#trading-account-rest-api-get-bills-details-last-7-days")
        self._ledger_at = 0

    def configure(self, config: dict) -> None:
        instrument = config.get("strategy", {}).get("okx_instrument", self.instrument)
        if instrument != self.instrument:
            self.instrument = instrument
            self._reset_market()
            self._ledger = _empty_ledger(OKX_DOC)
            self._ledger_at = 0
            self._book_state = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)

    def _private_headers(self, request_path: str, iso_time: str | None = None) -> dict:
        iso_time = iso_time or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return {"OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": okx_sign(self.api_secret, iso_time, "GET", request_path),
                "OK-ACCESS-TIMESTAMP": iso_time, "OK-ACCESS-PASSPHRASE": self.passphrase}

    async def poll(self) -> dict:
        async with self._poll_lock:
            self._errors = []
            await self.start()
            snap = _blank("okx", self.instrument, "USDT", OKX_DOC)
            # Public quotes remain available even without an account key.
            interval = 15 if self._ws_connected else 2
            requests = [
                self._fetch("instrument", 300, "GET", "/api/v5/public/instruments",
                            params={"instType": "SWAP", "instId": self.instrument}),
                self._fetch("ticker", interval, "GET", "/api/v5/market/ticker", params={"instId": self.instrument}),
                self._fetch("book", interval, "GET", "/api/v5/market/books", params={"instId": self.instrument, "sz": "100"}),
                self._fetch("mark", interval, "GET", "/api/v5/public/mark-price",
                            params={"instType": "SWAP", "instId": self.instrument}),
                self._fetch("index", interval, "GET", "/api/v5/market/index-tickers",
                            params={"instId": self.instrument.removesuffix("-SWAP")}),
                self._fetch("funding", 15, "GET", "/api/v5/public/funding-rate", params={"instId": self.instrument}),
                self._fetch("oi", 15, "GET", "/api/v5/public/open-interest",
                            params={"instType": "SWAP", "instId": self.instrument}),
            ]
            await asyncio.gather(*requests)
            inst = _first((self._cache.get("instrument") or {}).get("payload"))
            valid = inst.get("instId") == self.instrument and inst.get("instType") == "SWAP"
            valid = valid and inst.get("ctType") == "linear" and inst.get("settleCcy") == "USDT"
            meta = snap["metadata"]
            meta.update(quantity_unit="contracts", contract_value=number(inst.get("ctVal")),
                        contract_multiplier=number(inst.get("ctMult")), verified=bool(valid),
                        raw=inst, state=inst.get("state"), contract_value_currency=inst.get("ctValCcy"),
                        lot_size=number(inst.get("lotSz")), tick_size=number(inst.get("tickSz")),
                        reason="官方市场与原始张数单位已确认；报告参考合约的数量/报价映射、IPO股数仍待人工核实。" if valid
                        else "未确认精确 OKX 线性 USDT 永续市场，暂停换算。")
            if not valid:
                self._errors.append("instrument：未找到精确匹配的有效合约规格")
            if valid and inst.get("state") != "live":
                self._errors.append("instrument：交易状态非 live，请人工检查到期或下架规则")
            self._market(snap)
            snap["_raw"].update({k: v["payload"] for k, v in self._cache.items() if "payload" in v})
            if self.configured:
                await self._private(snap)
            elif any([self.api_key, self.api_secret, self.passphrase]):
                self._errors.append("OKX 只读凭证尚未填写完整")
            return self._finish(snap, self.configured)

    def _market(self, snap: dict) -> None:
        market, field_times, transports = snap["market"], {}, []
        mapping = {"ticker": {"last": "last", "bid": "bidPx", "ask": "askPx", "volume_24h": "vol24h"},
                   "mark": {"mark": "markPx"}, "index": {"oracle": "idxPx"},
                   "funding": {"funding_rate": "fundingRate"}, "oi": {"open_interest": "oi"}}
        for name, fields in mapping.items():
            entry = self._entry(name)
            if not entry:
                continue
            data = _first(entry["payload"])
            if name != "index" and data.get("instId") != self.instrument:
                continue
            for dest, src in fields.items():
                market[dest] = number(data.get(src))
                field_times[dest] = timestamp(data.get("ts")) or entry["received_at"]
            transports.append(entry.get("transport", "rest"))
            if name == "funding":
                current, following = timestamp(data.get("fundingTime")), timestamp(data.get("nextFundingTime"))
                market["funding_interval_hours"] = (following - current) / 3_600_000 if current and following and following > current else None
                market["next_funding_at"] = current  # fundingRate applies to the upcoming fundingTime, not nextFundingTime.
                market["funding_timestamp"] = field_times.get("funding_rate")
                market["funding_sign_convention"] = "正费率：多头支付、空头收取；负费率相反。"
        entry = self._entry("book")
        if entry:
            data = _first(entry["payload"])
            market["bids"], market["asks"] = _levels(data.get("bids", [])), _levels(data.get("asks", []))
            market["bid"] = market["bids"][0][0] if market["bids"] else None
            market["ask"] = market["asks"][0][0] if market["asks"] else None
            field_times["book"] = timestamp(data.get("ts")) or entry["received_at"]
            field_times["bid"] = field_times["ask"] = field_times["book"]
            transports.append(entry.get("transport", "rest"))
        critical_times = [field_times[k] for k in ("mark", "book") if field_times.get(k)]
        market["timestamp"] = min(critical_times) if len(critical_times) == 2 else None
        entries = [self._entry(k) for k in ("ticker", "mark", "book")]
        market["received_at"] = max([e["received_at"] for e in entries if e], default=None)
        market["field_timestamps"] = field_times
        market["transport"] = "websocket" if "websocket" in transports else "rest"
        market["volume_unit"] = "contracts"
        market["open_interest_unit"] = "contracts"
        market["source"] = self.base_url + "/api/v5/market/books + /public/mark-price + /public/funding-rate"

    async def _private(self, snap: dict) -> None:
        permission = await self._fetch("account_config", 60, "GET", "/api/v5/account/config", private=True)
        raw_config = _first(permission["payload"]) if permission else {}
        perms = {p.strip() for p in str(raw_config.get("perm") or "").split(",") if p.strip()}
        if not permission or perms != {"read_only"}:
            snap["private_status"] = "error"
            self._errors.append("账户权限：请使用仅 Read 的 OKX API Key；无法确认权限或存在 Trade/Withdraw 时停止账户读取")
            return
        await asyncio.gather(
            self._fetch("positions", 2, "GET", "/api/v5/account/positions", private=True,
                        params={"instType": "SWAP", "instId": self.instrument}),
            self._fetch("balance", 2, "GET", "/api/v5/account/balance", private=True, params={"ccy": "USDT"}),
            self._fetch("withdrawal", 15, "GET", "/api/v5/account/max-withdrawal", private=True, params={"ccy": "USDT"}),
        )
        position_entry, balance_entry = self._cache.get("positions"), self._cache.get("balance")
        if position_entry and "payload" in position_entry:
            rows = [p for p in position_entry["payload"]["data"] if p.get("instId") == self.instrument]
            snap["position"] = self._position(rows, position_entry["received_at"], snap["market"].get("mark"))
        if balance_entry and "payload" in balance_entry:
            balance = _first(balance_entry["payload"])
            detail = next((d for d in balance.get("details", []) if d.get("ccy") == "USDT"), {})
            withdrawal = _first((self._cache.get("withdrawal") or {}).get("payload"))
            snap["account"] = {"margin_balance": number(detail.get("eq")), "available_balance": number(detail.get("availBal")),
                               "withdrawable_balance": number(withdrawal.get("maxWd")),
                               "maintenance_margin": number(detail.get("mmr")), "margin_ratio": number(detail.get("mgnRatio")),
                               "timestamp": balance_entry["received_at"],
                               "exchange_updated_at": timestamp(balance.get("uTime")),
                               "timestamp_basis": "successful_account_snapshot_receive_time",
                               "currency": "USDT", "source": self.base_url + "/api/v5/account/balance",
                               "scope": "USDT币种账户；仓位逐仓保证金见 position；不使用账户USD总值冒充USDT。"}
        if now_ms() - self._ledger_at > 60_000:
            await self._read_ledger()
        snap["ledger"] = copy.deepcopy(self._ledger)
        healthy = all(self._cache.get(k, {}).get("failures", 0) == 0 and now_ms() - self._cache.get(k, {}).get("received_at", 0) <= 15_000 for k in ("positions", "balance"))
        snap["private_status"] = "live" if snap["position"] is not None and snap["account"] is not None and healthy else "error"
        snap["_raw"].update({k: v["payload"] for k, v in self._cache.items() if "payload" in v})

    def _position(self, rows: list, received: int, mark: float | None) -> dict:
        active = [p for p in rows if number(p.get("pos")) not in (0, None)]
        blank = {"side": "flat", "raw_quantity": 0, "quantity_unit": "contracts", "entry_price": None,
                 "mark_price": mark, "notional": 0, "initial_margin": None, "maintenance_margin": None,
                 "margin_mode": None, "leverage": None, "unrealized_pnl": 0, "realized_pnl": None,
                 "liquidation_price": None, "timestamp": received, "source": self.base_url + "/api/v5/account/positions",
                 "currency": "USDT", "timestamp_basis": "account_snapshot_receive_time"}
        if not active:
            # A malformed nonempty record is unavailable, never evidence of a flat account.
            if any(number(p.get("pos")) is None for p in rows):
                blank.update(side="unknown", raw_quantity=None, notional=None, unrealized_pnl=None)
            return blank
        if len(active) != 1:
            self._errors.append("positions：同合约存在多条非零仓位（双向/多保证金模式），须分别人工核对")
            blank.update(side="unknown", raw_quantity=None, notional=None, unrealized_pnl=None,
                         legs=[{"side": p.get("posSide"), "raw_quantity": number(p.get("pos")), "margin_mode": p.get("mgnMode")} for p in active])
            return blank
        p = active[0]
        qty, pos_side = number(p.get("pos")), p.get("posSide")
        side = pos_side if pos_side in {"long", "short"} else (
            "long" if qty > 0 else "short") if pos_side == "net" else "unknown"
        # A negative pos is illegal in long_short_mode; preserve the anomaly.
        if pos_side in {"long", "short"} and qty < 0:
            side = "unknown"
        margin = number(p.get("margin")) if p.get("mgnMode") == "isolated" else number(p.get("imr"))
        notional_coin, usd_px = number(p.get("notionalUsd")), positive(p.get("usdPx"))
        # notionalUsd is USD. Convert only when the API supplies USDT's actual USD price.
        notional = notional_coin / usd_px if notional_coin is not None and usd_px is not None else None
        blank.update(side=side, raw_quantity=qty, entry_price=number(p.get("avgPx")), mark_price=number(p.get("markPx")),
                     notional=notional, initial_margin=margin, maintenance_margin=number(p.get("mmr")),
                     margin_mode=p.get("mgnMode"), leverage=number(p.get("lever")), unrealized_pnl=number(p.get("upl")),
                     realized_pnl=number(p.get("pnl")), liquidation_price=positive(p.get("liqPx")),
                     timestamp=received, exchange_updated_at=timestamp(p.get("uTime")),
                     timestamp_basis="successful_account_snapshot_receive_time",
                     received_at=received, raw_notional_usd=notional_coin,
                     realized_pnl_semantics="pnl: accumulated closing-order trading PnL, excluding fees/funding; current position lifecycle only",
                     funding_pnl_reported=number(p.get("fundingFee")),
                     fees_paid_reported=-number(p["fee"]) if number(p.get("fee")) is not None else None,
                     exchange_realized_pnl_including_components=number(p.get("realizedPnl")),
                     position_margin=margin, position_side_field=pos_side)
        return blank

    async def _read_ledger(self) -> None:
        end, start, records, complete = now_ms(), now_ms() - 7 * DAY_MS, {}, False
        after = None
        try:
            for _ in range(10):
                params = {"instType": "SWAP", "instId": self.instrument, "ccy": "USDT", "limit": "100", "begin": str(start), "end": str(end)}
                if after:
                    params["after"] = after
                payload = await self._json("GET", "/api/v5/account/bills", params=params, private=True)
                rows = payload["data"]
                for row in rows:
                    if row.get("instId") == self.instrument and row.get("ccy") == "USDT" and row.get("billId"):
                        records[str(row["billId"])] = row
                if len(rows) < 100:
                    complete = True
                    break
                cursor = str(rows[-1].get("billId") or "")
                if not cursor or cursor == after:
                    break
                after = cursor
                await asyncio.sleep(0.22)  # Official bills endpoint permits 5 requests/s.
            ledger = self.normalize_bills(list(records.values()), start, end, complete)
            self._ledger, self._ledger_at = ledger, now_ms()
            self._cache["bills"] = {"payload": list(records.values()), "received_at": now_ms()}
        except ExchangeError as exc:
            self._ledger_at = now_ms() - 45_000
            self._errors.append(f"bills：{exc}")

    @staticmethod
    def normalize_bills(rows: list, start: int, end: int, complete: bool) -> dict:
        ledger = _empty_ledger(OKX_DOC + "#trading-account-rest-api-get-bills-details-last-7-days")
        funding, fees, trading = [], [], []
        seen = set()
        for row in rows:
            identifier = str(row.get("billId") or "")
            ts = timestamp(row.get("ts"))
            if not identifier or identifier in seen or ts is None or not start <= ts <= end:
                continue
            seen.add(identifier)
            base = {"id": identifier, "timestamp": ts, "currency": row.get("ccy")}
            if str(row.get("subType")) in {"173", "174"}:
                # Official docs explicitly identify pnl for funding expense; balChg can include unrelated margin changes.
                amount = number(row.get("pnl"))
                if amount is not None:
                    funding.append({**base, "amount": amount})
            elif str(row.get("type")) == "2":
                amount = number(row.get("pnl"))
                if amount is not None:
                    trading.append(amount)
            fee = number(row.get("fee"))
            if fee is not None:
                fees.append({**base, "amount": -fee})  # Positive = fee paid; rebates negative.
        ledger.update(funding_records=funding, fee_records=fees, period_start=start, period_end=end,
                      period_complete=complete, timestamp=end,
                      funding_24h=sum(r["amount"] for r in funding if r["timestamp"] >= end - DAY_MS) if complete else None,
                      funding_7d=sum(r["amount"] for r in funding) if complete else None,
                      period_fees_paid=sum(r["amount"] for r in fees) if complete else None,
                      period_realized_pnl=sum(trading) if complete else None,
                      reason="最近7日分页窗口；累计策略起点与更早历史未确认，complete=false。",
                      semantics="funding=pnl for subType 173/174; trading=pnl of type 2 only; fees=-fee, rebates negative; position.realizedPnl is never added")
        return ledger

    def _apply_book(self, data: dict, action: str) -> dict:
        if action == "snapshot":
            state = {"bids": {}, "asks": {}, "seqId": data.get("seqId")}
        else:
            if self._book_state is None or data.get("prevSeqId") != self._book_state.get("seqId"):
                raise ExchangeError("公开订单簿序列中断，正在重订阅")
            state = copy.deepcopy(self._book_state)
        for side in ("bids", "asks"):
            for row in data.get(side, []):
                if len(row) >= 2:
                    if number(row[1]) == 0:
                        state[side].pop(row[0], None)
                    elif positive(row[1]) is not None and positive(row[0]) is not None:
                        state[side][row[0]] = str(row[1])
        bids = sorted(state["bids"].items(), key=lambda x: Decimal(x[0]), reverse=True)
        asks = sorted(state["asks"].items(), key=lambda x: Decimal(x[0]))
        # Current official books feed deprecates checksum and fixes it to zero.
        # Sequence continuity is authoritative; validate nonzero legacy values only.
        if data.get("checksum") not in (None, 0) and _okx_checksum(bids, asks) != data["checksum"]:
            raise ExchangeError("公开订单簿校验失败，正在重订阅")
        state["seqId"] = data.get("seqId")
        self._book_state = state
        return {"data": [{"instId": self.instrument, "bids": bids, "asks": asks, "ts": data.get("ts"), "seqId": data.get("seqId")} ]}

    async def _ws_loop(self) -> None:
        failures = 0
        while not self._closed:
            try:
                self._book_state = None
                async with websockets.connect(self.ws_url, ping_interval=None, open_timeout=8, close_timeout=3,
                                              max_size=4_000_000) as ws:
                    channels = ["tickers", "books", "mark-price", "funding-rate", "open-interest"]
                    args = [{"channel": c, "instId": self.instrument} for c in channels]
                    args.append({"channel": "index-tickers", "instId": self.instrument.removesuffix("-SWAP")})
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    self._ws_connected, self._ws_error, failures = True, None, 0
                    while not self._closed:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=20)
                        except asyncio.TimeoutError:
                            await ws.send("ping")
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        if raw == "pong":
                            continue
                        message = json.loads(raw)
                        if message.get("event") == "error":
                            raise ExchangeError("公开行情订阅被拒绝，请检查市场与地区")
                        arg, rows = message.get("arg", {}), message.get("data", [])
                        if not rows:
                            continue
                        channels_map = {"tickers": "ticker", "mark-price": "mark", "funding-rate": "funding", "open-interest": "oi", "index-tickers": "index", "books": "book"}
                        name = channels_map.get(arg.get("channel"))
                        if not name or arg.get("instId") not in {self.instrument, self.instrument.removesuffix("-SWAP")}:
                            continue
                        payload = self._apply_book(rows[0], message.get("action", "")) if name == "book" else {"data": rows}
                        self._ws[name] = {"payload": payload, "received_at": now_ms(), "transport": "websocket"}
            except asyncio.CancelledError:
                self._ws_connected = False
                raise
            except Exception as exc:
                self._ws_connected = False
                self._ws_error = str(exc) if isinstance(exc, ExchangeError) else "公开 WebSocket 断线，REST继续校验并自动重连"
                failures += 1
                await asyncio.sleep(min(60, 2 ** min(failures, 6)))


def _okx_checksum(bids: list, asks: list) -> int:
    pieces = []
    for i in range(25):
        for rows in (bids, asks):
            if i < len(rows):
                pieces.extend([str(rows[i][0]), str(rows[i][1])])
    checksum = zlib.crc32(":".join(pieces).encode())
    return checksum if checksum < 2 ** 31 else checksum - 2 ** 32


class HyperliquidAdapter(_Adapter):
    def __init__(self, settings: dict):
        self.base_url = str(settings.get("hl_base_url") or "https://api.hyperliquid.xyz").rstrip("/")
        if self.base_url not in HL_BASES:
            raise ValueError("HL_BASE_URL 必须为允许的 Hyperliquid 官方主网域名")
        super().__init__(settings, "hl")
        self.ws_url = HL_BASES[self.base_url]
        self.address = settings.get("hl_address") or ""
        self.variant = settings.get("hyperliquid_market_variant") or "io"
        self.coin = settings.get("hyperliquid_coin") or "ANT"
        self._market_verified = False
        self._ledger = _empty_ledger(HL_DOC)
        self._ledger_at = 0

    @property
    def full_coin(self) -> str:
        return self.coin if ":" in self.coin else f"{self.variant}:{self.coin}"

    def configure(self, config: dict) -> None:
        strategy = config.get("strategy", {})
        variant = strategy.get("hyperliquid_market_variant", self.variant)
        coin = strategy.get("hyperliquid_coin", self.coin)
        if (variant, coin) != (self.variant, self.coin):
            self.variant, self.coin = variant, coin
            self._market_verified = False
            self._reset_market()
            self._ledger = _empty_ledger(HL_DOC)
            self._ledger_at = 0

    @property
    def configured(self) -> bool:
        return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", self.address))

    async def _info(self, name: str, ttl: float, kind: str, **kwargs) -> dict | None:
        return await self._fetch(name, ttl, "POST", "/info", body={"type": kind, **kwargs})

    async def poll(self) -> dict:
        async with self._poll_lock:
            self._errors = []
            snap = _blank("hl", self.full_coin, "USDC", HL_DOC)
            # Identity is established by exact perpDexs.name and exact universe.name, never symbol similarity.
            await asyncio.gather(self._info("dexs", 300, "perpDexs"), self._info("spot_meta", 3600, "spotMeta"),
                                 self._info("context", 2, "metaAndAssetCtxs", dex=self.variant))
            dexs = (self._cache.get("dexs") or {}).get("payload", [])
            result = (self._cache.get("context") or {}).get("payload", [])
            dex = next((d for d in dexs if isinstance(d, dict) and d.get("name") == self.variant), None) if isinstance(dexs, list) else None
            universe = result[0].get("universe", []) if isinstance(result, list) and len(result) == 2 and isinstance(result[0], dict) else []
            matches = [(i, u) for i, u in enumerate(universe) if u.get("name") == self.full_coin]
            spot_meta = (self._cache.get("spot_meta") or {}).get("payload", {})
            collateral = result[0].get("collateralToken") if universe else None
            token = next((t for t in spot_meta.get("tokens", []) if t.get("index") == collateral), {}) if isinstance(spot_meta, dict) else {}
            valid = bool(self.variant == "io" and dex and self.full_coin.startswith("io:") and len(matches) == 1 and token.get("name") == "USDC")
            instrument = matches[0][1] if matches else {}
            snap["metadata"].update(variant=self.variant if dex else None, verified=valid,
                quantity_unit=f"{self.full_coin} base asset units", raw={"dex": {"name": dex.get("name"), "fullName": dex.get("fullName")} if dex else None,
                "instrument": instrument, "collateralToken": collateral, "collateralCurrency": token.get("name")},
                size_decimals=instrument.get("szDecimals"), max_leverage=instrument.get("maxLeverage"),
                state="delisted" if instrument.get("isDelisted") else "live" if valid else None,
                candidates=[u.get("name") for u in universe],
                reason="官方 io dex、精确 coin 与 USDC 抵押资产已确认；报告参考合约数量/报价映射、IPO股数仍待人工核实。" if valid
                else f"官方 io 元数据中未确认精确市场 {self.full_coin} 或 USDC 抵押资产；不会自动替换别名。")
            self._market_verified = valid
            if not valid:
                self._errors.append(f"market：精确市场 {self.full_coin} 待核实，请核对 metadata.candidates（不会自动匹配其它币名或 dex）")
                snap["_raw"] = {k: v["payload"] for k, v in self._cache.items() if "payload" in v and k in {"dexs", "spot_meta", "context"}}
                return self._finish(snap, self.configured)
            if instrument.get("isDelisted"):
                self._errors.append("market：官方元数据标记 isDelisted，请人工检查")
            await self.start()
            interval = 15 if self._ws_connected else 2
            await asyncio.gather(self._info("book", interval, "l2Book", coin=self.full_coin),
                                 self._info("trades", 15, "recentTrades", coin=self.full_coin))
            self._market(snap, matches[0][0])
            if self.configured:
                await self._private(snap)
            elif self.address:
                self._errors.append("Hyperliquid 地址格式无效，请填写实际主账户或子账户公开地址")
            snap["_raw"].update({k: v["payload"] for k, v in self._cache.items() if "payload" in v})
            return self._finish(snap, self.configured)

    def _market(self, snap: dict, index: int) -> None:
        market, times = snap["market"], {}
        entry = self._entry("context")
        ws_context = self._ws.get("asset_context")
        raw_context = entry["payload"][1][index] if entry and index < len(entry["payload"][1]) else {}
        ctx_received = entry["received_at"] if entry else None
        if ws_context and ws_context["received_at"] > (ctx_received or 0):
            raw_context, ctx_received = ws_context["payload"], ws_context["received_at"]
            market["transport"] = "websocket"
        for target, src in {"mark": "markPx", "oracle": "oraclePx", "funding_rate": "funding", "open_interest": "openInterest", "volume_24h": "dayNtlVlm"}.items():
            market[target] = number(raw_context.get(src))
            times[target] = ctx_received  # This official endpoint provides no server timestamp.
        market["context_timestamp_basis"] = "local_receive_time; official metaAndAssetCtxs/activeAssetCtx has no server timestamp"
        market["funding_interval_hours"] = 1
        market["next_funding_at"] = ((ctx_received // 3_600_000) + 1) * 3_600_000 if ctx_received else None
        market["next_funding_basis"] = "Derived from official hourly settlement schedule, local UTC clock; not a server-returned time"
        market["funding_sign_convention"] = "正费率：多头支付、空头收取；负费率相反。"
        book = self._entry("book")
        if book:
            data = book["payload"]
            levels = data.get("levels", []) if data.get("coin") == self.full_coin else []
            if len(levels) == 2:
                market["bids"], market["asks"] = _levels(levels[0], True), _levels(levels[1], True)
                market["bid"] = market["bids"][0][0] if market["bids"] else None
                market["ask"] = market["asks"][0][0] if market["asks"] else None
                times["book"] = timestamp(data.get("time")) or book["received_at"]
                times["bid"] = times["ask"] = times["book"]
                if book.get("transport") == "websocket":
                    market["transport"] = "websocket"
        trades = self._entry("trades")
        rows = trades["payload"] if trades else []
        rows = [r for r in rows if r.get("coin") == self.full_coin and timestamp(r.get("time"))] if isinstance(rows, list) else []
        if rows:
            last = max(rows, key=lambda r: int(r["time"]))
            market["last"], times["last"] = number(last.get("px")), timestamp(last.get("time"))
        market["timestamp"] = min(ctx_received, times["book"]) if ctx_received and times.get("book") else None
        market["received_at"] = max([t for t in [ctx_received, book["received_at"] if book else None] if t], default=None)
        market["field_timestamps"] = times
        market["volume_unit"] = "USDC"
        market["open_interest_unit"] = f"{self.full_coin} base asset units"
        market["source"] = self.base_url + "/info (dex='io', metaAndAssetCtxs + l2Book + recentTrades)"

    async def _private(self, snap: dict) -> None:
        state = await self._info("account", 2, "clearinghouseState", user=self.address, dex=self.variant)
        if not state or not isinstance(state["payload"], dict) or not isinstance(state["payload"].get("assetPositions"), list):
            snap["private_status"] = "error"
            self._errors.append("account：Hyperliquid 账户快照结构异常")
            return
        payload = state["payload"]
        snap["position"] = self._position(payload, state["received_at"], snap["market"].get("mark"))
        summary = payload.get("marginSummary", {})
        snap["account"] = {"margin_balance": number(summary.get("accountValue")), "available_balance": None,
                           "withdrawable_balance": number(payload.get("withdrawable")), "maintenance_margin": None,
                           "margin_ratio": None, "timestamp": timestamp(payload.get("time")) or state["received_at"],
                           "currency": "USDC", "source": self.base_url + "/info (clearinghouseState, dex='io')",
                           "cross_maintenance_margin": number(payload.get("crossMaintenanceMarginUsed")),
                           "scope": "io dex clearinghouseState；跨仓维持保证金不等于含逐仓的账户维持保证金；统一账户总余额需另查 spot balances。"}
        snap["private_status"] = "live" if now_ms() - state["received_at"] <= 15_000 and state.get("failures", 0) == 0 else "error"
        if now_ms() - self._ledger_at > 60_000:
            await self._read_ledger()
        snap["ledger"] = copy.deepcopy(self._ledger)

    def _position(self, payload: dict, received: int, mark: float | None) -> dict:
        ts = timestamp(payload.get("time")) or received
        rows = [r for r in payload["assetPositions"] if isinstance(r.get("position"), dict) and r["position"].get("coin") == self.full_coin]
        blank = {"side": "flat", "raw_quantity": 0, "quantity_unit": f"{self.full_coin} base asset units", "entry_price": None,
                 "mark_price": mark, "notional": 0, "initial_margin": None, "maintenance_margin": None,
                 "margin_mode": None, "leverage": None, "unrealized_pnl": 0, "realized_pnl": None,
                 "liquidation_price": None, "timestamp": ts, "received_at": received,
                 "source": self.base_url + "/info (clearinghouseState, dex='io')", "currency": "USDC", "timestamp_basis": "account_snapshot_server_time"}
        if not rows:
            return blank
        if len(rows) != 1:
            blank.update(side="unknown", raw_quantity=None, notional=None, unrealized_pnl=None)
            self._errors.append("positions：精确 coin 返回多条仓位，需人工核对")
            return blank
        row, p = rows[0], rows[0]["position"]
        qty = number(p.get("szi"))
        side = ("long" if qty > 0 else "short" if qty < 0 else "flat") if qty is not None and row.get("type") == "oneWay" else "unknown"
        leverage = p.get("leverage", {})
        blank.update(side=side, raw_quantity=qty, entry_price=number(p.get("entryPx")),
                     notional=number(p.get("positionValue")), initial_margin=number(p.get("marginUsed")),
                     margin_mode=leverage.get("type"), leverage=number(leverage.get("value")),
                     unrealized_pnl=number(p.get("unrealizedPnl")), liquidation_price=positive(p.get("liquidationPx")),
                     direction_source="official oneWay position with signed szi; no separate side field exists in clearinghouseState",
                     position_margin=number(p.get("marginUsed")),
                     cumulative_funding_raw=p.get("cumFunding"),
                     cumulative_funding_note="cumFunding 原字段保留；符号及生命周期口径未独立确认，不与 userFunding 账单相加。")
        return blank

    async def _history(self, kind: str, start: int, end: int) -> tuple[list, bool]:
        records, cursor, complete = {}, start, False
        for _ in range(10):
            body = {"type": kind, "user": self.address, "startTime": cursor, "endTime": end}
            if kind == "userFillsByTime":
                body["aggregateByTime"] = False
            rows = await self._json("POST", "/info", body=body)
            if not isinstance(rows, list):
                raise ExchangeError("账户历史数据结构异常")
            old_len = len(records)
            for row in rows:
                delta = row.get("delta", {})
                identifier = f"{row.get('tid') or row.get('hash')}:{delta.get('coin') or row.get('coin')}:{row.get('time')}"
                records[identifier] = row
            if len(rows) < 500:
                complete = True
                break
            times = [timestamp(r.get("time")) for r in rows]
            latest = max([t for t in times if t], default=cursor)
            # Inclusive cursor + dedup avoids dropping multiple events in the boundary block.
            if latest <= cursor and len(records) == old_len:
                break
            if latest < cursor:
                break
            cursor = latest
            await asyncio.sleep(0.1)
        return list(records.values()), complete

    async def _read_ledger(self) -> None:
        end, start = now_ms(), now_ms() - 7 * DAY_MS
        try:
            results = await asyncio.gather(self._history("userFunding", start, end),
                                           self._history("userFillsByTime", start, end), return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    self._errors.append("ledger：" + (str(result) if isinstance(result, ExchangeError) else "历史接口暂不可用"))
            funding, funding_complete = results[0] if not isinstance(results[0], Exception) else ([], False)
            fills, fills_complete = results[1] if not isinstance(results[1], Exception) else ([], False)
            self._ledger = self.normalize_history(funding, fills, self.full_coin, start, end, funding_complete, fills_complete)
            self._ledger_at = now_ms() if funding_complete and fills_complete else now_ms() - 45_000
            self._cache["funding_history"] = {"payload": funding, "received_at": now_ms()}
            self._cache["fills_history"] = {"payload": fills, "received_at": now_ms()}
        except ExchangeError as exc:
            self._ledger_at = now_ms() - 45_000
            self._errors.append(f"ledger：{exc}")

    @staticmethod
    def normalize_history(funding: list, fills: list, coin: str, start: int, end: int,
                          funding_complete: bool, fills_complete: bool) -> dict:
        ledger = _empty_ledger(HL_DOC)
        funding_records, fee_records, trading, seen = [], [], [], set()
        for row in funding:
            delta, ts = row.get("delta", {}), timestamp(row.get("time"))
            identifier = f"{row.get('hash')}:{coin}:{ts}"
            if delta.get("coin") != coin or delta.get("type") != "funding" or ts is None or not start <= ts <= end or identifier in seen:
                continue
            seen.add(identifier)
            amount = number(delta.get("usdc"))
            if amount is not None:
                funding_records.append({"id": identifier, "timestamp": ts, "amount": amount, "currency": "USDC"})
        seen.clear()
        for fill in fills:
            ts, identifier = timestamp(fill.get("time")), str(fill.get("tid") or "")
            if fill.get("coin") != coin or ts is None or not start <= ts <= end or not identifier or identifier in seen:
                continue
            seen.add(identifier)
            fee, pnl = number(fill.get("fee")), number(fill.get("closedPnl"))
            if fee is not None:
                fee_records.append({"id": identifier, "timestamp": ts, "amount": fee, "currency": str(fill.get("feeToken") or "").strip() or None})
            if pnl is not None:
                trading.append(pnl)
        fees_usdc = all(r["currency"] == "USDC" for r in fee_records)
        ledger.update(funding_records=funding_records, fee_records=fee_records,
                      period_start=start, period_end=end, timestamp=end,
                      period_complete=funding_complete and fills_complete,
                      funding_24h=sum(r["amount"] for r in funding_records if r["timestamp"] >= end - DAY_MS) if funding_complete else None,
                      funding_7d=sum(r["amount"] for r in funding_records) if funding_complete else None,
                      period_fees_paid=sum(r["amount"] for r in fee_records) if fills_complete and fees_usdc else None,
                      period_realized_pnl=sum(trading) if fills_complete else None,
                      reason="最近7日窗口；fills接口最多保留最近10000条，策略完整历史与起点未确认；complete=false。",
                      semantics="funding=delta.usdc signed cashflow; trading=closedPnl; fee already includes builderFee (never add it twice); cumFunding is not added")
        return ledger

    async def _ws_loop(self) -> None:
        failures = 0
        while not self._closed and self._market_verified:
            try:
                async with websockets.connect(self.ws_url, ping_interval=None, open_timeout=8, close_timeout=3, max_size=4_000_000) as ws:
                    for sub in [{"type": "l2Book", "coin": self.full_coin}, {"type": "activeAssetCtx", "coin": self.full_coin}, {"type": "trades", "coin": self.full_coin}]:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                    self._ws_connected, self._ws_error, failures = True, None, 0
                    while not self._closed:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=20)
                        except asyncio.TimeoutError:
                            await ws.send(json.dumps({"method": "ping"}))
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        message = json.loads(raw)
                        channel, data = message.get("channel"), message.get("data")
                        if channel == "error":
                            raise ExchangeError("公开行情订阅被拒绝，请检查精确 coin")
                        if channel == "l2Book" and isinstance(data, dict) and data.get("coin") == self.full_coin:
                            self._ws["book"] = {"payload": data, "received_at": now_ms(), "transport": "websocket"}
                        elif channel == "activeAssetCtx" and isinstance(data, dict) and data.get("coin") == self.full_coin and isinstance(data.get("ctx"), dict):
                            self._ws["asset_context"] = {"payload": data["ctx"], "received_at": now_ms(), "transport": "websocket"}
                        elif channel == "trades" and isinstance(data, list) and all(r.get("coin") == self.full_coin for r in data):
                            self._ws["trades"] = {"payload": data, "received_at": now_ms(), "transport": "websocket"}
            except asyncio.CancelledError:
                self._ws_connected = False
                raise
            except Exception as exc:
                self._ws_connected = False
                self._ws_error = str(exc) if isinstance(exc, ExchangeError) else "公开 WebSocket 断线，REST继续校验并自动重连"
                failures += 1
                await asyncio.sleep(min(60, 2 ** min(failures, 6)))
