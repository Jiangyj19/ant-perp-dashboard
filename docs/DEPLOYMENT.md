# 本次部署记录

日期：2026-11-06（Asia/Shanghai）。

| 项目 | 值 |
|---|---|
| 源码 | `/Users/andy/work/tools/ant-perp-alter` |
| 本机网站 | `http://127.0.0.1:8787` |
| Google Cloud 项目 | `orderhistory-test` |
| 实例 / 区域 | `ant-monitor` / `asia-east2-a` |
| 规格 | e2-small，20 GB pd-balanced，Debian 12 |
| 云端程序 | `/opt/ant-monitor`，Docker Compose |
| 公网网站 | `https://ant-monitor.34-92-108-172.sslip.io`，Caddy 自动 HTTPS |
| 网络 | 独立 `ant-monitor-private`；开放公网 80/443 到 Caddy；SSH 仅允许 IAP 来源；8787 仅 VM 本机 |
| 数据 | `ant-monitor_monitor-data` Docker 卷，SQLite 历史与审计 |
| 备用访问隧道 | `http://127.0.0.1:8788`，需隧道进程运行 |

云端网页登录密码已生成，保存在本机 `.env.cloud` 的 `DASHBOARD_PASSWORD`，不写入文档或聊天。云端 `.env` 仅服务器 root 可读；同一份配置不进入镜像、Git 或实例 metadata。

本机 `.env` 和云端 `.env.cloud` 是两份独立配置。已配置用户提供的 OKX 只读凭证与 HL 公开地址。Bark 尚未配置、默认关闭。后续填写 `.env.cloud` 的 Bark 设备 Key 并设 `BARK_ENABLED=true`；需要重要警告时设 `BARK_CRITICAL=true`，并在 iPhone 中授权。更新云端环境变量：

```bash
GCP_PROJECT=orderhistory-test ./deploy/update-env.sh
```

公网入口不可用时，可临时重新建立网页隧道：

```bash
gcloud compute ssh ant-monitor --project=orderhistory-test --zone=asia-east2-a --tunnel-through-iap -- -N -L 127.0.0.1:8788:127.0.0.1:8787
```

隧道断开不影响公网网站或云端监控采集；云端 Docker 已设置自动重启。此实例持续运行会产生 Google Cloud 实例、磁盘、公网 IPv4 与少量流量等费用，实际以账单为准。

数量映射和最终 IPO 换股规则仍需人工依据官方资料确认，配置不会因为 API 接通就自动标为最终可信。已通过本机 63 项测试、TypeScript 构建和桌面 / 手机页面检查；24 小时连续运行及 Bark 真机投递仍需后续验证。
