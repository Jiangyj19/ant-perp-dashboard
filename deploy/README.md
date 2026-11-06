# Google Cloud 部署

建议一台 Debian 12 `e2-small` VM + 20 GB 持久盘。区域可用 `asia-east2` 或用户已有区域。持续运行约为每月十几至数十美元量级，另有外网 IPv4、磁盘和流量费用；以项目当时的 Google Cloud 价格和配额为准。

默认可以只通过 IAP SSH 隧道访问。需要公网 HTTPS 时，在 `.env` 设置 `PUBLIC_DOMAIN` 并把 `COOKIE_SECURE=true`，部署会同时启动 Caddy 反向代理，公网只开放 80/443，应用容器仍只在 Docker 网络内提供服务。

项目必须已开通计费，操作者具备 Compute 创建实例、网络规则、IAP 隧道及 SSH 所需权限。安装脚本会启用 Compute / IAP API，并创建独立网络、只允许 IAP 地址段的 SSH 规则、VM 和磁盘；会产生云端费用。实例不附带服务账号或云 API scope。

准备 `.env`，填好至少 16 字符的 `DASHBOARD_PASSWORD`；公私密配置仅从本机通过 SSH 传入实例，不进入实例 metadata、启动脚本、构建镜像或命令参数。然后运行：

```bash
GCP_PROJECT=你的项目ID GCP_ZONE=asia-east2-a ./deploy/gce.sh
```

可通过 `ANT_ENV_FILE=.env.cloud` 指定独立的云端私密配置。本次实例信息见 `docs/DEPLOYMENT.md`。

代码上传和密钥上传分别进行，不打包本机 data / .venv / node_modules。远程程序在 `/opt/ant-monitor`，账户数据在 Docker 持久卷。

部署后建立隧道：

```bash
gcloud compute ssh ant-monitor --project=你的项目ID --zone=asia-east2-a --tunnel-through-iap -- -N -L 8787:127.0.0.1:8787
```

打开 <http://127.0.0.1:8787>，输入 `.env` 中的网页密码。不要在隧道环境设置 `COOKIE_SECURE=true`，本机 HTTP 不会发送 Secure Cookie；公网 HTTPS 部署再开启。

更新时重新执行脚本会复用已有 VM / 网络与数据卷。首次构建需要数分钟。备份应使用 SQLite backup API 或停服务后复制卷，不直接复制活动 WAL 数据库文件。重建 VM 前先保存数据；删除实例、磁盘和备份为人工操作，脚本不执行。

公网部署示例：

```bash
PUBLIC_DOMAIN=ant-monitor.34-92-108-172.sslip.io
COOKIE_SECURE=true
```

需要在 Google Cloud 防火墙允许目标标签 `ant-monitor` 的 tcp:80、tcp:443。Caddy 会自动申请和续期 HTTPS 证书。

## GitHub Actions 自动部署

仓库包含 `.github/workflows/deploy.yml`。每次推送到 `main` 会先运行后端测试、前端构建和 Compose 配置校验；全部通过后，GitHub Actions 使用 Workload Identity Federation 获取 Google Cloud 短期身份，通过 IAP SSH 上传源码包并在现有 VM 上重建 Docker 容器。

Actions 不保存 OKX、Hyperliquid、Bark 或网页密码。云端私密配置继续保存在 VM 的 `/opt/ant-monitor/.env`，部署时只复制代码并复用该文件。首次配置需要在 Google Cloud 创建 workload identity pool、provider 和 `github-ant-monitor-deployer` 服务账号，并允许仓库 `Jiangyj19/ant-perp-dashboard` 的 `main` 分支 impersonate 该服务账号。若云端 `.env` 包含 `PUBLIC_DOMAIN`，CI/CD 会自动用 Caddy 发布公网 HTTPS。
