# 官方 API 核验与适配边界

核验日期：2026-11-06。使用交易所官方文档、Hyperliquid 官方 SDK 源码与真实公开 API 返回。公开 instrument 观察保存于 [metadata-observation.json](metadata-observation.json)，其中没有账户地址、持仓或凭证。

## 实际市场身份

- OKX `ANTHROPIC-USDT-SWAP` 的公开 instruments 返回 `instType=SWAP`、`ctType=linear`、`settleCcy=USDT`、`ctVal=1`、`ctMult=1`、`ctValCcy=ANTHROPIC`、`lotSz=0.001`、`state=live`。这些是核验时的元数据，运行中继续定时读取。
- Hyperliquid `perpDexs` 明确返回 `name=io`、`fullName=EntropyIO`。仅请求 `dex="io"` 的 `metaAndAssetCtxs` 并精确比较 `universe[].name`。核验时名称为 **`io:ANTH`**，没有 `io:ANT`。界面名称可能重映射；适配器不会把 ANT 自动替换成 ANTH，也不会选择 xyz 或 para。实际策略配置的修正需保留审计记录。
- io 元数据返回 `collateralToken=0`，运行时再用 `spotMeta.tokens[index]` 确认其名称为 `USDC`。不把任意 builder 市场都默认当作 USDC 抵押。
- `metadata.verified` 仅表示上述市场身份、数量字段规格已核实。**不表示 IPO 股数或说明书参考“张”与 API 单位已经等价。** `contracts_per_raw_unit`、`price_per_contract_multiplier` 均留 `null`，须由配置审计确认后才参与股数与隐含股价计算；`ctVal=1` 不是 IPO 换股承诺。

## OKX

