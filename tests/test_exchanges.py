"""Adapter fixtures exercise actual unit, identity, authentication and ledger hazards."""
import asyncio
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from antmonitor.exchanges import (DAY_MS, ExchangeError, HyperliquidAdapter, OKXAdapter,
                                  _okx_checksum, now_ms, number, okx_sign)


def run(coro):
    return asyncio.run(coro)


def hl_response(body, coin="io:ANTH", account=False):
    ts = now_ms()
    kind = body["type"]
    responses = {
        "perpDexs": [None, {"name": "xyz"}, {"name": "io", "fullName": "EntropyIO"}],
        "spotMeta": {"tokens": [{"index": 0, "name": "USDC"}]},
        "metaAndAssetCtxs": [{"universe": [{"name": coin, "szDecimals": 3, "maxLeverage": 15}], "collateralToken": 0},
                             [{"markPx": "1500", "oraclePx": "1499", "funding": "0.00001", "openInterest": "100", "dayNtlVlm": "120000"}]],
        "l2Book": {"coin": coin, "time": ts, "levels": [[{"px": "1499", "sz": "2"}], [{"px": "1501", "sz": "3"}]]},
        "recentTrades": [{"coin": coin, "px": "1500.5", "sz": "1", "time": ts}],
        "clearinghouseState": {"time": ts, "assetPositions": [], "marginSummary": {"accountValue": "1000"}, "withdrawable": "900"},
        "userFunding": [], "userFillsByTime": [],
    }
    return responses[kind]


def test_signature_includes_query_and_uppercase_method():
    ts = "2026-11-06T05:00:00.123Z"
    path = "/api/v5/account/positions?instType=SWAP&instId=ANTHROPIC-USDT-SWAP"
    expected = base64.b64encode(hmac.new(b"fixture-secret", (ts + "GET" + path).encode(), hashlib.sha256).digest()).decode()
    assert okx_sign("fixture-secret", ts, "get", path) == expected
    assert okx_sign("fixture-secret", ts, "get", path.split("?")[0]) != expected


def test_missing_numbers_never_become_zero():
    for value in [None, "", "nan", "inf", "-inf", "invalid", True]:
        assert number(value) is None
    assert number("0") == 0


def test_okx_side_comes_from_pos_side_not_positive_size():
    async def check():
        adapter = OKXAdapter({"enable_public_ws": False})
        received = now_ms()
        p = adapter._position([{"pos": "3.5", "posSide": "short", "uTime": "10000", "markPx": "150", "notionalUsd": "525", "usdPx": "0.999", "pnl": "5", "realizedPnl": "3", "fundingFee": "-1", "fee": "-1", "mgnMode": "isolated", "margin": "130"}], received, 150)
        assert p["side"] == "short"
        assert p["raw_quantity"] == 3.5
        assert p["quantity_unit"] == "contracts"
        assert p["timestamp"] == received  # last position change is not the data freshness clock
        assert p["exchange_updated_at"] == 10000
        assert p["realized_pnl"] == 5  # use pnl, never realizedPnl including fees/funding
        assert p["notional"] == pytest.approx(525 / 0.999)
        assert adapter._position([{"pos": "3", "posSide": "net"}], received, 150)["side"] == "long"
        assert adapter._position([{"pos": "-3", "posSide": "net"}], received, 150)["side"] == "short"
        assert adapter._position([{"pos": "3"}], received, 150)["side"] == "unknown"
        assert adapter._position([{"pos": "-3", "posSide": "short"}], received, 150)["side"] == "unknown"
        assert adapter._position([], received, 150)["side"] == "flat"
        assert adapter._position([{"pos": ""}], received, 150)["side"] == "unknown"
        await adapter.close()
    run(check())


def test_hl_exact_io_identity_rejects_alias_and_other_dex():
    async def check():
        adapter = HyperliquidAdapter({"enable_public_ws": False})
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=hl_response(json.loads(req.content)))))
        snapshot = await adapter.poll()
        assert snapshot["metadata"]["instrument"] == "io:ANT"
        assert snapshot["metadata"]["verified"] is False
        assert snapshot["market"]["mark"] is None
        assert snapshot["metadata"]["candidates"] == ["io:ANTH"]
        adapter.configure({"strategy": {"hyperliquid_coin": "ANTH"}})
        snapshot = await adapter.poll()
        assert snapshot["metadata"]["verified"] is True
        assert snapshot["metadata"]["contracts_per_raw_unit"] is None
        assert snapshot["metadata"]["price_per_contract_multiplier"] is None
        assert snapshot["metadata"]["contract_multiplier"] is None
        assert snapshot["market"]["bids"] == [[1499, 2]]
        assert snapshot["market"]["funding_interval_hours"] == 1
        adapter.configure({"strategy": {"hyperliquid_market_variant": "xyz", "hyperliquid_coin": "ANTH"}})
        snapshot = await adapter.poll()
        assert snapshot["metadata"]["verified"] is False
        assert snapshot["position"] is None
        await adapter.close()
    run(check())


