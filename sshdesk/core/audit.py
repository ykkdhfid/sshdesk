"""操作审计日志（JSONL）。

远程运维的风险在于「谁在什么时候对哪台机器做了什么」。GUI 和 CLI 的每次
连接、命令执行、文件上传都会落一条记录，便于事后回溯。日志里禁止出现明文
口令，写入前统一脱敏。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import app_dir

# password=xxx / passwd=xxx / -p xxx / Bearer xxx / 私钥内容
_SECRET_PATTERNS = [
    (re.compile(r"(password|passwd|pwd)\s*=\s*[^\s&;'\"]+", re.I), r"\1=***"),
    (re.compile(r"(--password|--passwd)\s+[^\s]+", re.I), r"\1 ***"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.I), "Bearer ***"),
    (re.compile(r"BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY[\s\S]*?END (RSA |OPENSSH |EC )?PRIVATE KEY", re.I),
     "[PRIVATE KEY REDACTED]"),
]


def redact(text: str) -> str:
    result = text
    for pattern, repl in _SECRET_PATTERNS:
        result = pattern.sub(repl, result)
    return result


@dataclass
class AuditLogger:
    path: Path = field(default_factory=lambda: app_dir() / "audit.log")
    enabled: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def log(self, action: str, target: str = "", detail: str = "", ok: bool = True, **extra) -> None:
        if not self.enabled:
            return
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": action,
            "target": target,
            "ok": ok,
            "detail": redact(detail)[:2000],
            **extra,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fp:
                    fp.write(line + "\n")
                os.chmod(self.path, 0o600)
            except OSError:
                pass  # 审计失败不阻塞主流程

    def tail(self, limit: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        out: list[dict] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def read_log(path: Path | None = None, limit: int = 200) -> list[dict]:
    return AuditLogger(path or app_dir() / "audit.log").tail(limit)
