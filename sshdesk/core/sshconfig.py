"""把 ~/.ssh/config 里的主机条目导入成 SSHDesk 的服务器配置。

只做"读"，不做"写"：不会修改用户的 ~/.ssh/config。
支持的指令：Host / HostName / User / Port / IdentityFile / ProxyJump。
Host 后面的通配模式（`Host *`、`Host *.example.com`）会跳过，不做猜测式展开。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import ServerConfig

DEFAULT_GROUP = "导入"


@dataclass
class SshHostEntry:
    alias: str
    hostname: str = ""
    user: str = ""
    port: int = 22
    identity_file: str = ""
    proxy_jump: str = ""

    @property
    def target(self) -> str:
        """没有 HostName 时，ssh 会把别名本身当主机名。"""
        return self.hostname or self.alias


def _strip_comment(line: str) -> str:
    quote = None
    out: list[str] = []
    for ch in line:
        if ch in ("'", '"'):
            quote = None if quote == ch else (ch if quote is None else quote)
        if ch == "#" and quote is None:
            break
        out.append(ch)
    return "".join(out)


def _is_pattern(value: str) -> bool:
    return any(c in value for c in "*?!,")


def _split_key_value(line: str) -> tuple[str, str]:
    """兼容 `Host foo` 与 `Host=foo` 两种写法。"""
    first = line.split(None, 1)[0]
    if "=" in first:
        key, _, value = line.partition("=")
        return key.strip(), value.strip()
    parts = line.split(None, 1)
    return parts[0], (parts[1].strip() if len(parts) > 1 else "")


def _apply(entry: SshHostEntry, key: str, value: str) -> None:
    if key == "hostname":
        entry.hostname = value
    elif key == "user":
        entry.user = value
    elif key == "port":
        try:
            entry.port = int(value)
        except ValueError:
            pass
    elif key == "identityfile":
        entry.identity_file = value
    elif key == "proxyjump":
        entry.proxy_jump = value


def parse_ssh_config(text: str) -> list[SshHostEntry]:
    entries: list[SshHostEntry] = []
    defaults: dict[str, str] = {}
    current: SshHostEntry | None = None
    in_defaults = False

    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        key, value = _split_key_value(line)
        key = key.lower()

        if key == "host":
            current = None
            in_defaults = False
            aliases = value.split()
            if not aliases:
                continue
            concrete = [a for a in aliases if not _is_pattern(a)]
            if not concrete:
                # 只有 `Host *` 是 ssh 的全局兜底；带域名的通配块无法静态展开，整块忽略
                if aliases == ["*"]:
                    in_defaults = True
                continue
            current = SshHostEntry(alias=concrete[0])
            entries.append(current)
        elif key == "match":
            # Match 块的语义依赖运行时条件，静态导入时直接跳过
            current = None
            in_defaults = False
        elif current is not None:
            _apply(current, key, value)
        elif in_defaults:
            defaults[key] = value

    for entry in entries:
        if not entry.user:
            entry.user = defaults.get("user", "")
        if entry.port == 22 and defaults.get("port"):
            try:
                entry.port = int(defaults["port"])
            except ValueError:
                pass
        if not entry.identity_file:
            entry.identity_file = defaults.get("identityfile", "")
        if not entry.proxy_jump:
            entry.proxy_jump = defaults.get("proxyjump", "")
    return entries


def expand_identity_path(value: str) -> str:
    """展开 ~ 与 %d（ssh 里 %d 代表本地 home 目录）。"""
    if not value:
        return ""
    value = value.replace("%d", str(Path.home()))
    return os.path.expanduser(value)


def to_server_config(entry: SshHostEntry, group: str = DEFAULT_GROUP) -> ServerConfig:
    key_path = expand_identity_path(entry.identity_file)
    has_key = bool(key_path) and Path(key_path).exists()
    return ServerConfig(
        name=entry.alias,
        host=entry.target,
        username=entry.user or os.environ.get("USER") or "root",
        port=entry.port,
        # 私钥不存在时退回 agent，避免导入后立刻在 validate 里报错
        auth_type="key" if has_key else "agent",
        key_path=key_path if has_key else "",
        group=group,
        jump_host=entry.proxy_jump,
        note="从 ~/.ssh/config 导入",
    )


def load_ssh_config(path: str | Path | None = None) -> list[SshHostEntry]:
    target = Path(path).expanduser() if path else Path.home() / ".ssh" / "config"
    if not target.exists():
        return []
    return parse_ssh_config(target.read_text(encoding="utf-8", errors="replace"))
