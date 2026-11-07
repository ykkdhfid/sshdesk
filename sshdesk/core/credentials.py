"""凭据管理：密码加密落盘 + 私钥加载。

安全边界（务必写进 README，不要过度承诺）：
本方案的目标是「不在磁盘上留明文密码」，而不是「抗专业离线破解」。
主密钥由本机 machine-id、用户名与固定盐经 PBKDF2 派生，配置文件被拷到
另一台机器上通常解不开；但能读到本机文件的攻击者仍可解密。真要更强保护，
应改用系统 keyring（macOS Keychain / Windows DPAPI / Linux Secret Service）。
"""

from __future__ import annotations

import base64
import json
import os
import stat
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .config import app_dir

_ITERATIONS = 200_000
_SALT = b"sshdesk-local-vault-v1"
_MACHINE_ID_FILES = ("/etc/machine-id", "/var/lib/dbus/machine-id")


class CredentialError(Exception):
    """凭据读取/解密失败。"""


def _machine_id() -> str:
    for candidate in _MACHINE_ID_FILES:
        try:
            text = Path(candidate).read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    # 容器/精简系统可能没有 machine-id，退化为本地生成并持久化
    fallback = app_dir() / "machine-id"
    if not fallback.exists():
        fallback.write_text(uuid.uuid4().hex, encoding="utf-8")
        try:
            os.chmod(fallback, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return fallback.read_text(encoding="utf-8").strip()


def _derive_key() -> bytes:
    material = f"{_machine_id()}|{Path.home().name}|sshdesk".encode()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_SALT, iterations=_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(material))


class CredentialStore:
    """密码等敏感字段的加密存储（Fernet）。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_dir() / "credentials.enc")
        self._fernet = Fernet(_derive_key())
        self._cache: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._cache = {}
            return
        try:
            blob = self.path.read_bytes()
            raw = self._fernet.decrypt(blob)
            data = json.loads(raw.decode("utf-8"))
            self._cache = {str(k): str(v) for k, v in data.items()}
        except (InvalidToken, ValueError, OSError):
            # 换机/换用户后解不开是预期行为：清空为空库，不让程序崩掉
            self._cache = {}

    def _save(self) -> None:
        blob = self._fernet.encrypt(
            json.dumps(self._cache, ensure_ascii=False).encode("utf-8")
        )
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def set_password(self, ref: str, password: str) -> None:
        self._cache[ref] = password
        self._save()

    def get_password(self, ref: str) -> str:
        if ref not in self._cache:
            raise CredentialError("本地没有保存该服务器的密码，请重新输入")
        return self._cache[ref]

    def has_password(self, ref: str) -> bool:
        return bool(ref) and ref in self._cache

    def delete_password(self, ref: str) -> None:
        if ref in self._cache:
            del self._cache[ref]
            self._save()

    def clear(self) -> None:
        self._cache = {}
        self._save()


def load_private_key(path: str, passphrase: str | None = None):
    """按常见密钥类型依次尝试加载私钥，返回 paramiko PKey 对象。"""
    import paramiko

    expanded = str(Path(path).expanduser())
    if not Path(expanded).exists():
        raise CredentialError(f"私钥文件不存在：{expanded}")

    last_error: Exception | None = None
    for key_cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_cls.from_private_key_file(expanded, password=passphrase or None)
        except paramiko.PasswordRequiredException:
            raise CredentialError("私钥已加密，需要提供 passphrase") from None
        except Exception as exc:  # 类型不匹配就换下一种
            last_error = exc
    raise CredentialError(f"无法识别该私钥格式：{expanded}（{last_error}）")


def key_fingerprint(key) -> str:
    """返回 SHA256 指纹，用于连接前核对主机密钥。"""
    import hashlib

    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
