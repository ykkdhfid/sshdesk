"""Web 版 SSH 终端：给手机/平板浏览器用的轻量入口。

安全模型（比功能更重要）：
1. 必须有访问令牌。未显式配置时自动生成一个 32 字节随机串，写到
   ~/.sshdesk/web_token（0600），启动日志打印一次。
2. 默认只监听 127.0.0.1。想从手机访问，用 SSH 隧道或 Nginx 反代 + HTTPS，
   不建议直接把端口暴露到公网。
3. 首次连接未知主机时默认拒绝（不像桌面端可以弹窗确认）。要放行必须显式设置
   SSHDESK_WEB_AUTO_TRUST=1，并在日志留痕。
4. 登录失败有速率限制，避免令牌被暴力枚举。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..core.audit import AuditLogger
from ..core.client import SSHError, build_connection
from ..core.config import ConfigStore, ServerConfig, app_dir
from ..core.credentials import CredentialStore
from ..core.monitor import collect

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "sshdesk_token"
TOKEN_ENV = "SSHDESK_WEB_TOKEN"
AUTO_TRUST_ENV = "SSHDESK_WEB_AUTO_TRUST"

LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_FAILURES = 5


def load_or_create_token() -> str:
    """优先环境变量；否则读本地文件，没有就生成。"""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    path = app_dir() / "web_token"
    if path.exists():
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


class LoginPayload(BaseModel):
    token: str


class _LoginLimiter:
    """按来源 IP 统计失败次数，超限后冷却。"""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = defaultdict(list)

    def check(self, ip: str) -> None:
        now = time.time()
        recent = [t for t in self._failures[ip] if now - t < LOGIN_WINDOW_SECONDS]
        self._failures[ip] = recent
        if len(recent) >= LOGIN_MAX_FAILURES:
            raise HTTPException(429, "尝试次数过多，请 1 分钟后再试")

    def fail(self, ip: str) -> None:
        self._failures[ip].append(time.time())

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:  # 走 Nginx 反代时取真实来源
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(
    store: ConfigStore | None = None,
    credentials: CredentialStore | None = None,
    token: str | None = None,
) -> FastAPI:
    app = FastAPI(title="SSHDesk Web", docs_url=None, redoc_url=None)
    app.state.store = store or ConfigStore()
    app.state.credentials = credentials or CredentialStore()
    app.state.token = token or load_or_create_token()
    app.state.limiter = _LoginLimiter()
    app.state.audit = AuditLogger()
    app.state.generated_token = token is None

    def require_token(request: Request) -> None:
        given = request.cookies.get(COOKIE_NAME, "")
        if not hmac.compare_digest(given, app.state.token):
            raise HTTPException(401, "未登录或令牌无效")

    # ------------------------------------------------------------------ 页面
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "servers": len(app.state.store.all()), "time": int(time.time())}

    # ------------------------------------------------------------------ 登录
    @app.post("/api/login")
    def login(payload: LoginPayload, request: Request) -> JSONResponse:
        ip = _client_ip(request)
        app.state.limiter.check(ip)
        if not hmac.compare_digest(payload.token.strip(), app.state.token):
            app.state.limiter.fail(ip)
            app.state.audit.log("web_login", ip, "令牌错误", ok=False)
            raise HTTPException(401, "令牌不正确")
        app.state.limiter.reset(ip)
        app.state.audit.log("web_login", ip, ok=True)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE_NAME, app.state.token, httponly=True, samesite="strict",
            max_age=7 * 24 * 3600,
            secure=os.environ.get("SSHDESK_WEB_SECURE_COOKIE") == "1",
        )
        return response

    @app.post("/api/logout")
    def logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME)
        return response

    # ------------------------------------------------------------------ 数据
    @app.get("/api/servers")
    def list_servers(_: None = Depends(require_token)) -> list[dict[str, Any]]:
        items = []
        for server in app.state.store.all():
            items.append({
                "id": server.id,
                "name": server.name,
                "host": server.host,
                "port": server.port,
                "username": server.username,
                "group": server.group,
                "tags": server.tags,
                "auth": server.auth_type,
                "last_connected_at": server.last_connected_at,
                "note": server.note,
            })
        return items

    @app.get("/api/monitor/{server_id}")
    def monitor(server_id: str, _: None = Depends(require_token)) -> dict[str, Any]:
        server = _get_server(app, server_id)
        conn = build_connection(server, app.state.credentials, _auto_trust_handler(app))
        try:
            conn.connect()
            snapshot = collect(conn)
        except SSHError as exc:
            raise HTTPException(502, str(exc)) from None
        except Exception as exc:  # 采集脚本可能因为对方缺命令而失败，别让它变成 500
            raise HTTPException(502, f"读取系统状态失败：{exc}") from None
        finally:
            conn.close()
        return {
            "cpu": snapshot.cpu_percent,
            "load": [snapshot.load1, snapshot.load5, snapshot.load15],
            "cores": snapshot.cores,
            "mem_percent": snapshot.mem_percent,
            "mem_text": snapshot.mem_text,
            "disk_percent": snapshot.disk_percent,
            "disk_text": snapshot.disk_text,
            "uptime": snapshot.uptime_text,
            "os": snapshot.os_name,
            "kernel": snapshot.kernel,
            "ssh": snapshot.ssh_service,
            "processes": [
                {"cpu": p.cpu_percent, "mem": p.mem_percent, "cmd": p.command}
                for p in snapshot.processes
            ],
        }

    # ------------------------------------------------------------------ 终端
    @app.websocket("/ws/term/{server_id}")
    async def terminal(ws: WebSocket, server_id: str) -> None:
        # 先 accept 再校验：ASGI 规范下在 accept 之前 close 会被降级成 HTTP 403，
        # 浏览器就读不到 4401 这个「请重新登录」的信号了。
        await ws.accept()
        given = ws.cookies.get(COOKIE_NAME, "") or ws.query_params.get("token", "")
        if not hmac.compare_digest(given, app.state.token):
            await ws.close(code=4401)  # 前端据此跳回登录页
            return
        try:
            server = _get_server(app, server_id)
        except HTTPException:
            await ws.close(code=4404)
            return

        loop = asyncio.get_running_loop()
        conn = build_connection(server, app.state.credentials, _auto_trust_handler(app))
        channel = None
        try:
            await asyncio.to_thread(conn.connect)
            channel = await asyncio.to_thread(conn.open_shell, 100, 30)
            channel.settimeout(0.5)
            app.state.audit.log("web_terminal_open", server.endpoint, ok=True)
            await ws.send_text(json.dumps({"type": "ready", "name": server.name}))
        except SSHError as exc:
            await ws.send_text(json.dumps({
                "type": "error",
                "message": exc.message,
                "hint": exc.hint,
            }, ensure_ascii=False))
            await ws.close(code=4502)
            conn.close()
            return

        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def pump_remote() -> None:
            """阻塞读远端输出，把数据丢回事件循环。"""
            while True:
                try:
                    data = channel.recv(8192)
                except socket.timeout:
                    continue
                except Exception:
                    break
                if not data:
                    break
                loop.call_soon_threadsafe(queue.put_nowait, data)
            loop.call_soon_threadsafe(queue.put_nowait, None)

        reader = threading.Thread(target=pump_remote, daemon=True)
        reader.start()

        async def send_output() -> None:
            while True:
                data = await queue.get()
                if data is None:
                    break
                try:
                    await ws.send_bytes(data)
                except Exception:
                    break
            try:
                await ws.send_text(json.dumps({"type": "closed"}))
            except Exception:
                pass

        async def receive_input() -> None:
            async for message in ws.iter_text():
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                kind = payload.get("type")
                if kind == "input":
                    await asyncio.to_thread(channel.send, payload.get("data", "").encode("utf-8"))
                elif kind == "resize":
                    cols = int(payload.get("cols", 80))
                    rows = int(payload.get("rows", 24))
                    await asyncio.to_thread(conn.resize_pty, cols, rows)
                elif kind == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))

        tasks = [asyncio.create_task(send_output()), asyncio.create_task(receive_input())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            for task in tasks:
                task.cancel()
            try:
                channel.close()
            except Exception:
                pass
            conn.close()
            app.state.audit.log("web_terminal_close", server.endpoint, ok=True)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _get_server(app: FastAPI, server_id: str) -> ServerConfig:
    server = app.state.store.get(server_id) or app.state.store.find_by_name(server_id)
    if not server:
        raise HTTPException(404, "服务器不存在")
    return server


def _auto_trust_handler(app: FastAPI):
    """默认拒绝未知主机；只有显式开启才自动信任（并写审计）。"""
    enabled = os.environ.get(AUTO_TRUST_ENV) == "1"

    def handler(hostname: str, key_type: str, fingerprint: str) -> bool:
        if enabled:
            app.state.audit.log("web_auto_trust_hostkey", hostname, f"{key_type} {fingerprint}")
            return True
        raise SSHError(
            "首次连接的主机尚未信任",
            hint="请先在这台机器上用 `python -m sshdesk test 机器名 --trust-host` 确认一次指纹；"
                 "确认无误后，Web 端会自动复用 ~/.sshdesk/known_hosts。",
        )

    return handler


def main(argv: list[str] | None = None) -> int:
    """python -m sshdesk web"""
    import argparse

    parser = argparse.ArgumentParser(prog="sshdesk web", description="浏览器版 SSH 终端")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token", default="", help="访问令牌，默认读取/生成本地 token")
    args = parser.parse_args(argv)

    import uvicorn

    app = create_app(token=args.token or None)
    print("=" * 60)
    print(" SSHDesk Web 已启动")
    print(f" 地址：http://{args.host}:{args.port}")
    print(f" 令牌：{app.state.token}")
    if app.state.generated_token:
        print(f"（令牌已保存到 {app_dir() / 'web_token'}，以后可直接查看）")
    print(" 手机访问：建议用 SSH 隧道或 Nginx + HTTPS，不要直接暴露公网端口")
    print("=" * 60, flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
