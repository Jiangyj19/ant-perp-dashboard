"""Pure, read-only ANT monitor calculations. No network or trading operations.

``calculate(snapshots, config, now_ms)`` returns JSON-safe ``hedge``, ``spread``,
``funding``, ``pnl``, ``risk`` and ``alerts`` objects. Numbers are floats or null;
unknown data never becomes a zero. Each block carries formulas, sources and a
source timestamp. Additional per-leg provenance lives in ``legs``.

Public metric fields (the UI can render these directly):
* hedge: okx_shares, hl_shares, share_difference, mismatch_pct, balanced
  (bool/null), provisional, realtime, status, reasons, legs.
* spread: okx_share_price, hl_share_price (native quote),
  hl_share_price_usdt, gross_spread_pct, top_of_book_pct, depth_spread_pct,
  net_spread_pct, target_shares, estimated_fees_usdt,
* capitalization: provisional equity-basis convergence view used for dashboard
  layout: HL io per-share price, OKX fully-diluted equivalent, OKX post-IPO
  equivalent, current remaining convergence gap.
  estimated_slippage_usdt, theoretical_profit_usdt (matched held shares),
  theoretical_target_profit_usdt, estimated_net_convergence_usdt,
  close_pnl_usdt, close_fees_usdt, close_slippage_usdt,
  estimated_exit_cost_usdt, close_net_pnl_usdt, entry_legs, close_legs.
* funding: legs, net_per_8h_usdt, cumulative_usdt, past_24h_usdt,
  past_7d_usdt, funding_to_initial_margin_pct. Positive cash flow is income.
* pnl: current_total_usdt, current_total_mode, unrealized_usdt, realized_usdt,
  funding_usdt, fees_usdt, total_usdt, complete, legs, reasons. Realized is
  trading-only ledger PnL; an exchange's realized aggregate is never added
  again to fees/funding.
* risk: status, legs, freshness, fx, rules_confirmed. Risk legs include
  liquidation_buffer_pct, margin_balance, available_balance,
  maintenance_margin, balance_coverage_ratio, leverage, margin_mode, status.
* alerts: code, severity (info/warning/critical), exchange, title, message,
  value, threshold, formula, source, timestamp. Debounce belongs to service.

Unit mapping: raw quantity * contracts_per_raw_unit = reference contracts;
raw price * price_per_contract_multiplier = price per reference contract.
Thus native quote notional = raw quantity * both multipliers * raw price.
Exchange metadata must be verified AND the operator must confirm the unit
mapping. Report share ratios can yield explicitly provisional estimates, but
never an "已配平" verdict. FX conversion requires verified USDC/USDT <=60s old.
Prices/positions older than 60s or failed API snapshots cannot support realtime
hedge/spread/close metrics. Funding uses documented OKX/Hyperliquid semantics:
positive rate means longs pay shorts, and each leg's actual interval is used.
Theoretical convergence amounts are scenarios, never locked or guaranteed PnL.
"""

from __future__ import annotations

import math
from typing import Any


