import math
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
OFFICIAL_OKX = {"https://www.okx.com", "https://openapi.okx.com", "https://us.okx.com", "https://eea.okx.com"}
CONFIG_FIELDS = {
    "strategy": {"name", "okx_instrument", "hyperliquid_coin", "hyperliquid_market_variant", "target_leverage_okx", "target_leverage_hl"},
    "conversion": {"okx_shares_per_contract", "hl_io_shares_per_contract", "source", "status", "last_reviewed", "confirmed_by", "okx_contracts_per_raw_unit", "okx_price_per_contract_multiplier", "hl_contracts_per_raw_unit", "hl_price_per_contract_multiplier", "units_confirmed"},
    "fx": {"usdc_usdt", "verified", "timestamp", "source"},
    "thresholds": {"hedge_warning_pct", "hedge_critical_pct", "funding_warning", "funding_critical", "net_spread_min_pct", "liquidation_warning_pct", "liquidation_orange_pct", "liquidation_critical_pct", "liquidation_severe_pct", "balance_safety_multiple", "cooldown_seconds"},
    "fees": {"okx_taker", "hl_taker"},
    "execution": {"target_shares"},
    "rules": {"ipo_status", "sources"},
}


def normalize_bark_settings(server, device_key):
    server = (server or "https://api.day.app").strip().rstrip("/")
    device_key = (device_key or "").strip()
    if device_key.startswith("https://"):
        parsed = urlparse(device_key)
        parts = [part for part in parsed.path.split("/") if part]
        if not parsed.netloc or not parts:
            raise ValueError("BARK_DEVICE_KEY URL 需要包含 Bark device key")
        server = f"{parsed.scheme}://{parsed.netloc}"
        device_key = parts[0]
    if not server.startswith("https://") or any(c in server for c in "?#@"):
        raise ValueError("BARK_SERVER 必须为 HTTPS 服务源地址")
    if device_key and any(c in device_key for c in "/?#"):
        raise ValueError("BARK_DEVICE_KEY 只填写设备 Key，或填写 https://api.day.app/<key> 形式")
    return server, device_key


