# Internal interface
All timestamps are UTC epoch milliseconds. All monetary calculations carry a quote currency; core converts USDC with config.fx.usdc_usdt (only if fx.verified and timestamp fresh). Missing data is null, never zero. Frontend is Chinese.

ExchangeAdapter async `poll() -> dict`, `close()`. modules exchanges.py exports `OKXAdapter(settings: dict)`, `HyperliquidAdapter(settings: dict)`. Calls read-only official endpoints exclusively. settings from env: okx_api_key, okx_api_secret, okx_passphrase, okx_base_url, hl_address, hl_base_url. Also `start()` optional for WS. Never expose settings/secrets/raw private payload in API response. Persist raw JSON server-side via service (adapter may include `_raw` dict removed before public response).

Snapshot shape per exchange, keys same for both:
```
{
  id: 'okx'|'hl', status: 'live'|'unconfigured'|'error'|'stale', errors: [string],
  received_at: ms|null,
  metadata: {instrument, variant: 'io'|null, quantity_unit, contract_value, contract_multiplier,
    contracts_per_raw_unit: number|null, price_per_contract_multiplier: number|null,
    verified: bool, source: officialURL, reason, raw: {}},
  market: {last, mark, oracle, bid, ask, volume_24h, open_interest, funding_rate,
    funding_interval_hours, next_funding_at, timestamp, received_at, quote_currency:'USDT'|'USDC',
    bids: [[price,quantity]], asks: [[price,quantity]], source, transport:'websocket'|'rest'},
  position: null|{side:'long'|'short'|'flat'|'unknown', raw_quantity, quantity_unit,
    entry_price, mark_price, notional, initial_margin, maintenance_margin, margin_mode,
    leverage, unrealized_pnl, realized_pnl, liquidation_price, timestamp, source},
  account: null|{margin_balance, available_balance, withdrawable_balance, maintenance_margin, margin_ratio, timestamp},
  ledger: {funding_pnl:null|number, funding_24h:null|number, funding_7d:null|number,
    fees_paid:null|number, realized_pnl:null|number, complete:false, source,
    funding_records:[{id,timestamp,amount,currency}], fee_records:[{id,timestamp,amount,currency}]},
  _raw: {key:JSON}
}
```
Raw quantities and books use same exchange quantity units. contracts_per_raw_unit and price_per_contract_multiplier MUST remain null unless the exact unit mapping to reference conversion contract is verified from metadata and manually confirmed. Do not assume ctVal is report's contract; keep unknown instead. HL variant comes from perpDexs name 'io', request metaAndAssetCtxs with dex='io', exact coin 'io:ANT'. No variant via string substring guessing. Explicit flat when account API successful no instrument position; null means unavailable.

config YAML keys:
```
strategy: {name, okx_instrument, hyperliquid_coin, hyperliquid_market_variant, target_leverage_okx:4, target_leverage_hl:4}
conversion: {okx_shares_per_contract:1.275, hl_io_shares_per_contract:16.93, source, status:'provisional_not_exchange_confirmed', last_reviewed, confirmed_by:'', okx_contracts_per_raw_unit:null, okx_price_per_contract_multiplier:null, hl_contracts_per_raw_unit:null, hl_price_per_contract_multiplier:null, units_confirmed:false}
fx: {usdc_usdt:null, verified:false, timestamp:null, source:''}
thresholds: {hedge_warning_pct:0.5, hedge_critical_pct:1, funding_warning:50, funding_critical:150, net_spread_min_pct:1, liquidation_warning_pct:30, liquidation_orange_pct:20, liquidation_critical_pct:12, liquidation_severe_pct:8, balance_safety_multiple:2, cooldown_seconds:600}
fees: {okx_taker:0.0005, hl_taker:0.00045}
execution: {target_shares:100}
```
Core module `engine.py` exports `calculate(snapshots: dict, config: dict, now_ms: int) -> dict` returning object {hedge, spread, funding, pnl, risk, alerts}. Exchange dict keys okx, hl. Core validates mapping config.units_confirmed and multiplier >0 before share calculations; share conversion confirmed required for balanced verdict. Calculations can show provisional estimates explicitly if unit mapping verified but conversion provisional. FX missing blocks combined USDT PnL; same-currency funding per leg still shown. `pnl.total_usdt` is strict cumulative strategy PnL and remains null unless both ledgers and all components are complete. `pnl.current_total_usdt` is the dashboard current PnL: strict total when complete, otherwise current unrealized PnL if both legs and FX are usable. Alerts list each {code,severity:'info'|'warning'|'critical',exchange|null,title,message,value,threshold,formula,source,timestamp}. root handles debounce/recovery and persistence. Always exact formulas, timestamps, sources in metrics or alerts. Numeric missing => null.

Root service applies config conversion confirmed multipliers to metadata before calculate, retains last values on failed fetch with original timestamp. It runs adapters and engine every ~3 sec, stores snapshots+history+event audit, exposes /api/snapshot, /api/history?hours=24, /api/events, /api/config and PUT /api/config with confirmation audit (actor,reason), GET /api/status (sanitized configuration flags), POST /api/alerts/test. Bark notifications are configured only from server env; by default they send warning/critical risk alerts and skip info notices. GET /api/snapshot includes {generated_at,exchanges,config,metrics,alerts,events,history}. /api/config excludes secrets. Frontend can poll /api/snapshot every 3 sec and events/history separately if helpful. No mock/default positions. Default empty state should be beautiful and useful.
