# ANT 双交易所套利监控

中文只读监控网站：FastAPI 后台、React 仪表盘、SQLite 持久化、官方行情 WebSocket / REST 校验、Bark 风险告警。代码不包含下单、转账、钱包签名或自动调仓接口。

## 已核实的市场

- OKX：`ANTHROPIC-USDT-SWAP`，USDT 线性永续，原始持仓单位为张；开发时官方元数据为 `ctVal=1`、`ctMult=1`、`ctValCcy=ANTHROPIC`。
- Hyperliquid：官方 `perpDexs` 中的 `io`（EntropyIO），API 精确市场为 **`io:ANTH`**。说明书中的 `ANT` 与当前 API 名称不同；已结合用户提供公开地址的实际 `clearinghouseState` 核实，初始化审计日志会记录此修正。不会匹配 xyz / para 或其他近似名称。
- `1.275` 和 `16.93` 保留为报告暂定参考值，尚未获得交易所最终换股承诺。数量和报价映射初始为未知；不从截图名义价值反推张数，不默认显示已配平。

## 本机启动

需要 Python 3.12+、Node 20.19+ 或 Node 22。

```bash
cd /Users/andy/work/tools/ant-perp-alter
cp .env.example .env  # 已有 .env 时不要覆盖
chmod 600 .env
./start.sh
```

首次运行安装依赖并构建前端，访问 <http://127.0.0.1:8787>。若系统 `python3` 版本较低，可运行 `ANT_PYTHON=/path/to/python3.12 ./start.sh`。已有本地 `.venv` 可直接启动。服务默认仅绑定本机；同一服务同时提供前端与 API。

开发模式：后台启动后，在 `frontend/` 运行 `npm ci && npm run dev`。Vite 将 `/api` 代理到 8787。生产运行不需要 Vite。

## 需要填的私密配置

在本机编辑 `.env`，填写后重启后台；不要在聊天、Git、截图或前端代码中放密钥。

| 变量 | 用途 |
|---|---|
| `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_PASSPHRASE` | OKX **仅 Read** 权限的账户凭证，禁止 Trade / Withdraw；后台会检查 `account/config.perm` |
| `OKX_BASE_URL` | 中国账户默认 `https://www.okx.com`，支持的官方地区域名见 `.env.example` 和源码 allowlist |
| `HL_ADDRESS` | 公开钱包地址，已在本机配置用户提供地址；**不需要私钥、助记词或 agent signing key** |
| `DASHBOARD_PASSWORD` | 远程访问必须设置，至少 16 字符；网页以 HttpOnly / SameSite 会话登录 |
| `BARK_DEVICE_KEY` | Bark App 中的设备 Key；也可直接填 `https://api.day.app/<key>` 形式 |
| `BARK_ENABLED` | 填好 Key 后设为 `true` |
| `BARK_MIN_SEVERITY` | 默认 `warning`，只推送需关注 / 高风险；可设 `critical` 仅推高风险，或 `info` 推全部 |
| `BARK_CRITICAL` | 设为 `true` 时严重风险使用 `critical` 重要警告；须在 iPhone 授权 Bark 重要警告能力，实际投递受 iOS / 网络影响 |
| `COOKIE_SECURE` | HTTPS 反向代理部署时设 `true`；本机 HTTP 和 SSH 隧道用 `false` |

Bark 请求使用 HTTPS `POST /push`，设备 Key 在 JSON 请求体中。通知不含凭证、完整钱包地址或原始账户响应。默认只推送 `warning` / `critical` 风险，不推送普通 `info` 提示；风险升级立即通知，同类风险按冷却时间合并重发。网页“发送 Bark 测试通知”会发送一条真实设备测试。未启用或未填 Key 时不发送。Telegram 是可选备选通道，默认关闭。

## 第一次接通后的核对

1. 在“参数配置”查看两所原始单位、面值和乘数，并用官方规格、实际成交回报核对到**报告参考合约**的数量与价格映射。元数据的市场身份已验证，不等于报告换股单位已验证。
2. 填写四个数量 / 报价乘数及来源、确认人、复核日期后，才能确认“单位映射”。例如仅当原始数量与报告的参考合约一一对应且报价也是每该合约价格时，两个对应乘数才都是 1。不得因为网页看起来接近而确认。
3. 规则未获得官方最终确认时保留“报告暂定”。此时即使可以输出暂定股数和价差，也不会宣称已配平。
4. 核对 OKX 空头、HL io 多头、逐仓 / 全仓及 4x 杠杆；两所各自的保证金和强平风险独立。
5. 核对资金费和盈亏口径。历史接口按平台可用窗口分页读取，过去 24h / 7d 仅在对应窗口完整时输出；策略起点与全周期账本未确认时，累计已实现、累计手续费和总策略 PnL 保持未知，不把最近 7 天假装成策略全周期。
6. 在“告警”页发送 Bark 测试，检查 iPhone 勿扰模式下实际行为。连续运行至少 24 小时，与两所账户界面核对。这一步需要实际凭证和时间，不属于已完成的本地自动化验证。