def load_settings():
    load_dotenv(ROOT / ".env")
    bark_server, bark_device_key = normalize_bark_settings(os.getenv("BARK_SERVER", "https://api.day.app"), os.getenv("BARK_DEVICE_KEY", ""))
    bark_min_severity = os.getenv("BARK_MIN_SEVERITY", "warning").lower()
    settings = {key.lower(): os.getenv(key, "") for key in (
        "OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE", "HL_ADDRESS", "DASHBOARD_PASSWORD",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    settings.update(
        okx_base_url=os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/"),
        hl_base_url=os.getenv("HL_BASE_URL", "https://api.hyperliquid.xyz").rstrip("/"),
        bark_server=bark_server,
        bark_device_key=bark_device_key,
        bark_enabled=os.getenv("BARK_ENABLED", "false").lower() == "true",
        bark_critical=os.getenv("BARK_CRITICAL", "false").lower() == "true",
        bark_min_severity=bark_min_severity,
        bark_volume=max(0, min(10, int(os.getenv("BARK_VOLUME", "5")))),
        telegram_enabled=os.getenv("TELEGRAM_ENABLED", "false").lower() == "true",
        cookie_secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        enable_public_ws=os.getenv("ENABLE_PUBLIC_WS", "true").lower() == "true",
        poll_seconds=max(2, min(15, float(os.getenv("POLL_SECONDS", "3")))),
        data_dir=str(Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()),
        config_path=str(Path(os.getenv("CONFIG_PATH", str(ROOT / "config/strategy.yaml"))).resolve()),
    )
    if settings["okx_base_url"] not in OFFICIAL_OKX:
        raise ValueError("OKX_BASE_URL 必须为官方 API 域名")
    if settings["hl_base_url"] != "https://api.hyperliquid.xyz":
        raise ValueError("HL_BASE_URL 必须为官方主网 API")
    if settings["hl_address"] and not re.fullmatch(r"0x[0-9a-fA-F]{40}", settings["hl_address"]):
        raise ValueError("HL_ADDRESS 需要 0x 开头的公开地址，禁止输入私钥")
    if settings["dashboard_password"] and len(settings["dashboard_password"]) < 16:
        raise ValueError("DASHBOARD_PASSWORD 至少 16 个字符")
    if settings["bark_min_severity"] not in {"info", "warning", "critical"}:
        raise ValueError("BARK_MIN_SEVERITY 必须为 info、warning 或 critical")
    return settings


def read_config(path):
    with open(path, encoding="utf-8") as file:
        config = yaml.safe_load(file)
    validate_config(config)
    return config


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("配置必须为对象")
    required = {"strategy", "conversion", "fx", "thresholds", "fees", "execution"}
    if not required.issubset(config) or set(config) - required - {"rules"}:
        raise ValueError("配置区块不完整或包含未知区块")
    for key in required | ({"rules"} if "rules" in config else set()):
        if not isinstance(config[key], dict):
            raise ValueError(f"{key} 必须为对象")
        if set(config[key]) - CONFIG_FIELDS[key]:
            raise ValueError(f"{key} 包含未知字段；密钥只能配置在服务器 .env 中")
        if key in {"strategy", "conversion", "thresholds", "fees", "execution"} and CONFIG_FIELDS[key] - set(config[key]):
            raise ValueError(f"{key} 缺少必要字段")
    strategy = config["strategy"]
    if strategy.get("okx_instrument") != "ANTHROPIC-USDT-SWAP" or strategy.get("hyperliquid_market_variant") != "io":
        raise ValueError("此策略必须使用 ANTHROPIC-USDT-SWAP 与 io 市场")
    if strategy.get("hyperliquid_coin") not in {"ANT", "ANTH"}:
        raise ValueError("仅支持精确 ANT 或经官方钱包核实的 ANTH")
    for block in ("thresholds", "fees", "execution"):
        for key, value in config[block].items():
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{block}.{key} 需要有限非负数")
    if config["execution"].get("target_shares", 0) <= 0:
        raise ValueError("目标股数必须大于零")
    for key in ("target_leverage_okx", "target_leverage_hl"):
        value = strategy.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 1 <= value <= 100:
            raise ValueError("目标杠杆需要为 1–100 的数值")
    conv = config["conversion"]
    if conv.get("status") not in {"provisional_not_exchange_confirmed", "exchange_confirmed"}:
        raise ValueError("换算参数状态无效")
    for key in ("okx_shares_per_contract", "hl_io_shares_per_contract", "okx_contracts_per_raw_unit", "hl_contracts_per_raw_unit", "okx_price_per_contract_multiplier", "hl_price_per_contract_multiplier"):
        value = conv.get(key)
        if value is None and "shares_per_contract" not in key:
            continue
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} 必须为正数或未核实 null")
    if conv.get("units_confirmed") is True:
        for key in ("okx_contracts_per_raw_unit", "hl_contracts_per_raw_unit", "okx_price_per_contract_multiplier", "hl_price_per_contract_multiplier"):
            if conv.get(key) is None:
                raise ValueError("确认单位前需要填写全部数量和价格乘数")
    if conv.get("status") == "exchange_confirmed" or conv.get("units_confirmed"):
        if not all(isinstance(conv.get(key), str) and conv[key].strip() for key in ("source", "confirmed_by", "last_reviewed")):
            raise ValueError("确认参数需要来源、复核日期和确认人")
    if not isinstance(conv.get("units_confirmed"), bool):
        raise ValueError("单位确认标记需要为布尔值")
    if conv.get("last_reviewed"):
        from datetime import date
        try:
            date.fromisoformat(conv["last_reviewed"])
        except (ValueError, TypeError):
            raise ValueError("复核日期需要为 YYYY-MM-DD")
    fees = config["fees"]
    if any(value > 0.1 for value in fees.values()):
        raise ValueError("手续费采用小数率，例如 0.0005")
    t = config["thresholds"]
    if not (t["liquidation_severe_pct"] < t["liquidation_critical_pct"] < t["liquidation_orange_pct"] < t["liquidation_warning_pct"] <= 100):
        raise ValueError("强平阈值需保持 severe < critical < orange < warning，critical 用作强平亏损进度叫醒阈值")
    if t["hedge_warning_pct"] >= t["hedge_critical_pct"] or t["funding_warning"] >= t["funding_critical"]:
        raise ValueError("警告阈值必须低于严重阈值")
    sources = config.get("rules", {}).get("sources", [])
    if not isinstance(sources, list) or len(sources) > 8:
        raise ValueError("公告来源最多 8 条")
    from urllib.parse import urlparse
    hosts = {"www.okx.com", "openapi.okx.com", "hyperliquid.gitbook.io", "docs.entropyio.com", "www.sec.gov", "data.sec.gov"}
    for source in sources:
        if not isinstance(source, dict) or set(source) - {"url", "enabled", "name"} or not isinstance(source.get("url"), str) or not isinstance(source.get("enabled", False), bool):
            raise ValueError("公告来源需要 {url, enabled} 对象")
        parsed = urlparse(source.get("url", ""))
        if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("公告来源仅允许受支持的官方 HTTPS 页面")
    return config
