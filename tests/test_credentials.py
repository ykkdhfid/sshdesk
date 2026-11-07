"""凭据库与审计日志测试。"""

from __future__ import annotations

import pytest

from sshdesk.core.audit import AuditLogger, redact
from sshdesk.core.credentials import CredentialStore


@pytest.fixture
def creds(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHDESK_HOME", str(tmp_path))
    return CredentialStore(tmp_path / "credentials.enc")


def test_set_get_delete(creds):
    creds.set_password("pw-1", "s3cret!密码")
    assert creds.get_password("pw-1") == "s3cret!密码"
    assert creds.has_password("pw-1") is True
    creds.delete_password("pw-1")
    assert creds.has_password("pw-1") is False


def test_missing_password_raises(creds):
    with pytest.raises(Exception):
        creds.get_password("不存在")


def test_password_is_not_plaintext_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHDESK_HOME", str(tmp_path))
    store = CredentialStore(tmp_path / "credentials.enc")
    store.set_password("pw-1", "SuperSecret123")
    blob = (tmp_path / "credentials.enc").read_bytes()
    assert b"SuperSecret123" not in blob


def test_reload_same_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHDESK_HOME", str(tmp_path))
    path = tmp_path / "credentials.enc"
    store = CredentialStore(path)
    store.set_password("pw-x", "token")
    assert CredentialStore(path).get_password("pw-x") == "token"


def test_redact_password_forms():
    assert "***" in redact("mysql --password=abc123")
    assert "abc123" not in redact("mysql --password=abc123")
    assert "Bearer" in redact("Authorization: Bearer eyJhbGciOi.abc.def")
    assert "eyJhbGciOi" not in redact("Authorization: Bearer eyJhbGciOi.abc.def")
    assert "[PRIVATE KEY REDACTED]" in redact(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3Nza\n-----END OPENSSH PRIVATE KEY-----"
    )


def test_redact_leaves_normal_commands():
    assert redact("df -h && uptime") == "df -h && uptime"


def test_audit_log_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHDESK_HOME", str(tmp_path))
    logger = AuditLogger(tmp_path / "audit.log")
    logger.log("connect", "root@1.2.3.4:22", "password=abc", ok=True)
    entries = logger.tail()
    assert len(entries) == 1
    assert entries[0]["event"] == "connect"
    assert "abc" not in entries[0]["detail"]


def test_audit_disabled_writes_nothing(tmp_path):
    logger = AuditLogger(tmp_path / "audit.log", enabled=False)
    logger.log("connect", "x")
    assert not (tmp_path / "audit.log").exists()