官方来源：[API v5 文档](https://www.okx.com/docs-v5/en/)；相关章节包括 REST Authentication / Signature、Trading Account / Get positions / Get balance / Get bills details、Public Data / Instruments / Funding rate、Order Book Trading / Market Data。

| 只读端点 | 使用字段与口径 |
| --- | --- |
| `GET /api/v5/public/instruments?instType=SWAP&instId=...` | 精确 instId、ctType、ctVal、ctMult、ctValCcy、lotSz、tickSz、state |
| `GET /api/v5/market/ticker?instId=...` | last、bidPx、askPx、vol24h、ts；SWAP 的 vol24h 单位为合约张数 |
| `GET /api/v5/market/books?instId=...&sz=100` | 100 档买卖深度、ts；衍生品各档 quantity 单位为合约张数，与 positions.pos 一致 |
| `GET /api/v5/public/mark-price`、`GET /api/v5/market/index-tickers` | markPx、idxPx、ts |
| `GET /api/v5/public/open-interest` | oi 单位为合约张数，保留源时间 |
| `GET /api/v5/public/funding-rate` | fundingRate 是 upcoming `fundingTime` 的预测；实际周期以 `nextFundingTime - fundingTime` 计算，不能固定为 8 小时 |
| `GET /api/v5/account/config` | 先检查 perm。仅接受精确 `read_only` 权限；权限不明或含 trade/withdraw 时停止后续账户查询，公开行情继续读取 |
| `GET /api/v5/account/positions` | pos 为原始合约张数；long/short 模式读 posSide；net 模式按官方 signed pos 语义读取。多条非零仓位不擅自净额相抵，标记 unknown |
| `GET /api/v5/account/balance?ccy=USDT` | 仅使用 USDT details 的 eq、availBal、mmr、mgnRatio；不将账户 totalEq 的 USD 误标为 USDT |
| `GET /api/v5/account/max-withdrawal?ccy=USDT` | maxWd，扣除可借资产的最大可提现额 |
| `GET /api/v5/account/bills` | 最近 7 日窗口，begin/end、after billId 分页，最多每秒 5 次，每页 100 条；本地最多 10 页，达到上限即标为未完整 |

鉴权为官方 HMAC-SHA256 + Base64：`timestamp + "GET" + requestPath（含实际 query）+ emptyBody`，ISO UTC 毫秒时间与 Header 一致。仅对允许列表内 GET 做账户读取鉴权。代码中没有下单、转账、提币或更改杠杆接口。

合约持仓 `uTime` 是仓位最后更新时刻，持仓未改变时可能很早。适配器另存 `exchange_updated_at=uTime`，以成功查询接收时间衡量账户数据是否失联；失败时不刷新该时间。`notionalUsd` 保留原值，仅在同时存在官方 `usdPx` 时除以该值换为 USDT。逐仓 margin 和跨仓 imr 分别保留为 position margin；缺失 MMR 不推测。

费用与 PnL 分离：官方 positions 文档说明 `realizedPnl = pnl + fee + fundingFee + liqPenalty + settledPnl`。因此 normalized position.realized_pnl 使用交易分项 `pnl`，不使用 aggregate realizedPnl 参与总和。账单 funding 只取 subType 173/174 的 `pnl`（官方明确 funding expense 可查 pnl），不把 balChg 当成纯资金费；交易 PnL 仅取 type 2 的 pnl；fees_paid = -fee，返佣为负。

全局文档当前写明生产 REST `https://openapi.okx.com`；用户中国账户配置使用已验证可读的 `https://www.okx.com`。官方提示 US/AU 注册账户使用 `https://us.okx.com`，EU 使用 `https://eea.okx.com`。域名仅接受这四个官方来源，不能任意指定 host。US/AU 与 EU 文档页在本次环境无法读取，因此区域 WebSocket 不猜测地址，自动使用 REST；本策略中国域名的公共 WS 为官方 `wss://ws.okx.com/ws/v5/public`。

公共 WS 订阅 tickers、books、mark-price、funding-rate、open-interest、index-tickers。books 采用完整 snapshot + 增量更新，验证 prevSeqId 与 seqId 连续性，数量为 0 时删除档位，失败重新订阅。当前官方已将 books checksum 固定为 0 并废弃，不对 0 做 CRC32 验证；仅兼容校验非零旧版值。20 秒无消息发送官方文本 `ping`，10 秒未恢复即重连。REST 至少每 15 秒重新校验；WS 不可用时每次服务轮询回落 REST。

## Hyperliquid

官方来源：

- [Info endpoint：分页、币名与账户地址](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [Perpetuals：perpDexs、metadata、asset context、账户、funding](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
- [WebSocket subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)
- [WebSocket heartbeat](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/timeouts-and-heartbeats)
- [Funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)
- [Rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)
- [官方 Python SDK info.py](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/info.py)

所有请求使用 `POST https://api.hyperliquid.xyz/info`；此 POST 是只读查询，不调用 `/exchange`，不需签名或钱包私钥。

| type | 使用方式 |
| --- | --- |
| `perpDexs` | 精确确认 builder dex `name=io`；不按显示名子串判断 variant |
| `metaAndAssetCtxs` + `dex=io` | universe 与上下文按索引对应，精确 coin；szDecimals、maxLeverage、marginMode、collateralToken；markPx、oraclePx、funding、openInterest、dayNtlVlm |
| `spotMeta` | collateralToken 对应的确切抵押资产名 |
| `l2Book` + 完整 coin | 原始 base-asset size 和盘口 server time，20 档；不把名义金额除价格替代持仓原始 quantity |
| `recentTrades` + 完整 coin | 实际最近成交价及成交时间；不将 midPx 伪装成 last |
| `clearinghouseState` + `dex=io,user=实际账户地址` | assetPositions 的完整 coin、type=oneWay、signed szi、entryPx、leverage.type/value、liquidationPx、marginUsed、unrealizedPnl、positionValue、marginSummary、withdrawable、server time |
| `userFunding` + user,startTime,endTime | delta.coin 精确筛选目标市场；delta.usdc 是有符号现金流 |
| `userFillsByTime` + user,startTime,endTime | tid 去重，完整 coin；closedPnl 与 fee 分开，feeToken 原币保留，**fee 已包含 builderFee，不能再加一次** |

Hyperliquid account schema 没有独立的 long/short 字段；只在返回的仓位模式为 `oneWay` 时按 signed `szi` 归一化方向，同时保留方向来源说明。没有目标 coin 且账户 API 成功时才明确 flat；未配置地址或失败为 null。地址必须为实际主账户或子账户，不应填 agent wallet；代码不向公开 API 返回或告警泄露完整地址。

`metaAndAssetCtxs` 与 `activeAssetCtx` 没有 server timestamp，因此保留并明确标注 local receive time；盘口和账户保留各自 server time。资金费按照官方每小时结算规则，正费率多头付、空头收；下一整点为规则推导并标注来源，不伪装成 API 返回。`cumFunding` 的生命周期与符号未独立核实，保留原字段供人工检查，不与 userFunding 再相加。

`marginUsed` 为 API 报告的仓位占用保证金；逐仓 MMR 不由最大杠杆推测。`crossMaintenanceMarginUsed` 只代表跨仓 MMR，额外保留字段但不冒充包含逐仓的账户 MMR。withdrawable 不冒充 available balance。官方指出统一账户/组合保证金的全局交易余额应读 spot balances，本版本只显示 io clearinghouse scope 并标注该限制。

公共 WS `wss://api.hyperliquid.xyz/ws` 订阅精确 coin 的 l2Book、activeAssetCtx、trades。20 秒无消息发送 `{"method":"ping"}`，10 秒无响应重连；官方服务器空闲 60 秒会断线。REST 校验间隔 15 秒；断线使用 REST，recentTrades 至少间隔 15 秒。元数据与字典缓存 5 分钟/1 小时，账户 2–5 秒由服务轮询。使用保守的本地 1000 权重/分钟预算，为返回数据附加计重预留额度，预算满时退避；官方共享上限为 1200 权重/分钟。官方 l2Book、clearinghouseState 权重 2，其他常用 info 权重 20，分页历史另有按返回条数计重。HTTP 429 根据 Retry-After 做退避，其它失败按 2、4、8…60 秒退避，保留原时间。

## 账本完整性与验证

两所账本只读取滚动最近 7 日。Hyperliquid 时间范围端点保留 inclusive cursor，去重并最多 10 页；返回达到分页上限时继续请求，不跨过同毫秒边界。官方 fills 最多提供最近 10000 条，无法仅凭空页证明策略自建立以来的账本完整。因此 normalized ledger 的 `complete=false`，lifetime `funding_pnl/realized_pnl/fees_paid=null`；可明确显示已读完整窗口的 `funding_24h/funding_7d`、period totals。分页不完整时该窗口总额也为 null，不把部分和伪装成累计值。

字段为空、无接口、未配置凭证、无法确认单位、无法确认语义时均保留 null；只有成功返回空仓位时数量为 0。发生网络失败保留最后成功值与原始时间，由服务标记过期。raw response 仅放入 `_raw` 供服务端保存，公开接口必须剔除此字段；错误仅含固定说明、HTTP 状态或数字型官方错误代码。

已验证：真实无凭证公共 REST（两所）、两所公开 WS、精确 io:ANTH identity、盘口单位、资金费周期、用户授权公开地址的 HL io 持仓/账本只读查询，以及适配器单元测试。未使用真实 OKX 私有凭证验证账户模式，未完成 24 小时稳定运行；需用户配置后核对实际账户及小额历史成交回报后再确认换算单位。
