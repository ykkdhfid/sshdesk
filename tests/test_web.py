"""Web 层测试：鉴权、越权防护、字段脱敏、速率限制、WebSocket 拒绝。"""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sshdesk.core.config import ConfigStore, ServerConfig
from sshdesk.core.credentials import CredentialStore
from sshdesk.web.server import create_app

TOKEN = "test-token-123"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHDESK_HOME", str(tmp_path))
    store = ConfigStore(tmp_path / "servers.json")
    store.upsert(ServerConfig(name="测试机", host="1.2.3.4", username="root",
                              auth_type="agent", group="生产", key_path="",
                              password_ref="pw-should-not-leak"))
    return create_app(store=store,
                      credentials=CredentialStore(tmp_path / "credentials.enc"),
                      token=TOKEN)


@pytest.fixture
def client(app):
    return TestClient(app)


def test_index_page_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "SSHDesk" in response.text


def test_health(client):
    assert client.get("/api/health").json()["servers"] == 1


def test_servers_requires_login(client):
    assert client.get("/api/servers").status_code == 401


def test_login_with_wrong_token(client):
    assert client.post("/api/login", json={"token": "nope"}).status_code == 401


def test_login_then_list_servers(client):
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
    servers = client.get("/api/servers").json()
    assert len(servers) == 1
    assert servers[0]["name"] == "测试机"
    # 敏感字段不得出现在响应里
    assert "password_ref" not in servers[0]
    assert "key_path" not in servers[0]


def test_cookie_is_httponly(client):
    response = client.post("/api/login", json={"token": TOKEN})
    cookie_header = response.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie_header
    assert "SameSite=strict" in cookie_header or "SameSite=Strict" in cookie_header


def test_logout_clears_access(client):
    client.post("/api/login", json={"token": TOKEN})
    assert client.get("/api/servers").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/servers").status_code == 401


def test_login_rate_limit(client):
    for _ in range(5):
        client.post("/api/login", json={"token": "bad"})
    response = client.post("/api/login", json={"token": "bad"})
    assert response.status_code == 429


def test_monitor_unknown_server(client):
    client.post("/api/login", json={"token": TOKEN})
    assert client.get("/api/monitor/not-exist").status_code == 404


def test_monitor_requires_login(client):
    assert client.get("/api/monitor/whatever").status_code == 401


def test_websocket_rejects_without_token(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/term/whatever") as ws:
            ws.receive_text()
    assert exc.value.code == 4401


def test_websocket_rejects_unknown_server(client):
    client.post("/api/login", json={"token": TOKEN})
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/term/not-exist") as ws:
            ws.receive_text()
    assert exc.value.code == 4404


def test_token_file_permissions(tmp_path, monkeypatch):
    """未显式配置 token 时应生成随机 token 并落到 0600 文件。"""
    monkeypatch.setenv("SSHDESK_HOME", str(tmp_path))
    monkeypatch.delenv("SSHDESK_WEB_TOKEN", raising=False)
    from sshdesk.web.server import load_or_create_token

    token = load_or_create_token()
    assert len(token) >= 20
    path = tmp_path / "web_token"
    assert path.exists()
    # Windows 上 chmod 不映射成 NTFS 权限，st_mode 恒为 0o666，只能跳过这条
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert load_or_create_token() == token