def test_no_credentials_still_get_public_hl_market_without_default_position():
    async def check():
        types = []
        def handler(req):
            body = json.loads(req.content)
            types.append(body["type"])
            return httpx.Response(200, json=hl_response(body))
        adapter = HyperliquidAdapter({"hyperliquid_coin": "ANTH", "enable_public_ws": False})
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        snap = await adapter.poll()
        assert snap["status"] == "unconfigured"
        assert snap["public_status"] == "live"
        assert snap["position"] is None
        assert snap["account"] is None
        assert "clearinghouseState" not in types
        await adapter.close()
    run(check())


def test_hl_oneway_signed_quantity_and_no_cumfunding_double_count():
    async def check():
        adapter = HyperliquidAdapter({"hyperliquid_coin": "ANTH", "enable_public_ws": False})
        ts = now_ms()
        state = {"time": ts, "assetPositions": [{"type": "oneWay", "position": {"coin": "io:ANTH", "szi": "108.508", "leverage": {"type": "isolated", "value": 4}, "marginUsed": "45918.94", "cumFunding": {"allTime": "0.08"}}}]}
        p = adapter._position(state, ts, 1500)
        assert p["side"] == "long"
        assert p["raw_quantity"] == 108.508
        assert p["maintenance_margin"] is None
        assert p["realized_pnl"] is None
        assert p["cumulative_funding_raw"] == {"allTime": "0.08"}
        state["assetPositions"][0]["type"] = "unknown"
        assert adapter._position(state, ts, 1500)["side"] == "unknown"
        assert adapter._position({"time": ts, "assetPositions": []}, ts, 1500)["side"] == "flat"
        await adapter.close()
    run(check())


def test_okx_bills_separate_trading_funding_fees_and_incomplete_lifetime():
    end, start = now_ms(), now_ms() - 7 * DAY_MS
    rows = [{"billId": "1", "ts": str(end - 10), "ccy": "USDT", "subType": "173", "pnl": "-5", "balChg": "95", "fee": "0", "type": "8"},
            {"billId": "2", "ts": str(end - 10), "ccy": "USDT", "subType": "174", "pnl": "2", "fee": "0", "type": "8"},
            {"billId": "3", "ts": str(end - 10), "ccy": "USDT", "subType": "5", "pnl": "10", "fee": "-0.4", "type": "2"}]
    ledger = OKXAdapter.normalize_bills(rows + rows, start, end, True)
    assert ledger["funding_7d"] == -3
    assert ledger["period_realized_pnl"] == 10
    assert ledger["period_fees_paid"] == pytest.approx(0.4)
    assert ledger["complete"] is False
    assert ledger["funding_pnl"] is None
    assert ledger["realized_pnl"] is None
    assert ledger["fees_paid"] is None
    assert OKXAdapter.normalize_bills(rows, start, end, False)["funding_7d"] is None


def test_hl_ledger_exact_coin_builder_fee_included_and_currency_preserved():
    end, start = now_ms(), now_ms() - 7 * DAY_MS
    funding = [{"time": end - 10, "hash": "h1", "delta": {"type": "funding", "coin": "io:ANTH", "usdc": "2.5"}},
               {"time": end - 10, "hash": "h2", "delta": {"type": "funding", "coin": "xyz:ANTH", "usdc": "999"}}]
    fills = [{"time": end - 10, "tid": 1, "coin": "io:ANTH", "closedPnl": "8", "fee": "1.2", "builderFee": "0.2", "feeToken": " USDC"}]
    ledger = HyperliquidAdapter.normalize_history(funding * 2, fills * 2, "io:ANTH", start, end, True, True)
    assert ledger["funding_7d"] == 2.5
    assert ledger["period_fees_paid"] == 1.2
    assert ledger["period_realized_pnl"] == 8
    assert ledger["complete"] is False
    assert ledger["realized_pnl"] is None
    fills[0]["feeToken"] = "OTHER"
    ledger = HyperliquidAdapter.normalize_history([], fills, "io:ANTH", start, end, True, True)
    assert ledger["period_fees_paid"] is None
    assert ledger["fee_records"][0]["currency"] == "OTHER"