EXCHANGES = ("okx", "hl")
EXPECTED_SIDES = {"okx": "short", "hl": "long"}
FUNDING_SOURCES = {
    "okx": "https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate",
    "hl": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _positive(value: Any) -> float | None:
    result = _number(value)
    return result if result is not None and result > 0 else None


def _nonnegative(value: Any) -> float | None:
    result = _number(value)
    return result if result is not None and result >= 0 else None


def _obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _age(timestamp: Any, now_ms: int) -> float | None:
    timestamp = _positive(timestamp)
    if timestamp is None or timestamp > now_ms + 5000:
        return None
    return max(0.0, (now_ms - timestamp) / 1000)


def _oldest(*timestamps: Any) -> float | None:
    values = [_number(value) for value in timestamps]
    return min(values) if values and all(value is not None for value in values) else None


def _sum_known(values: list[float | None]) -> float | None:
    return sum(values) if values and all(value is not None for value in values) else None


def _severity(*statuses: str) -> str:
    order = {"normal": 0, "flat": 0, "info": 1, "unknown": 1, "warning": 2,
             "provisional": 2, "stale": 3, "critical": 4}
    return max(statuses, key=lambda item: order.get(item, 1)) if statuses else "unknown"


def _confirmed_rules(conversion: dict) -> bool:
    return (conversion.get("status") in {"confirmed", "exchange_confirmed", "verified"}
            and bool(str(conversion.get("confirmed_by") or "").strip())
            and bool(str(conversion.get("source") or "").strip()))


def _mapping(exchange: str, snapshot: dict, conversion: dict) -> dict:
    meta = _obj(snapshot.get("metadata"))
    share_key = "okx_shares_per_contract" if exchange == "okx" else "hl_io_shares_per_contract"
    shares = _positive(conversion.get(share_key))
    contracts = _positive(meta.get("contracts_per_raw_unit"))
    price = _positive(meta.get("price_per_contract_multiplier"))
    verified = meta.get("verified") is True and conversion.get("units_confirmed") is True
    reasons = []
    if not verified:
        reasons.append("原始数量单位尚未由官方 metadata 与人工共同确认")
    if contracts is None:
        reasons.append("缺少原始数量到参考合约的有效乘数")
    if price is None:
        reasons.append("缺少原始报价到参考合约价格的有效乘数")
    if shares is None:
        reasons.append("每张参考合约对应股数待核实")
    # Configuration changes must not silently disagree with applied metadata.
    prefix = "okx" if exchange == "okx" else "hl"
    for suffix, actual in (("contracts_per_raw_unit", contracts),
                           ("price_per_contract_multiplier", price)):
        configured = conversion.get(f"{prefix}_{suffix}")
        if configured is not None:
            configured = _positive(configured)
            if configured is None or actual is None or not math.isclose(configured, actual, rel_tol=1e-10):
                verified = False
                reasons.append("人工确认乘数与当前 metadata 不一致")
    return {"verified": verified, "contracts_per_raw_unit": contracts,
            "price_per_contract_multiplier": price, "shares_per_contract": shares,
            "quantity_ready": verified and contracts is not None and shares is not None,
            "price_ready": verified and price is not None and shares is not None,
            "source": meta.get("source"), "reasons": reasons}


def _vwap(levels: Any, requested_raw: float | None, descending: bool = False) -> dict:
    """Require full depth. A partial fill never masquerades as executable VWAP."""
    result = {"requested_raw": requested_raw, "filled_raw": None,
              "available_raw": None, "vwap": None, "complete": False}
    if requested_raw is None or requested_raw < 0 or not isinstance(levels, list):
        return result
    if requested_raw == 0:
        result.update(filled_raw=0.0, available_raw=0.0, complete=True)
        return result
    valid = []
    for row in levels:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            return result
        price, quantity = _positive(row[0]), _nonnegative(row[1])
        if price is None or quantity is None:
            return result
        valid.append((price, quantity))
    valid.sort(key=lambda row: row[0], reverse=descending)
    available = sum(quantity for _, quantity in valid)
    needed, cost = requested_raw, 0.0
    for price, quantity in valid:
        filled = min(needed, quantity)
        cost += price * filled
        needed -= filled
        if needed <= max(1e-12, requested_raw * 1e-12):
            needed = 0.0
            break
    result.update(filled_raw=requested_raw - needed, available_raw=available,
                  complete=needed == 0)
    if needed == 0:
        result["vwap"] = cost / requested_raw
    return result


def calculate(snapshots: dict, config: dict, now_ms: int) -> dict:
    snapshots, config = _obj(snapshots), _obj(config)
    strategy = _obj(config.get("strategy"))
    conversion = _obj(config.get("conversion"))
    thresholds = _obj(config.get("thresholds"))
    fees = _obj(config.get("fees"))
    execution = _obj(config.get("execution"))
    fx_config = _obj(config.get("fx"))
    alerts = []

    def threshold(key: str, default: float) -> float:
        value = _nonnegative(thresholds.get(key))
        return default if value is None else value

    def alert(code: str, severity: str, exchange: str | None, title: str,
              message: str, value: Any, limit: Any, formula: str,
              source: Any = None, timestamp: Any = None) -> None:
        alerts.append({"code": code, "severity": severity, "exchange": exchange,
                       "title": title, "message": message, "value": value,
                       "threshold": limit, "formula": formula,
                       "source": source or "configuration / official API snapshot",
                       "timestamp": _number(timestamp)})

    fx_rate = _positive(fx_config.get("usdc_usdt"))
    fx_age = _age(fx_config.get("timestamp"), now_ms)
    fx_ready = fx_config.get("verified") is True and fx_rate is not None and fx_age is not None and fx_age <= 60
    fx = {"rate": fx_rate, "verified": fx_config.get("verified") is True,
          "usable": fx_ready, "age_seconds": fx_age, "timestamp": fx_config.get("timestamp"),
          "source": fx_config.get("source"),
          "formula": "USDT = USDC × verified USDC/USDT rate; quote age ≤ 60 seconds"}

    def usdt(value: float | None, currency: Any) -> float | None:
        if value is None:
            return None
        if currency == "USDT":
            return value
        if currency == "USDC" and fx_ready:
            return value * fx_rate
        return None

    rules_confirmed = _confirmed_rules(conversion)
    if not rules_confirmed:
        alert("conversion_unconfirmed", "warning", None, "股数换算规则待确认",
              "报告换算参数只用于暂定估算，不能据此认定已配平或收益已锁定。", None, "official confirmation",
              "share equivalents = abs(raw quantity) × confirmed unit multiplier × shares per contract",
              conversion.get("source"), conversion.get("last_reviewed"))
    if not fx_ready:
        alert("fx_unverified", "info", None, "USDC/USDT 汇率待核实",
              "跨币种 USDT 汇总暂停，保留各腿原币金额。", fx_rate, "verified and age ≤ 60 seconds",
              fx["formula"], fx.get("source"), fx.get("timestamp"))

    legs, freshness, risk_legs = {}, {}, {}
    for exchange in EXCHANGES:
        snapshot = _obj(snapshots.get(exchange))
        market = _obj(snapshot.get("market"))
        position = _obj(snapshot.get("position"))
        account = _obj(snapshot.get("account"))
        meta = _obj(snapshot.get("metadata"))
        mapping = _mapping(exchange, snapshot, conversion)
        quote = market.get("quote_currency")
        market_age = _age(market.get("timestamp"), now_ms)
        position_age = _age(position.get("timestamp"), now_ms)
        account_age = _age(account.get("timestamp"), now_ms)
        api_ok = snapshot.get("status") not in {"error", "stale", "unconfigured"} and bool(snapshot)
        market_api_ok = snapshot.get("public_status") == "live" if "public_status" in snapshot else api_ok
        private_api_ok = snapshot.get("private_status") == "live" if "private_status" in snapshot else api_ok
        market_ready = market_api_ok and market_age is not None and market_age <= 60
        position_ready = private_api_ok and position_age is not None and position_age <= 60 and bool(position)
        account_ready = private_api_ok and account_age is not None and account_age <= 60 and bool(account)
        age_values = [value for value in (market_age, position_age) if value is not None]
        freshness_status = ("stale" if not market_api_ok or market_age is None or market_age > 15
                            or (position and (not private_api_ok or position_age is None or position_age > 15))
                            else "warning" if max(age_values, default=0) > 5 else "normal")
        freshness[exchange] = {"status": freshness_status, "market_age_seconds": market_age,
                               "position_age_seconds": position_age, "account_age_seconds": account_age,
                               "market_timestamp": market.get("timestamp"),
                               "position_timestamp": position.get("timestamp"),
                               "market_realtime": market_ready, "position_realtime": position_ready,
                               "source": market.get("source"),
                               "formula": "age_seconds = (now_ms − source_timestamp_ms) / 1000; realtime requires age ≤ 60 and healthy API"}
        if freshness_status == "stale":
            age = max(age_values) if age_values else None
            alert("data_stale", "critical" if age is not None and age > 60 else "warning", exchange,
                  "数据过期或接口不可用", "最后数据仅供核对；超过 60 秒或 API 失败时暂停实时计算。",
                  age, 15, freshness[exchange]["formula"], market.get("source"), market.get("timestamp"))

        side = position.get("side")
        raw = _number(position.get("raw_quantity"))
        raw_abs = abs(raw) if raw is not None else None
        # Explicit side is authoritative; quantities never imply a side.
        flat = side == "flat" and raw_abs == 0
        consistent = not ((side == "flat" and raw_abs != 0) or (side in {"long", "short"} and raw_abs == 0))
        expected = side == EXPECTED_SIDES[exchange] and consistent
        if position and side in {"long", "short"} and not expected:
            alert("direction_wrong", "critical", exchange, "持仓方向与策略不符",
                  f"本腿要求 {EXPECTED_SIDES[exchange]}；API 返回 {side}。", side, EXPECTED_SIDES[exchange],
                  "position.side must equal configured strategy side; do not infer side from quantity sign",
                  position.get("source"), position.get("timestamp"))
        if position and not consistent:
            alert("position_inconsistent", "critical", exchange, "仓位方向与数量矛盾",
                  "请核对原始账户字段，暂停本腿计算。", raw, side,
                  "flat requires raw quantity = 0; long/short require abs(raw quantity) > 0",
                  position.get("source"), position.get("timestamp"))

        if exchange == "okx":
            expected_instrument = strategy.get("okx_instrument", "ANTHROPIC-USDT-SWAP")
            market_valid = meta.get("instrument") == expected_instrument
            known_wrong = meta.get("instrument") is not None and meta.get("instrument") != expected_instrument
            expected_variant = None
        else:
            expected_variant = strategy.get("hyperliquid_market_variant", "io")
            expected_coin = strategy.get("hyperliquid_coin", "ANT")
            expected_exact = expected_coin if ":" in str(expected_coin) else f"{expected_variant}:{expected_coin}"
            market_valid = meta.get("variant") == expected_variant and meta.get("instrument") == expected_exact
            known_wrong = ((meta.get("variant") is not None and meta.get("variant") != expected_variant)
                           or (meta.get("instrument") is not None and meta.get("instrument") != expected_exact))
        if not market_valid:
            alert("market_mismatch", "critical" if known_wrong else "info", exchange,
                  "合约市场不符" if known_wrong else "合约市场待确认",
                  "需要官方 metadata 确认目标合约与 io 市场，禁止仅按币名匹配。",
                  {"instrument": meta.get("instrument"), "variant": meta.get("variant")},
                  strategy.get("okx_instrument", "ANTHROPIC-USDT-SWAP") if exchange == "okx" else "io:ANT",
                  "OKX exact instrument; Hyperliquid exact dex variant and qualified coin",
                  meta.get("source"), market.get("timestamp"))
        if not mapping["quantity_ready"] or not mapping["price_ready"]:
            alert("units_unconfirmed", "info", exchange, "合约数量或报价单位待确认",
                  "；".join(mapping["reasons"]), None, "official metadata + manual unit confirmation",
                  "reference contracts = raw quantity × contracts_per_raw_unit; reference price = raw price × price_per_contract_multiplier",
                  mapping.get("source"), position.get("timestamp") or market.get("timestamp"))

        quantities_ready = position_ready and market_ready and market_valid and consistent and mapping["quantity_ready"]
        shares = (raw_abs * mapping["contracts_per_raw_unit"] * mapping["shares_per_contract"]
                  if quantities_ready and raw_abs is not None and (expected or flat) else None)
        price_ready = market_ready and market_valid and mapping["price_ready"]
        share_price = (_positive(market.get("mark")) * mapping["price_per_contract_multiplier"] / mapping["shares_per_contract"]
                       if price_ready and _positive(market.get("mark")) is not None else None)
        leverage = _positive(position.get("leverage"))
        target_leverage = _positive(strategy.get(f"target_leverage_{exchange}"))
        leg_status = "flat" if flat and position_ready else "normal" if position_ready and expected and market_valid else "unknown"
        if not consistent or (position and side in {"long", "short"} and not expected) or known_wrong:
            leg_status = "critical"
        if position_ready and not flat and leverage is not None and target_leverage is not None and not math.isclose(leverage, target_leverage, rel_tol=1e-6):
            leg_status = "critical"
            alert("leverage_mismatch", "critical", exchange, "杠杆与设定不符", "请在交易所人工核对杠杆。",
                  leverage, target_leverage, "actual leverage = configured target leverage", position.get("source"), position.get("timestamp"))
        margin_mode = position.get("margin_mode")
        expected_mode = strategy.get(f"target_margin_mode_{exchange}")
        if position_ready and expected_mode and margin_mode and str(margin_mode).lower() != str(expected_mode).lower():
            leg_status = _severity(leg_status, "warning")
            alert("margin_mode_mismatch", "warning", exchange, "保证金模式与设定不符", "请核对本交易所独立保证金风险。",
                  margin_mode, expected_mode, "actual margin mode = configured target margin mode",
                  position.get("source"), position.get("timestamp"))
        liquidation = _positive(position.get("liquidation_price")) if position_ready else None
        mark = _positive(market.get("mark")) if market_ready else None
        buffer = None
        if position_ready and market_ready and not flat and side in {"long", "short"} and mark is not None and liquidation is not None:
            buffer = ((mark - liquidation) if side == "long" else (liquidation - mark)) / mark * 100
            if buffer <= 0:
                leg_status = "critical"
                alert("liquidation_anomaly", "critical", exchange, "强平价方向异常", "强平价位于当前标记价错误一侧，需人工核对。",
                      buffer, 0, "long: (mark − liquidation) / mark × 100; short: (liquidation − mark) / mark × 100",
                      position.get("source"), _oldest(position.get("timestamp"), market.get("timestamp")))
            else:
                for key, default, severity, code in (
                    ("liquidation_severe_pct", 8, "critical", "liquidation_severe"),
                    ("liquidation_critical_pct", 12, "critical", "liquidation_critical"),
                    ("liquidation_orange_pct", 20, "warning", "liquidation_orange"),
                    ("liquidation_warning_pct", 30, "warning", "liquidation_warning")):
                    limit = threshold(key, default)
                    if buffer < limit:
                        leg_status = _severity(leg_status, severity)
                        alert(code, severity, exchange, "强平缓冲不足", "各交易所独立承担保证金风险，另一腿浮盈不能覆盖本腿强平。",
                              buffer, limit, "long: (mark − liquidation) / mark × 100; short: (liquidation − mark) / mark × 100",
                              position.get("source"), _oldest(position.get("timestamp"), market.get("timestamp")))
                        break
        balance = _number(account.get("margin_balance")) if account_ready else None
        available = _number(account.get("available_balance")) if account_ready else None
        maintenance = _nonnegative(position.get("maintenance_margin")) if position_ready else None
        if maintenance is None and account_ready:
            maintenance = _nonnegative(account.get("maintenance_margin"))
        coverage = balance / maintenance if balance is not None and maintenance is not None and maintenance > 0 else None
        safety = threshold("balance_safety_multiple", 2)
        if coverage is not None and coverage < safety:
            leg_status = _severity(leg_status, "warning")
            alert("margin_balance_low", "warning", exchange, "保证金余额安全倍数不足", "请核对本平台余额和维持保证金要求。",
                  coverage, safety, "margin balance / maintenance margin < configured safety multiple",
                  position.get("source"), _oldest(account.get("timestamp"), position.get("timestamp")))
        if available is not None and available < 0:
            leg_status = "critical"
            alert("available_balance_negative", "critical", exchange, "可用余额为负", "请在本交易所人工检查账户风险。",
                  available, 0, "available balance < 0", position.get("source"), account.get("timestamp"))
        missing_risk_fields = []
        if position_ready and not flat:
            missing_risk_fields = [name for name, value in (
                ("liquidation_buffer_pct", buffer), ("margin_balance", balance),
                ("maintenance_margin", maintenance), ("leverage", leverage),
                ("margin_mode", margin_mode)) if value is None]
            if missing_risk_fields and leg_status == "normal":
                leg_status = "unknown"
        if freshness_status == "stale":
            leg_status = _severity(leg_status, "stale")
        risk_legs[exchange] = {"status": leg_status, "liquidation_buffer_pct": buffer,
                               "liquidation_price": liquidation, "mark_price": mark,
                               "margin_balance": balance, "available_balance": available,
                               "maintenance_margin": maintenance, "balance_coverage_ratio": coverage,
                               "leverage": leverage, "target_leverage": target_leverage,
                               "margin_mode": margin_mode, "target_margin_mode": expected_mode,
                               "market_valid": market_valid, "quote_currency": quote,
                               "missing_fields": missing_risk_fields,
                               "market_timestamp": market.get("timestamp"),
                               "position_timestamp": position.get("timestamp"),
                               "account_timestamp": account.get("timestamp"),
                               "timestamp": _oldest(market.get("timestamp"), position.get("timestamp")),
                               "source": position.get("source"),
                               "formula": {"liquidation_buffer_pct": "long: (mark − liquidation) / mark × 100; short: (liquidation − mark) / mark × 100",
                                           "balance_coverage_ratio": "margin balance / maintenance margin"}}
        legs[exchange] = {"snapshot": snapshot, "market": market, "position": position, "account": account,
                          "mapping": mapping, "quote": quote, "side": side, "flat": flat,
                          "raw_abs": raw_abs, "expected": expected, "market_valid": market_valid,
                          "consistent": consistent,
                          "market_ready": market_ready, "position_ready": position_ready,
                          "account_ready": account_ready, "shares": shares, "share_price": share_price,
                          "status": leg_status, "price_ready": price_ready}

    okx, hl = legs["okx"], legs["hl"]
    common_timestamp = _oldest(okx["market"].get("timestamp"), hl["market"].get("timestamp"))
    sources = {exchange: legs[exchange]["market"].get("source") for exchange in EXCHANGES}
    share_difference = hl["shares"] - okx["shares"] if hl["shares"] is not None and okx["shares"] is not None else None
    max_shares = max(hl["shares"], okx["shares"]) if share_difference is not None else None
    mismatch = abs(share_difference) / max_shares * 100 if max_shares is not None and max_shares > 0 else None
    both_flat = okx["flat"] and hl["flat"] and okx["position_ready"] and hl["position_ready"]
    hedge_realtime = all(leg["market_ready"] and leg["position_ready"] for leg in legs.values())
    hedge_status, balanced = "unknown", None
    if both_flat:
        hedge_status = "flat"
    elif not hedge_realtime:
        hedge_status = "stale" if any(value["status"] == "stale" for value in freshness.values()) else "unknown"
    elif mismatch is not None:
        hedge_status = "balanced" if mismatch <= 0.1 else "warning"
        if mismatch > threshold("hedge_critical_pct", 1):
            hedge_status = "critical"
        if rules_confirmed:
            balanced = mismatch <= 0.1
        elif hedge_status != "critical":
            hedge_status = "provisional"
    if any(leg["status"] == "critical" and not leg["expected"] and not leg["flat"] for leg in legs.values()):
        hedge_status, balanced = "critical", None
    if not all(leg["mapping"]["price_ready"] for leg in legs.values()) and hedge_status not in {"critical", "stale", "flat"}:
        hedge_status, balanced = "unknown", None
    hedge_reasons = [reason for leg in legs.values() for reason in leg["mapping"]["reasons"]]
    if not rules_confirmed:
        hedge_reasons.append("股票换算参数尚未获官方确认，配平结论暂停")
    if mismatch is not None and mismatch > threshold("hedge_warning_pct", 0.5):
        severity = "critical" if mismatch > threshold("hedge_critical_pct", 1) else "warning"
        alert("hedge_mismatch", severity, None, "股数对冲偏差", "请人工核对两腿实际数量、单位和换算规则。",
              mismatch, threshold("hedge_critical_pct", 1) if severity == "critical" else threshold("hedge_warning_pct", 0.5),
              "abs(HL shares − OKX shares) / max(HL shares, OKX shares) × 100", conversion.get("source"),
              _oldest(okx["position"].get("timestamp"), hl["position"].get("timestamp")))
    hedge = {"okx_shares": okx["shares"], "hl_shares": hl["shares"],
             "share_difference": share_difference, "mismatch_pct": mismatch,
             "balanced": balanced, "status": hedge_status, "provisional": not rules_confirmed,
             "realtime": hedge_realtime and share_difference is not None, "reasons": hedge_reasons,
             "timestamp": _oldest(common_timestamp, okx["position"].get("timestamp"), hl["position"].get("timestamp")),
             "source": {**sources, "conversion": conversion.get("source")},
             "formula": {"shares": "abs(raw quantity) × contracts_per_raw_unit × shares_per_contract",
                         "share_difference": "HL long shares − OKX short shares",
                         "mismatch_pct": "abs(share_difference) / max(HL shares, OKX shares) × 100"},
             "legs": {exchange: {"shares": leg["shares"], "raw_quantity": _number(leg["position"].get("raw_quantity")),
                                  "side": leg["side"], "mapping": leg["mapping"],
                                  "timestamp": leg["position"].get("timestamp"), "source": leg["position"].get("source")}
                      for exchange, leg in legs.items()}}

    # New entry uses OKX bids (sell) and HL asks (buy), at the requested share size.
    target_shares = _positive(execution.get("target_shares"))
    entry_legs, close_legs = {}, {}
    for exchange, leg in legs.items():
        mapping, market, position = leg["mapping"], leg["market"], leg["position"]
        share_factor = mapping["contracts_per_raw_unit"] * mapping["shares_per_contract"] if mapping["quantity_ready"] else None
        value_factor = mapping["contracts_per_raw_unit"] * mapping["price_per_contract_multiplier"] if mapping["quantity_ready"] and mapping["price_ready"] else None
        ready = leg["price_ready"] and mapping["quantity_ready"] and target_shares is not None
        target_raw = target_shares / share_factor if ready else None
        fee_rate = _nonnegative(fees.get(f"{exchange}_taker"))
        entry_side = "bids" if exchange == "okx" else "asks"
        book = _vwap(market.get(entry_side), target_raw, descending=entry_side == "bids")
        top = _positive(market.get("bid" if exchange == "okx" else "ask")) if leg["price_ready"] else None
        entry_price = book["vwap"]
        entry_share_price = entry_price * mapping["price_per_contract_multiplier"] / mapping["shares_per_contract"] if entry_price is not None else None
        fee = target_raw * value_factor * entry_price * fee_rate if all(value is not None for value in (target_raw, value_factor, entry_price, fee_rate)) else None
        slippage = (target_raw * value_factor * ((top - entry_price) if exchange == "okx" else (entry_price - top))
                    if all(value is not None for value in (target_raw, value_factor, entry_price, top)) else None)
        entry_legs[exchange] = {**book, "side": "sell" if exchange == "okx" else "buy",
                                "share_price": entry_share_price,
                                "top_share_price": top * mapping["price_per_contract_multiplier"] / mapping["shares_per_contract"] if top is not None else None,
                                "fees": fee, "fees_usdt": usdt(fee, leg["quote"]),
                                "slippage": slippage, "slippage_usdt": usdt(slippage, leg["quote"]),
                                "fee_rate": fee_rate, "quote_currency": leg["quote"],
                                "source": market.get("source"), "timestamp": market.get("timestamp")}
        close_ready = leg["market_ready"] and leg["position_ready"] and leg["market_valid"] and leg["consistent"] and value_factor is not None and leg["side"] in {"long", "short", "flat"} and leg["raw_abs"] is not None
        close_book = _vwap(market.get("bids" if leg["side"] == "long" else "asks"), leg["raw_abs"] if close_ready else None, descending=leg["side"] == "long")
        close_price, entry = close_book["vwap"], _positive(position.get("entry_price"))
        mark = _positive(market.get("mark")) if close_ready else None
        close_pnl = close_fee = close_slippage = None
        if close_ready and leg["flat"]:
            close_pnl, close_fee, close_slippage = 0.0, 0.0, 0.0
        elif close_ready and close_price is not None:
            sign = 1 if leg["side"] == "long" else -1
            close_pnl = (close_price - entry) * sign * leg["raw_abs"] * value_factor if entry is not None else None
            close_fee = close_price * leg["raw_abs"] * value_factor * fee_rate if fee_rate is not None else None
            close_slippage = (mark - close_price) * sign * leg["raw_abs"] * value_factor if mark is not None else None
        close_legs[exchange] = {**close_book, "side": "sell" if leg["side"] == "long" else "buy" if leg["side"] == "short" else None,
                                "pnl": close_pnl, "pnl_usdt": usdt(close_pnl, leg["quote"]),
                                "fees": close_fee, "fees_usdt": usdt(close_fee, leg["quote"]),
                                "slippage": close_slippage, "slippage_usdt": usdt(close_slippage, leg["quote"]),
                                "quote_currency": leg["quote"], "source": market.get("source"),
                                "timestamp": _oldest(market.get("timestamp"), position.get("timestamp"))}
        if target_shares is not None and ready and not book["complete"]:
            alert("entry_depth_insufficient", "warning", exchange, "目标规模盘口深度不足",
                  "目标规模无法在已读取深度中完整成交，可执行价差保持待核实。", book["available_raw"], target_raw,
                  "sum(order book raw quantity) ≥ target shares / (contracts_per_raw_unit × shares_per_contract)",
                  market.get("source"), market.get("timestamp"))
        if close_ready and not close_book["complete"]:
            alert("close_depth_insufficient", "warning", exchange, "持仓平仓盘口深度不足",
                  "现有持仓无法在已读取深度中完整平仓，平仓损益和成本保持待核实。", close_book["available_raw"], leg["raw_abs"],
                  "sum(opposite-side order book raw quantity) ≥ abs(current raw position quantity)",
                  market.get("source"), market.get("timestamp"))

    def spread_pct(okx_price: float | None, hl_price: float | None) -> float | None:
        return (okx_price / hl_price - 1) * 100 if okx_price is not None and hl_price is not None and hl_price > 0 else None

    okx_share_usdt, hl_share_usdt = usdt(okx["share_price"], okx["quote"]), usdt(hl["share_price"], hl["quote"])
    gross = spread_pct(okx_share_usdt, hl_share_usdt)
    top_spread = spread_pct(usdt(entry_legs["okx"]["top_share_price"], okx["quote"]), usdt(entry_legs["hl"]["top_share_price"], hl["quote"]))
    entry_okx_usdt = usdt(entry_legs["okx"]["share_price"], okx["quote"])
    entry_hl_usdt = usdt(entry_legs["hl"]["share_price"], hl["quote"])
    depth_spread = spread_pct(entry_okx_usdt, entry_hl_usdt)
    entry_fees = _sum_known([entry_legs[exchange]["fees_usdt"] for exchange in EXCHANGES])
    entry_slippage = _sum_known([entry_legs[exchange]["slippage_usdt"] for exchange in EXCHANGES])
    net_spread = ((entry_okx_usdt - entry_hl_usdt - entry_fees / target_shares) / entry_hl_usdt * 100
                  if all(value is not None for value in (entry_okx_usdt, entry_hl_usdt, entry_fees, target_shares)) and entry_hl_usdt > 0 else None)
    matched = min(okx["shares"], hl["shares"]) if share_difference is not None else None
    theoretical = matched * (okx_share_usdt - hl_share_usdt) if all(value is not None for value in (matched, okx_share_usdt, hl_share_usdt)) else None
    theoretical_target = target_shares * (okx_share_usdt - hl_share_usdt) if all(value is not None for value in (target_shares, okx_share_usdt, hl_share_usdt)) else None
    close_pnl = _sum_known([close_legs[exchange]["pnl_usdt"] for exchange in EXCHANGES])
    close_fees = _sum_known([close_legs[exchange]["fees_usdt"] for exchange in EXCHANGES])
    close_slippage = _sum_known([close_legs[exchange]["slippage_usdt"] for exchange in EXCHANGES])
    exit_cost = close_fees + close_slippage if close_fees is not None and close_slippage is not None else None
    close_net = close_pnl - close_fees if close_pnl is not None and close_fees is not None else None

    def mark_usdt(exchange: str) -> float | None:
        leg = legs[exchange]
        value = _positive(leg["market"].get("mark"))
        if not leg["market_ready"] or not leg["market_valid"] or value is None:
            return None
        return usdt(value, leg["quote"])

    hl_fd_factor = _positive(conversion.get("hl_io_shares_per_contract"))
    okx_total_factor = _positive(conversion.get("okx_shares_per_contract"))
    # OKX ANTHROPIC marks are quoted on a one-tenth contract scale versus the
    # full io/HL share divisor in the strategy report. Keep this provisional
    # dashboard view separate from confirmed hedge math, which still requires
    # explicit unit mapping before it can be treated as authoritative.
    okx_fd_factor = _positive(conversion.get("okx_fully_diluted_shares_per_contract"))
    if okx_fd_factor is None and hl_fd_factor is not None and okx_total_factor is not None:
        okx_fd_factor = hl_fd_factor / 10 if hl_fd_factor > okx_total_factor * 5 else hl_fd_factor
    hl_mark_usdt, okx_mark_usdt = mark_usdt("hl"), mark_usdt("okx")
    cap_hl_share = hl_mark_usdt / hl_fd_factor if hl_mark_usdt is not None and hl_fd_factor is not None else None
    cap_fd_share = okx_mark_usdt / okx_fd_factor if okx_mark_usdt is not None and okx_fd_factor is not None else None
    cap_total_share = okx_mark_usdt / okx_total_factor if okx_mark_usdt is not None and okx_total_factor is not None else None
    cap_fd_gap = spread_pct(cap_fd_share, cap_hl_share)
    cap_remaining = spread_pct(cap_total_share, cap_hl_share)
    capitalization = {
        "hl_share_price_usdt": cap_hl_share,
        "fd_share_price_usdt": cap_fd_share,
        "total_share_price_usdt": cap_total_share,
        "fd_discount_pct": cap_fd_gap,
        "remaining_gap_pct": cap_remaining,
        "realtime": cap_remaining is not None,
        "status": "provisional" if cap_remaining is not None and not rules_confirmed else "normal" if cap_remaining is not None else "unknown",
        "provisional": not rules_confirmed or okx_fd_factor is not None,
        "factors": {"hl_fully_diluted": hl_fd_factor, "okx_fully_diluted": okx_fd_factor, "okx_post_ipo_total": okx_total_factor},
        "rows": [{"label": "OKX · io", "hl_share_price_usdt": cap_hl_share, "fd_share_price_usdt": cap_fd_share,
                  "fd_discount_pct": cap_fd_gap, "total_share_price_usdt": cap_total_share,
                  "remaining_gap_pct": cap_remaining}],
        "source": {**sources, "conversion": conversion.get("source")},
        "timestamp": common_timestamp,
        "reasons": ([] if rules_confirmed else ["股本折算沿用报告参数，仍需官方来源确认；该表只用于观察收敛趋势"]),
        "formula": {"hl_share_price_usdt": "HL mark × FX / hl_io_shares_per_contract",
                    "fd_share_price_usdt": "OKX mark / okx_fully_diluted_shares_per_contract (defaults to HL divisor ÷ 10)",
                    "total_share_price_usdt": "OKX mark / okx_shares_per_contract",
                    "remaining_gap_pct": "OKX post-IPO total-share equivalent / HL io per-share price − 1",
                    "fd_discount_pct": "OKX fully-diluted equivalent / HL io per-share price − 1"}
    }
    spread_reasons = []
    if not fx_ready:
        spread_reasons.append("缺少已核实且 60 秒内更新的 USDC/USDT 汇率")
    if any(not leg["price_ready"] for leg in legs.values()):
        spread_reasons.append("行情过期、市场或价格单位待确认")
    if not rules_confirmed:
        spread_reasons.append("换股规则未最终确认；价差与收敛金额为暂定情景估算")
    if net_spread is not None and net_spread < threshold("net_spread_min_pct", 1):
        alert("net_spread_low", "warning", None, "净可执行价差低于阈值", "本指标仅扣当前目标建仓手续费，未来退出费用与 IPO 规则风险未知。",
              net_spread, threshold("net_spread_min_pct", 1),
              "(OKX sell VWAP/share − HL buy VWAP/share − entry fees/target shares) / HL buy VWAP/share × 100",
              sources, common_timestamp)
    spread = {"okx_share_price": okx["share_price"], "hl_share_price": hl["share_price"],
              "hl_share_price_usdt": hl_share_usdt, "gross_spread_pct": gross,
              "top_of_book_pct": top_spread, "depth_spread_pct": depth_spread,
              "net_spread_pct": net_spread, "target_shares": target_shares,
              "estimated_fees_usdt": entry_fees, "estimated_slippage_usdt": entry_slippage,
              "matched_held_shares": matched, "theoretical_profit_usdt": theoretical,
              "theoretical_target_profit_usdt": theoretical_target,
              "estimated_net_convergence_usdt": theoretical - exit_cost if theoretical is not None and exit_cost is not None else None,
              "close_pnl_usdt": close_pnl, "close_fees_usdt": close_fees,
              "close_slippage_usdt": close_slippage, "estimated_exit_cost_usdt": exit_cost,
              "close_net_pnl_usdt": close_net,
              "entry_legs": entry_legs, "close_legs": close_legs,
              "status": "provisional" if gross is not None and not rules_confirmed else "normal" if gross is not None else "unknown",
              "provisional": not rules_confirmed, "realtime": gross is not None,
              "source": sources, "timestamp": common_timestamp, "reasons": spread_reasons,
              "limitations": "理论收敛空间并非已锁定收益。净可执行价差只扣当前建仓费用；未来退出价格、费用与 IPO 换算规则仍有风险。立即平仓 PnL 不包含已实现、历史资金费和已付手续费。",
              "formula": {"share_price": "raw mark × price_per_contract_multiplier / shares_per_contract",
                          "gross_spread_pct": "(OKX implied share price USDT / HL implied share price USDT − 1) × 100",
                          "depth_spread_pct": "(OKX bid VWAP/share USDT / HL ask VWAP/share USDT − 1) × 100",
                          "net_spread_pct": "(OKX bid VWAP/share − HL ask VWAP/share − entry fees/target shares) / HL ask VWAP/share × 100",
                          "estimated_fees_usdt": "sum(raw size × contracts_per_raw_unit × price_per_contract_multiplier × entry VWAP × taker rate × FX)",
                          "estimated_slippage_usdt": "OKX: (best bid − sell VWAP) × quantity value factor; HL: (buy VWAP − best ask) × quantity value factor; convert to USDT",
                          "theoretical_profit_usdt": "min(OKX held shares, HL held shares) × (OKX mark/share USDT − HL mark/share USDT)",
                          "estimated_net_convergence_usdt": "current matched-share theoretical spread amount − current full-position estimated exit fees − current full-position mark-to-exit slippage (scenario only)",
                          "close_pnl_usdt": "sum(side sign × (close VWAP − entry) × abs(raw quantity) × both unit multipliers × FX)",
                          "close_net_pnl_usdt": "close_pnl_usdt − close_fees_usdt; historical funding/fees/realized PnL excluded"}}

    funding_legs, pnl_legs = {}, {}
    for exchange, leg in legs.items():
        market, position, snapshot = leg["market"], leg["position"], leg["snapshot"]
        ledger = _obj(snapshot.get("ledger"))
        notional = _number(position.get("notional")) if leg["position_ready"] else None
        if notional is not None:
            notional = abs(notional)
        funding_timestamp = _obj(market.get("field_timestamps")).get("funding_rate", market.get("funding_timestamp", market.get("timestamp")))
        funding_age = _age(funding_timestamp, now_ms)
        funding_ready = leg["market_ready"] and funding_age is not None and funding_age <= 60
        rate = _number(market.get("funding_rate")) if funding_ready else None
        interval = _positive(market.get("funding_interval_hours")) if funding_ready else None
        if market.get("funding_rate") is not None and (funding_age is None or funding_age > 15):
            alert("funding_data_stale", "warning", exchange, "资金费率数据过期",
                  "盘口刷新不能代替独立资金费率数据刷新；超过 60 秒暂停预测。", funding_age, 15,
                  "funding age = (now_ms − funding field source timestamp) / 1000",
                  market.get("source"), funding_timestamp)
        sign = 1 if leg["side"] == "short" else -1 if leg["side"] == "long" else 0 if leg["flat"] else None
        projected = notional * rate * sign if all(value is not None for value in (notional, rate, interval, sign)) else None
        per8h = projected * 8 / interval if projected is not None else None
        # A period ledger can be displayed, but cannot stand in for lifetime totals.
        cumulative = _number(ledger.get("funding_pnl"))
        past24, past7 = _number(ledger.get("funding_24h")), _number(ledger.get("funding_7d"))
        funding_legs[exchange] = {"rate": rate, "interval_hours": interval,
                                  "notional": notional, "per_interval": projected,
                                  "per_interval_usdt": usdt(projected, leg["quote"]),
                                  "per_8h": per8h, "per_8h_usdt": usdt(per8h, leg["quote"]),
                                  "next_funding_at": market.get("next_funding_at"),
                                  "cumulative": cumulative, "cumulative_usdt": usdt(cumulative, leg["quote"]),
                                  "past_24h": past24, "past_24h_usdt": usdt(past24, leg["quote"]),
                                  "past_7d": past7, "past_7d_usdt": usdt(past7, leg["quote"]),
                                  "quote_currency": leg["quote"], "complete": ledger.get("complete") is True,
                                  "timestamp": _oldest(funding_timestamp, position.get("timestamp")),
                                  "funding_age_seconds": funding_age,
                                  "source": {"market": market.get("source"), "ledger": ledger.get("source"), "sign_convention": FUNDING_SOURCES[exchange]},
                                  "formula": "signed cashflow/interval = abs(position notional) × funding rate × (short:+1, long:−1); per8h = cashflow × 8 / actual interval hours"}
        unrealized = _number(position.get("unrealized_pnl")) if leg["position_ready"] else None
        if leg["flat"] and leg["position_ready"] and unrealized is None:
            unrealized = 0.0
        realized = _number(ledger.get("realized_pnl"))
        paid_fees = _nonnegative(ledger.get("fees_paid"))
        leg_pnl = {"unrealized": unrealized, "unrealized_usdt": usdt(unrealized, leg["quote"]),
                   "realized": realized, "realized_usdt": usdt(realized, leg["quote"]),
                   "funding": cumulative, "funding_usdt": usdt(cumulative, leg["quote"]),
                   "fees": paid_fees, "fees_usdt": usdt(paid_fees, leg["quote"]),
                   "quote_currency": leg["quote"], "complete": ledger.get("complete") is True,
                   "timestamp": position.get("timestamp"), "source": ledger.get("source"),
                   "formula": "trading-only realized + unrealized + separately accounted funding − positive fees paid; exchange aggregate realized PnL is not reused"}
        leg_components = [leg_pnl[key] for key in ("realized_usdt", "unrealized_usdt", "funding_usdt", "fees_usdt")]
        leg_strict_total = (leg_pnl["realized_usdt"] + leg_pnl["unrealized_usdt"] + leg_pnl["funding_usdt"] - leg_pnl["fees_usdt"]
                            if leg_pnl["complete"] and all(value is not None for value in leg_components) else None)
        leg_pnl["total_usdt"] = leg_strict_total
        leg_pnl["current_total_usdt"] = leg_strict_total if leg_strict_total is not None else leg_pnl["unrealized_usdt"]
        leg_pnl["current_total_mode"] = "complete_exchange" if leg_strict_total is not None else "current_unrealized" if leg_pnl["current_total_usdt"] is not None else "unavailable"
        pnl_legs[exchange] = leg_pnl
    funding_net = _sum_known([funding_legs[exchange]["per_8h_usdt"] for exchange in EXCHANGES])
    funding_cumulative = _sum_known([funding_legs[exchange]["cumulative_usdt"] for exchange in EXCHANGES])
    funding24 = _sum_known([funding_legs[exchange]["past_24h_usdt"] for exchange in EXCHANGES])
    funding7 = _sum_known([funding_legs[exchange]["past_7d_usdt"] for exchange in EXCHANGES])
    margins = _sum_known([usdt(_positive(leg["position"].get("initial_margin")), leg["quote"]) if leg["position_ready"] else None for leg in legs.values()])
    funding = {"legs": funding_legs, "net_per_8h_usdt": funding_net,
               "cumulative_usdt": funding_cumulative, "past_24h_usdt": funding24, "past_7d_usdt": funding7,
               "funding_to_initial_margin_pct": funding_cumulative / margins * 100 if funding_cumulative is not None and margins is not None and margins > 0 else None,
               "source": sources, "timestamp": _oldest(common_timestamp, okx["position"].get("timestamp"), hl["position"].get("timestamp")),
               "formula": "net_per_8h_usdt = sum(signed per-interval funding × 8 / actual interval hours × verified quote FX); historical funding is ledger cash flow, not predicted rates"}
    if funding_net is not None and -funding_net > threshold("funding_warning", 50):
        severity = "critical" if -funding_net > threshold("funding_critical", 150) else "warning"
        alert("funding_cost", severity, None, "预计八小时净资金费支出偏高",
              "按两腿各自真实周期折算八小时；预测不代表下一次两腿同时结算。", -funding_net,
              threshold("funding_critical", 150) if severity == "critical" else threshold("funding_warning", 50),
              funding["formula"], {exchange: FUNDING_SOURCES[exchange] for exchange in EXCHANGES}, funding["timestamp"])
    pnl = {f"{key}_usdt": _sum_known([pnl_legs[exchange][f"{key}_usdt"] for exchange in EXCHANGES])
           for key in ("unrealized", "realized", "funding", "fees")}
    pnl_complete = all(leg["complete"] for leg in pnl_legs.values()) and all(value is not None for value in pnl.values())
    pnl_reasons = []
    if not all(leg["complete"] for leg in pnl_legs.values()):
        pnl_reasons.append("完整策略周期的交易损益、资金费和费用账本尚未确认；近期窗口不能代替累计总额")
    if any(value is None for value in pnl.values()):
        pnl_reasons.append("存在缺失、过期的损益分项或未核实汇率，暂停总损益汇总")
    strict_total = pnl["realized_usdt"] + pnl["unrealized_usdt"] + pnl["funding_usdt"] - pnl["fees_usdt"] if pnl_complete else None
    current_total = strict_total if strict_total is not None else pnl["unrealized_usdt"]
    current_mode = "complete_strategy" if strict_total is not None else "current_unrealized" if current_total is not None else "unavailable"
    if current_mode == "current_unrealized":
        pnl_reasons.append("当前整体盈亏使用两腿未实现盈亏合计；严格累计收益等待完整账本确认")
    pnl.update(current_total_usdt=current_total, current_total_mode=current_mode,
               total_usdt=strict_total,
               complete=pnl_complete, legs=pnl_legs, reasons=pnl_reasons,
               source={exchange: pnl_legs[exchange]["source"] for exchange in EXCHANGES},
               timestamp=_oldest(okx["position"].get("timestamp"), hl["position"].get("timestamp")),
               formula="current_total = strict total when complete, otherwise current unrealized PnL; strict total = trading-only realized + unrealized + separately accounted funding − positive fees paid")
    risk_status = _severity(*(leg["status"] for leg in risk_legs.values()),
                            "warning" if not rules_confirmed else "normal",
                            "unknown" if hedge_status == "unknown" or not fx_ready else "normal",
                            "critical" if hedge_status == "critical" else "warning" if mismatch is not None and mismatch > threshold("hedge_warning_pct", 0.5) else "normal")
    if any(item["severity"] == "critical" for item in alerts):
        risk_status = "critical"
    elif risk_status not in {"critical", "stale"} and any(item["severity"] == "warning" for item in alerts):
        risk_status = "warning"
    risk = {"status": risk_status, "legs": risk_legs, "freshness": freshness,
            "fx": fx, "rules_confirmed": rules_confirmed,
            "source": sources, "timestamp": common_timestamp,
            "formula": "Evaluate each exchange independently; no cross-exchange collateral netting"}
    return {"hedge": hedge, "spread": spread, "capitalization": capitalization, "funding": funding, "pnl": pnl, "risk": risk, "alerts": alerts}