USDC 金额保留原币，同时后台每 5 秒查询 OKX `USDC-USDT` 买卖中价作为汇率。不存在或超过 60 秒则暂停跨币种汇总，不假定 1 USDC = 1 USDT。

## 数据、计算与告警

- 行情公共 WebSocket 自动重连、指数退避和心跳；REST 定时校验。私有账户默认约 3 秒查询，失败遵守退避。保留交易所更新时间与本机接收时间。
- 页面 3 秒轮询后台。数据新鲜度：5 秒内绿色，5–15 秒黄色，15 秒以上红色；60 秒以上不输出实时配平和价差，接口失败不重新盖时间戳。
- 股数：`abs(原始数量) × 已核实参考合约乘数 × 每参考合约股数`。
- 配平误差：`abs(HL 股数 - OKX 股数) / max(两边股数) × 100%`。
- 可执行价差用目标等价股数，OKX 卖出吃 bid、HL 买入吃 ask，多档深度模拟 VWAP。平仓方向相反；深度不够返回未知，不将部分成交作为全部成交。
- 资金费使用真实周期：OKX 从资金费时间字段计算、HL 按官方每小时结算；正费率多头付、空头收；换算预计 8 小时净流量。未来费率不作确定收益。
- 盈亏按交易盈亏、资金费和费用分项去重。账本范围或已实现口径不完整时不计算总计。
- 同类风险默认 10 分钟冷却，风险升级即时通知；恢复必须有可用的新数据，数据失联不会被误当成风险恢复。
- 规则来源默认未配置；可在参数页填官方 HTTPS 公告 URL，后台 5 分钟抓取一次并在 SQLite 留存原文、URL、哈希和时间。内容变化只发“待人工复核”，动态页面也可能改变哈希，不自动改股数或推断 IPO。IPO 状态由人工依据可靠来源更新；没有配置 SEC 指定申报页时不会声称已自动监控 SEC。
- 参数变化需要填写确认人和理由，旧值 / 新值与配置在 SQLite 同一事务保存。保存后 SQLite 配置优先于初始 YAML。

`data/monitor.sqlite3` 包含真实账户数据，权限 0600；原始响应按端点去重并无损压缩保留 7 天、采样历史保留 30 天、审计日志持久保存。调试可用服务端 `Store.decode_raw` 解码。请保护备份。数据库、`.env` 和依赖目录已加入 `.gitignore`，浏览器不提供原始响应下载接口。

## Docker

先在 `.env` 中设置至少 16 字符的 `DASHBOARD_PASSWORD`。Docker 网络中的访问来源为网桥，使用网页密码认证；未设置时 Compose 会明确拒绝启动。

```bash
docker compose up -d --build
docker compose logs --tail=100 monitor
docker compose ps
```

默认只映射本机 8787；持久卷 `monitor-data` 保存 SQLite。镜像以非 root 用户运行，根文件系统只读，带健康检查、自动重启和日志轮转。`docker compose down` 保留数据；不要使用 `down -v`，它会删除卷。

需要公网网页时用 HTTPS 反向代理，配置强密码及 `COOKIE_SECURE=true`。不要直接开放 8787 到互联网。安全默认拒绝未设置密码的远程请求，不信任代理伪造的客户端 IP。

## Google Cloud

参见 [部署说明](deploy/README.md)。推荐持久 VM + Docker，让后台持续采集并保留 SQLite；默认通过 IAP SSH 隧道访问，不把账户资产网页公开到互联网。Cloud Run 的实例生命周期与默认 CPU 分配不适合此版本的持续后台进程和本地 SQLite。

已配置 `PUBLIC_DOMAIN` 时，会额外启动 Caddy 公网 HTTPS 反向代理；应用仍需要网页密码登录，账户接口仍是只读。

## 验证

```bash
.venv/bin/python -m pip install -r backend/requirements-dev.txt
.venv/bin/python -m pytest -q
cd frontend && npm ci && npm run build
docker compose config --no-interpolate --quiet
```

测试覆盖原始单位 / 市场身份 / 签名 / 方向 / 配平 / FX / 深度不足 / 费率 / 风险阈值 / 过期数据 / 告警去重升级恢复 / 服务持久化与认证。官方接口来源和开发时真实 metadata 观察见 `docs/API_SOURCES.md`、`docs/metadata-observation.json`。

此网站显示理论收敛空间与监控值；IPO、结算规则、换股数和流动性变化均可能改变结果。不会声称利润已锁定。
