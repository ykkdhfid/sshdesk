"""诊断层测试：私网识别、端口探测、结论生成。"""

from __future__ import annotations

import socket
import threading

from sshdesk.core.config import ServerConfig
from sshdesk.core.diagnostics import (Report, diagnose, is_private_host, probe_tcp,
                                      _split_sections)


class _FakeSSHServer:
    """监听本地端口并返回 SSH banner，用来模拟真实 sshd。"""

    def __init__(self, banner: bytes = b"SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.10\r\n") -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.banner = banner
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
            conn.sendall(self.banner)
            conn.close()
        except OSError:
            pass

    def close(self) -> None:
        self.sock.close()


def test_private_address_detection():
    assert is_private_host("172.16.0.12") is True   # 阿里云 VPC 私网
    assert is_private_host("10.0.0.8") is True
    assert is_private_host("192.168.1.5") is True
    assert is_private_host("100.64.1.1") is True      # 运营商级 NAT
    assert is_private_host("127.0.0.1") is True
    assert is_private_host("47.108.20.30") is False   # 公网
    assert is_private_host("server.example.com") is False


def test_probe_detects_ssh_service():
    server = _FakeSSHServer()
    try:
        result = probe_tcp("127.0.0.1", server.port, timeout=2)
        assert result.ok is True
        assert result.error == ""
        assert result.latency_ms >= 0
    finally:
        server.close()


def test_probe_flags_non_ssh_service():
    server = _FakeSSHServer(banner=b"HTTP/1.1 200 OK\r\n")
    try:
        result = probe_tcp("127.0.0.1", server.port, timeout=2)
        assert result.ok is True
        assert "不是 sshd" in result.error
    finally:
        server.close()


def test_probe_closed_port_reports_error():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # 端口立刻关闭，探测应得到拒绝
    result = probe_tcp("127.0.0.1", port, timeout=1)
    assert result.ok is False
    assert result.error


def test_diagnose_private_ip_suggests_public_ip():
    server = ServerConfig(name="阿里云成都", host="172.16.0.12", username="root",
                          auth_type="agent", timeout=1)
    report = diagnose(server, timeout=1)
    assert report.level in ("warn", "error")
    joined = " ".join(report.suggestions)
    assert "公网 IP" in joined
    assert any("私网地址" in item.detail for item in report.items)


def test_diagnose_public_ip_ok():
    server_obj = _FakeSSHServer()
    try:
        server = ServerConfig(name="test", host="127.0.0.1", username="root",
                              port=server_obj.port, auth_type="agent", timeout=2)
        report = diagnose(server, timeout=2)
        assert report.level in ("ok", "warn")
        assert any(item.status == "ok" and item.name == "SSH 端口" for item in report.items)
    finally:
        server_obj.close()


def test_report_text_contains_all_items():
    report = Report(target="1.2.3.4:22")
    report.add("端口", "ok", "22ms")
    report.suggest("检查安全组")
    text = report.to_text()
    assert "1.2.3.4:22" in text
    assert "检查安全组" in text
    assert "端口" in text


def test_split_sections_ignores_shell_prompts():
    text = "#SSH\nactive\n#UFW\nStatus: inactive"
    assert _split_sections(text) == {"SSH": "active", "UFW": "Status: inactive"}
