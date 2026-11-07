"""服务器配置模型与本地持久化。

设计要点：
- 配置里永远不出现明文密码，只保留一个 password_ref 指向凭据库（见 credentials.py）。
- 写入采用 "临时文件 + os.replace" 的原子替换，避免程序崩溃时把配置写坏。
- 配置文件权限收紧到 0600，防止同机其他用户读取主机信息。
"""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

APP_DIR_ENV = "SSHDESK_HOME"


def app_dir() -> Path:
    """应用数据目录，默认 ~/.sshdesk，可用环境变量覆盖（便于测试与绿色版）。"""
    base = os.environ.get(APP_DIR_ENV) or str(Path.home() / ".sshdesk")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


@dataclass
class ServerConfig:
    """一台服务器的连接配置。"""

    name: str
    host: str
    username: str
    port: int = 22
    auth_type: str = "key"  # key | password | agent
    key_path: str = ""
    password_ref: str = ""  # 凭据库中的键，不存明文
    group: str = "默认"
    tags: list[str] = field(default_factory=list)
    jump_host: str = ""  # 跳板机 "user@host:port"，留空表示直连
    timeout: int = 15
    note: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    last_connected_at: float = 0.0

    def validate(self) -> None:
        """在保存/连接前做基本校验，把错误提前到用户输入阶段。"""
        if not self.name.strip():
            raise ValueError("服务器名称不能为空")
        if not self.host.strip():
            raise ValueError("主机地址不能为空")
        if not self.username.strip():
            raise ValueError("用户名不能为空")
        if not isinstance(self.port, int) or not (0 < self.port < 65536):
            raise ValueError(f"端口不合法：{self.port}")
        if self.auth_type not in ("key", "password", "agent"):
            raise ValueError(f"未知的认证方式：{self.auth_type}")
        if self.auth_type == "key" and not self.key_path.strip():
            raise ValueError("密钥认证需要指定私钥文件路径")
        if self.auth_type == "key" and not Path(self.key_path).expanduser().exists():
            raise ValueError(f"私钥文件不存在：{self.key_path}")

    def jump_endpoint(self) -> tuple[str, int]:
        """解析跳板机配置，返回 (host, port)；未配置时返回 ("", 0)。"""
        return parse_jump(self.jump_host)

    @property
    def endpoint(self) -> str:
        return f"{self.username}@{self.host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServerConfig":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


def parse_jump(expr: str) -> tuple[str, int]:
    """把 'root@10.0.0.5:2222' 解析成 ('10.0.0.5', 2222)，容错常见写法。"""
    if not expr.strip():
        return "", 0
    expr = expr.strip()
    if "@" in expr:
        expr = expr.split("@", 1)[1]
    if ":" in expr:
        host, port = expr.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return expr, 22
    return expr, 22


class ConfigStore:
    """servers.json 的读写仓库。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_dir() / "servers.json")
        self._servers: list[ServerConfig] = []
        self.load()

    # ---------- 持久化 ----------
    def load(self) -> list[ServerConfig]:
        if not self.path.exists():
            self._servers = []
            return self._servers
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 配置损坏时不静默丢弃：备份一份再重建，避免用户数据凭空消失
            backup = self.path.with_suffix(f".broken.{int(time.time())}")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            self._servers = []
            return self._servers
        items = raw.get("servers", raw) if isinstance(raw, dict) else raw
        self._servers = [ServerConfig.from_dict(i) for i in items if isinstance(i, dict)]
        return self._servers

    def save(self) -> None:
        payload = {
            "version": 1,
            "servers": [s.to_dict() for s in self._servers],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, self.path)  # 原子替换

    # ---------- CRUD ----------
    def all(self) -> list[ServerConfig]:
        return list(self._servers)

    def get(self, server_id: str) -> ServerConfig | None:
        for s in self._servers:
            if s.id == server_id:
                return s
        return None

    def find_by_name(self, name: str) -> ServerConfig | None:
        for s in self._servers:
            if s.name == name or s.endpoint == name:
                return s
        return None

    def upsert(self, server: ServerConfig) -> ServerConfig:
        server.validate()
        for idx, existing in enumerate(self._servers):
            if existing.id == server.id:
                self._servers[idx] = server
                self.save()
                return server
        self._servers.append(server)
        self.save()
        return server

    def delete(self, server_id: str) -> bool:
        for idx, existing in enumerate(self._servers):
            if existing.id == server_id:
                del self._servers[idx]
                self.save()
                return True
        return False

    def touch_connected(self, server_id: str) -> None:
        server = self.get(server_id)
        if server:
            server.last_connected_at = time.time()
            self.save()