def test_okx_incremental_book_validates_sequence_and_checksum():
    async def check():
        adapter = OKXAdapter({"enable_public_ws": False})
        bids, asks = [["100", "2"]], [["101", "3"]]
        payload = {"bids": bids, "asks": asks, "seqId": 1, "prevSeqId": -1, "checksum": _okx_checksum(bids, asks)}
        result = adapter._apply_book(payload, "snapshot")
        assert result["data"][0]["bids"] == [("100", "2")]
        with pytest.raises(ExchangeError, match="序列"):
            adapter._apply_book({"bids": [], "asks": [], "prevSeqId": 0, "seqId": 2}, "update")
        with pytest.raises(ExchangeError, match="校验"):
            adapter._apply_book({"bids": [["100", "0"]], "asks": [], "prevSeqId": 1, "seqId": 2, "checksum": 1}, "update")
        assert adapter._book_state["seqId"] == 1  # failed updates are atomic
        # 2026 official API fixes the deprecated checksum to zero; continuity still required.
        adapter._apply_book({"bids": [], "asks": [], "prevSeqId": 1, "seqId": 2, "checksum": 0}, "update")
        assert adapter._book_state["seqId"] == 2
        await adapter.close()
    run(check())


def test_private_methods_and_unofficial_hosts_are_rejected():
    async def check():
        adapter = OKXAdapter({"enable_public_ws": False})
        with pytest.raises(ExchangeError, match="只读"):
            await adapter._json("POST", "/api/v5/trade/order", body={})
        await adapter.close()
        hl = HyperliquidAdapter({"enable_public_ws": False})
        with pytest.raises(ExchangeError, match="只读"):
            await hl._json("POST", "/exchange", body={"type": "order"})
        await hl.close()
    run(check())
    with pytest.raises(ValueError, match="官方"):
        OKXAdapter({"okx_base_url": "https://attacker.invalid"})


def test_okx_trade_permission_blocks_account_reads_without_exposing_key():
    async def check():
        paths = []
        def handler(req):
            paths.append(req.url.path)
            data = [{"perm": "read_only,trade"}] if req.url.path.endswith("/config") else []
            return httpx.Response(200, json={"code": "0", "data": data})
        adapter = OKXAdapter({"enable_public_ws": False, "okx_api_key": "fixture-key", "okx_api_secret": "fixture-secret", "okx_passphrase": "fixture-pass"})
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        snap = await adapter.poll()
        assert snap["private_status"] == "error"
        assert "/api/v5/account/positions" not in paths
        assert snap["position"] is None
        serialized = json.dumps(snap)
        assert "fixture-key" not in serialized and "fixture-secret" not in serialized and "fixture-pass" not in serialized
        await adapter.close()
    run(check())


def test_network_failure_keeps_original_received_time_and_backoff():
    async def check():
        adapter = HyperliquidAdapter({"enable_public_ws": False})
        await adapter.client.aclose()
        old = now_ms() - 20_000
        adapter._cache["book"] = {"payload": {"coin": "io:ANT"}, "received_at": old}
        def handler(req):
            return httpx.Response(429, headers={"Retry-After": "10"}, text="private information must never be shown")
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        entry = await adapter._info("book", 0, "l2Book", coin="io:ANT")
        assert entry["received_at"] == old
        assert entry["retry_at"] >= now_ms() + 9000
        assert "private information" not in str(adapter._errors)
        await adapter.close()
    run(check())


def test_hl_rate_budget_reserves_response_weight_before_request():
    async def check():
        adapter = HyperliquidAdapter({"enable_public_ws": False})
        adapter._weight_events = [(now_ms(), 999)]
        with pytest.raises(ExchangeError, match="权重预算"):
            await adapter._json("POST", "/info", body={"type": "l2Book", "coin": "io:ANTH"})
        await adapter.close()
    run(check())


def test_hl_history_pagination_keeps_inclusive_timestamp_and_deduplicates():
    async def check():
        adapter = HyperliquidAdapter({"enable_public_ws": False})
        starts = []
        first = [{"time": 1000 + i, "tid": i + 1, "coin": "io:ANTH"} for i in range(500)]
        async def fake(method, path, **kwargs):
            starts.append(kwargs["body"]["startTime"])
            return first if len(starts) == 1 else [first[-1], {"time": 1500, "tid": 501, "coin": "io:ANTH"}]
        adapter._json = fake
        rows, complete = await adapter._history("userFillsByTime", 1000, 2000)
        assert starts == [1000, 1499]
        assert len(rows) == 501 and complete is True
        await adapter.close()
    run(check())
