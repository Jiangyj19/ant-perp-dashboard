import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .service import Monitor, now_ms
from .settings import ROOT, load_settings, read_config, validate_config
from .storage import Store


def create_app(settings=None, start_monitor=True):
    s = settings or load_settings()
    session_secret = secrets.token_bytes(32)
    login_attempts = {}

    @asynccontextmanager
    async def lifespan(app):
        store = Store(s["data_dir"])
        config = store.get("config") or read_config(s["config_path"])
        validate_config(config)
        monitor = Monitor(s, store, config)
        app.state.store, app.state.monitor = store, monitor
        if not store.get("initial_market_verification"):
            store.event("rule_audit", {"severity": "info", "title": "HL 精确市场身份核对", "actor": "安装初始化",
                "old": "说明书 ANT", "new": config["strategy"]["hyperliquid_coin"],
                "reason": "io 的官方 metadata 以及用户公开地址持仓的 coin 为 io:ANTH；不对其他市场或相似代币做自动匹配。",
                "source": "https://api.hyperliquid.xyz/info (perpDexs, metaAndAssetCtxs, clearinghouseState)"})
            store.set("initial_market_verification", True)
        if start_monitor:
            await monitor.start()
        try:
            yield
        finally:
            await monitor.close()
            store.close()

    app = FastAPI(title="ANT Monitor · Read Only", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def local(request):
        return request.client and request.client.host in {"127.0.0.1", "::1", "testclient"}

    def authenticated(request):
        if not s.get("dashboard_password"):
            return local(request)
        token = request.cookies.get("ant_session", "")
        try:
            payload, signature = token.rsplit(".", 1)
            expected = hmac.new(session_secret, payload.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature) and int(payload) > int(time.time())
        except (ValueError, TypeError):
            return False

    @app.middleware("http")
    async def security(request, call_next):
        if not s.get("dashboard_password"):
            host = request.url.hostname
            if not local(request) or host not in {"localhost", "127.0.0.1", "::1", "testserver"}:
                return JSONResponse({"detail": "远程访问必须先设置 DASHBOARD_PASSWORD"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.url.netloc:
                return JSONResponse({"detail": "跨站请求已拒绝"}, status_code=403)
            try:
                if int(request.headers.get("content-length", "0")) > 65536:
                    return JSONResponse({"detail": "请求过大"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "无效请求"}, status_code=400)
        public = {"/api/auth/status", "/api/auth/login", "/healthz"}
        if request.url.path.startswith("/api/") and request.url.path not in public and not authenticated(request):
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/api/auth/status")
    async def auth_status(request: Request):
        return {"required": bool(s.get("dashboard_password")), "authenticated": authenticated(request), "local_only": not bool(s.get("dashboard_password"))}

    class Login(BaseModel):
        password: str = Field(max_length=512)

    @app.post("/api/auth/login")
    async def login(body: Login, request: Request):
        ip = request.client.host
        attempts = [stamp for stamp in login_attempts.get(ip, []) if time.time() - stamp < 300]
        if len(attempts) >= 10:
            raise HTTPException(429, "登录尝试过多，请稍后再试")
        attempts.append(time.time())
        login_attempts[ip] = attempts
        if not s.get("dashboard_password") or not hmac.compare_digest(body.password.encode(), s["dashboard_password"].encode()):
            await asyncio.sleep(0.25)
            raise HTTPException(401, "密码错误")
        login_attempts.pop(ip, None)
        payload = str(int(time.time()) + 12 * 3600)
        signature = hmac.new(session_secret, payload.encode(), hashlib.sha256).hexdigest()
        response = JSONResponse({"authenticated": True})
        response.set_cookie("ant_session", payload + "." + signature, httponly=True, secure=s.get("cookie_secure", False), samesite="strict", max_age=12*3600)
        return response

    @app.post("/api/auth/logout")
    async def logout():
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("ant_session")
        return response

    @app.get("/api/status")
    async def status():
        address = s.get("hl_address", "")
        return {"okx": {"configured": all(s.get(key) for key in ("okx_api_key", "okx_api_secret", "okx_passphrase")), "base_url": s["okx_base_url"]},
                "hl": {"configured": bool(address), "address_masked": address[:6] + "…" + address[-4:] if address else None},
                "bark": {"enabled": s.get("bark_enabled", False), "configured": bool(s.get("bark_device_key")), "critical": s.get("bark_critical", False), "min_severity": s.get("bark_min_severity", "warning")},
                "telegram": {"enabled": s.get("telegram_enabled", False), "configured": bool(s.get("telegram_bot_token") and s.get("telegram_chat_id"))},
                "storage": {"enabled": True}, "readonly": True, "version": "1.0.0", "public_ws": s.get("enable_public_ws", True)}

    @app.get("/api/snapshot")
    async def snapshot():
        return app.state.monitor.snapshot()

    @app.get("/api/history")
    async def history(hours: int = 24):
        return app.state.store.history(max(1, min(hours, 720)))

    @app.get("/api/events")
    async def events(limit: int = 150):
        return app.state.store.events(limit=max(1, min(limit, 500)))

    @app.get("/api/config")
    async def config():
        return app.state.monitor.config

    class ConfigUpdate(BaseModel):
        config: dict
        actor: str = Field(min_length=1, max_length=80)
        reason: str = Field(min_length=3, max_length=1000)
        review_rules: bool = False

    @app.put("/api/config")
    async def save_config(body: ConfigUpdate):
        if not body.actor.strip() or len(body.reason.strip()) < 3:
            raise HTTPException(422, "请填写确认人和变更原因")
        try:
            validate_config(body.config)
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(422, str(error))
        store, monitor = app.state.store, app.state.monitor
        previous = monitor.config
        # Store parameter update and its audit entry in one SQLite transaction.
        from .storage import encode
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO kv VALUES ('config',?)", (encode(body.config),))
            store.db.execute("INSERT INTO events(timestamp,kind,value) VALUES (?,?,?)", (now_ms(), "rule_audit", encode({
                "severity": "info", "title": "监控配置人工更新", "old": previous, "new": body.config,
                "actor": body.actor.strip(), "reason": body.reason.strip(), "review_rules": body.review_rules,
                "source": body.config["conversion"].get("source")})))
            if body.review_rules:
                store.db.execute("INSERT OR REPLACE INTO kv VALUES ('rule_unreviewed','false')")
        monitor.configure(body.config)
        return {"ok": True, "config": body.config}

    @app.post("/api/alerts/test")
    async def test_alert():
        if not (s.get("bark_enabled") and s.get("bark_device_key") or s.get("telegram_enabled") and s.get("telegram_bot_token") and s.get("telegram_chat_id")):
            raise HTTPException(409, "请先在 .env 配置并启用 Bark，然后重启服务")
        alert = {"code": "MANUAL_TEST", "severity": "critical", "exchange": None, "title": "测试告警",
                 "message": "用户在监控页主动发送的通知测试。", "value": "test", "threshold": "manual", "formula": "手动测试，无交易操作", "source": "ANT Monitor", "timestamp": now_ms()}
        delivery = await app.state.monitor.notifier.send(alert)
        app.state.store.event("notification_test", {**alert, "delivery": delivery})
        if "failed" in delivery.values():
            raise HTTPException(502, {"message": "通知未成功送达，请检查设备 Key 与网络", "delivery": delivery})
        return {"ok": True, "delivery": delivery}

    dist = ROOT / "frontend/dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        index = dist / "index.html"
        if not index.exists():
            return JSONResponse({"detail": "请先运行 cd frontend && npm ci && npm run build"}, status_code=503)
        candidate = (dist / path).resolve()
        if candidate.is_relative_to(dist) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    return app
