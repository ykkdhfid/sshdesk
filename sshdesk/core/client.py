"""SSH 连接层：认证、主机密钥校验、保活、跳板机、PTY、命令执行、SFTP。

这一层不依赖任何 UI，CLI 与 GUI 共用，也是单元测试的重点。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import paramiko

from .config import ServerConfig, app_dir
from .credentials import CredentialStore, CredentialError, key_fingerprint, load_private_key

HostKeyCallback = Callable[[str, str, str], bool]
"""未知主机确认回调：(hostname, key_type, fingerprint) -> 是否信任。"""


class SSHError(Exception):
    """对 paramiko 异常的统一包装，message 直接面向用户。"""

    def __init__(self, message: str, hint: str = "", cause: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.cause = cause

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"{self.message}" + (f"\n建议：{self.hint}" if self.hint else "")


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error


class SSHConnection:
    """一台服务器的单次连接会话。"""

    def __init__(
        self,
        server: ServerConfig,
        credentials: CredentialStore | None = None,
        on_unknown_host: HostKeyCallback | None = None,
        known_hosts: Path | None = None,
    ) -> None:
        self.server = server
        self.credentials = credentials or CredentialStore()
        self.on_unknown_host = on_unknown_host
        self.known_hosts = known_hosts or (app_dir() / "known_hosts")
        self._client: paramiko.SSHClient | None = None
        self._jump: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._shell: paramiko.Channel | None = None

    # ------------------------------------------------------------------ 连接
    def connect(self, password: str | None = None) -> "SSHConnection":
        server = self.server
        password = password or self._resolve_password()

        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        if self.known_hosts.exists():
            try:
                self._client.load_host_keys(str(self.known_hosts))
            except (OSError, paramiko.SSHException):
                pass
        self._client.set_missing_host_key_policy(self._make_policy())

        sock = None
        try:
            jump_host, jump_port = server.jump_endpoint()
            if jump_host:
                sock = self._open_jump_socket(jump_host, jump_port, password)

            key = self._load_key() if server.auth_type == "key" else None
            self._client.connect(
                hostname=server.host,
                port=server.port,
                username=server.username,
                pkey=key,
                password=password if server.auth_type == "password" else None,
                sock=sock,
                timeout=server.timeout,
                banner_timeout=server.timeout,
                auth_timeout=server.timeout,
                allow_agent=(server.auth_type == "agent"),
                look_for_keys=(server.auth_type == "key" and key is None),
            )
        except paramiko.AuthenticationException as exc:
            self.close()
            raise SSHError(
                "认证失败：用户名或凭据不正确",
                hint="确认用户名/密码；密钥认证请确认 private key 已加入 ~/.ssh/authorized_keys，"
                "并检查 Ubuntu 的 PasswordAuthentication 设置。",
                cause=str(exc),
            ) from None
        except paramiko.BadHostKeyException as exc:
            self.close()
            raise SSHError("主机密钥与本地记录不一致，可能存在中间人风险",
                           hint="确认服务器是否重装过；如确认安全，删除 ~/.sshdesk/known_hosts 中对应条目后重连。",
                           cause=str(exc)) from None
        except (paramiko.SSHException, socket.timeout, OSError) as exc:
            self.close()
            raise SSHError("无法连接到服务器", hint=connect_hint(server, exc), cause=str(exc)) from None

        transport = self._client.get_transport()
        if transport:
            transport.set_keepalive(30)  # 防止 NAT/空闲超时把连接掐断
        return self

    def _make_policy(self) -> paramiko.MissingHostKeyPolicy:
        connection = self

        class _Policy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key) -> None:  # type: ignore[override]
                fingerprint = key_fingerprint(key)
                if connection.on_unknown_host is None:
                    raise SSHError(
                        "首次连接该服务器，需要人工确认主机指纹",
                        hint=f"指纹 {fingerprint}。在图形界面中确认，或先 ssh 手动登录一次。",
                    )
                if not connection.on_unknown_host(hostname, key.get_name(), fingerprint):
                    raise SSHError("已取消信任该主机密钥")
                _append_known_host(connection.known_hosts, hostname, key)

        return _Policy()

    def _resolve_password(self) -> str | None:
        if self.server.auth_type != "password":
            return None
        if not self.server.password_ref:
            return None
        try:
            return self.credentials.get_password(self.server.password_ref)
        except CredentialError:
            return None

    def _load_key(self):
        try:
            return load_private_key(self.server.key_path)
        except CredentialError as exc:
            raise SSHError(str(exc), hint="检查私钥路径与文件权限（chmod 600）。") from None

    def _open_jump_socket(self, host: str, port: int, password: str | None) -> paramiko.Channel:
        """通过跳板机的 direct-tcpip 通道建立到目标机 TCP 连接。"""
        self._jump = paramiko.SSHClient()
        self._jump.load_system_host_keys()
        if self.known_hosts.exists():
            try:
                self._jump.load_host_keys(str(self.known_hosts))
            except (OSError, paramiko.SSHException):
                pass
        self._jump.set_missing_host_key_policy(self._make_policy())
        self._jump.connect(
            hostname=host,
            port=port or 22,
            username=self.server.username,
            password=password,
            timeout=self.server.timeout,
            banner_timeout=self.server.timeout,
            auth_timeout=self.server.timeout,
            allow_agent=True,
            look_for_keys=True,
        )
        transport = self._jump.get_transport()
        if transport is None:
            raise SSHError("跳板机连接建立失败")
        return transport.open_channel(
            "direct-tcpip", (self.server.host, self.server.port), ("127.0.0.1", 0)
        )

    # ------------------------------------------------------------------ 能力
    def open_shell(self, cols: int = 100, rows: int = 30) -> paramiko.Channel:
        """申请一个交互式 PTY。xterm-256color 让远端程序输出彩色与光标控制序列。"""
        if self._client is None:
            raise SSHError("尚未连接")
        try:
            chan = self._client.invoke_shell(term="xterm-256color", width=cols, height=rows)
        except paramiko.SSHException as exc:
            raise SSHError("申请交互终端失败，服务器可能禁用了 PTY", cause=str(exc)) from None
        chan.settimeout(0.0)  # 非阻塞，配合 UI 轮询
        self._shell = chan
        return chan

    def resize_pty(self, cols: int, rows: int) -> None:
        if self._shell is not None:
            try:
                self._shell.resize_pty(width=cols, height=rows)
            except (OSError, paramiko.SSHException):
                pass

    def exec_command(self, command: str, timeout: int | None = None) -> ExecResult:
        """执行单条命令并收集结果。注意：先读完 stdout/stderr，再取退出码，避免死锁。"""
        if self._client is None:
            raise SSHError("尚未连接")
        start = time.time()
        try:
            _, stdout, stderr = self._client.exec_command(command, timeout=timeout or 30)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            return ExecResult(out, err, code, time.time() - start)
        except (paramiko.SSHException, socket.timeout, OSError) as exc:
            return ExecResult("", "", -1, time.time() - start, error=str(exc))

    def get_sftp(self) -> paramiko.SFTPClient:
        if self._client is None:
            raise SSHError("尚未连接")
        if self._sftp is None:
            try:
                self._sftp = self._client.open_sftp()
            except paramiko.SSHException as exc:
                raise SSHError("SFTP 不可用，服务器可能未开启 sftp 子系统", cause=str(exc)) from None
        return self._sftp

    def is_connected(self) -> bool:
        transport = self._client.get_transport() if self._client else None
        return bool(transport and transport.is_active())

    def close(self) -> None:
        for obj, closer in (
            (self._sftp, lambda o: o.close()),
            (self._shell, lambda o: o.close()),
            (self._jump, lambda o: o.close()),
        ):
            if obj is not None:
                try:
                    closer(obj)
                except Exception:
                    pass
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._sftp = self._shell = self._jump = self._client = None


def _append_known_host(path: Path, hostname: str, key) -> None:
    """把确认过的主机密钥写入本地 known_hosts，下次秒过。"""
    try:
        keys = paramiko.HostKeys(str(path)) if path.exists() else paramiko.HostKeys()
        keys.add(hostname, key.get_name(), key)
        path.parent.mkdir(parents=True, exist_ok=True)
        keys.save(str(path))
        path.chmod(0o600)
    except (OSError, paramiko.SSHException):
        pass


def connect_hint(server: ServerConfig, exc: Exception) -> str:
    """把 socket 层错误翻译成用户能直接照做的建议。"""
    text = f"{exc}".lower()
    if "timed out" in text or "timeout" in text:
        return (
            f"连接 {server.host}:{server.port} 超时。请依次检查："
            "① 阿里云实例是否已分配公网 IP（控制台实例详情里公网 IP 不能是“-”）；"
            "② 安全组是否放行 22 端口；"
            "③ 本地网络是否可出网（公司/校园网常封 22）。"
        )
    if "refused" in text:
        return "端口拒绝连接：sshd 未运行或未监听该端口。在阿里云 Workbench 内执行 systemctl status ssh 检查。"
    if "unable to connect" in text or "no route" in text:
        return "网络不可达：地址或路由有问题，确认 IP/端口是否正确、实例是否在同一网络。"
    return "检查地址、端口、安全组与 sshd 状态。"


def build_connection(server: ServerConfig, credentials: CredentialStore | None = None,
                     on_unknown_host: HostKeyCallback | None = None) -> SSHConnection:
    return SSHConnection(server, credentials=credentials, on_unknown_host=on_unknown_host)
