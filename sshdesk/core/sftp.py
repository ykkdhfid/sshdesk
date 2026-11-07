"""SFTP 文件操作：列目录、上传下载（含目录递归）、改名、删除、属性。"""

from __future__ import annotations

import os
import posixpath
import stat as stat_mod
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import paramiko

from .client import SSHConnection, SSHError

ProgressCallback = Callable[[int, int], None]


@dataclass
class RemoteEntry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    mode: int = 0
    is_link: bool = False

    @property
    def size_text(self) -> str:
        return human_size(self.size)

    @property
    def mtime_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.mtime)) if self.mtime else ""

    @property
    def perms(self) -> str:
        return stat_mod.filemode(self.mode) if self.mode else ""


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    units = ["KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        value /= 1024.0
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
    return f"{size} B"


class SFTPBrowser:
    """对 paramiko.SFTPClient 的一层薄封装，负责路径规范化与递归。"""

    def __init__(self, connection: SSHConnection) -> None:
        self.conn = connection
        self._sftp = connection.get_sftp()

    # ------------------------------------------------------------------ 浏览
    def listdir(self, path: str = ".") -> list[RemoteEntry]:
        try:
            attrs = self._sftp.listdir_attr(path)
        except (OSError, paramiko.SSHException) as exc:
            raise SSHError(f"读取目录失败：{path}", cause=str(exc)) from None
        entries: list[RemoteEntry] = []
        for a in attrs:
            entry_path = posixpath.join(path, a.filename)
            entries.append(
                RemoteEntry(
                    name=a.filename,
                    path=entry_path,
                    is_dir=stat_mod.S_ISDIR(a.st_mode or 0),
                    size=a.st_size or 0,
                    mtime=a.st_mtime or 0.0,
                    mode=a.st_mode or 0,
                    is_link=stat_mod.S_ISLNK(a.st_mode or 0),
                )
            )
        # 目录优先，再按名字排序，符合人类浏览习惯
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def home_dir(self) -> str:
        result = self.conn.exec_command("pwd")
        return result.stdout.strip() or "."

    def stat(self, path: str) -> RemoteEntry:
        try:
            a = self._sftp.stat(path)
        except (OSError, paramiko.SSHException) as exc:
            raise SSHError(f"读取属性失败：{path}", cause=str(exc)) from None
        return RemoteEntry(
            name=posixpath.basename(path.rstrip("/")) or "/",
            path=path,
            is_dir=stat_mod.S_ISDIR(a.st_mode or 0),
            size=a.st_size or 0,
            mtime=a.st_mtime or 0.0,
            mode=a.st_mode or 0,
        )

    # ------------------------------------------------------------------ 上传
    def upload(self, local_path: str, remote_path: str, progress: ProgressCallback | None = None) -> None:
        local = Path(local_path)
        if not local.exists():
            raise SSHError(f"本地文件不存在：{local_path}")
        if local.is_dir():
            self._upload_dir(local, remote_path, progress)
            return
        self._ensure_remote_dir(posixpath.dirname(remote_path) or ".")
        try:
            self._sftp.put(str(local), remote_path, callback=progress, confirm=True)
        except (OSError, paramiko.SSHException) as exc:
            raise SSHError(f"上传失败：{local.name} -> {remote_path}", cause=str(exc)) from None

    def _upload_dir(self, local_dir: Path, remote_dir: str,
                    progress: ProgressCallback | None = None) -> None:
        remote_dir = posixpath.join(remote_dir, local_dir.name) if posixpath.basename(
            remote_dir.rstrip("/")) != local_dir.name else remote_dir
        self._ensure_remote_dir(remote_dir)
        for item in local_dir.iterdir():
            if item.is_dir():
                self._upload_dir(item, remote_dir, progress)
            else:
                self._sftp.put(str(item), posixpath.join(remote_dir, item.name), callback=progress)

    def _ensure_remote_dir(self, remote_dir: str) -> None:
        """逐级创建远端目录；已存在则忽略。"""
        if remote_dir in ("", ".", "/"):
            return
        parts = remote_dir.strip("/").split("/")
        current = "/" if remote_dir.startswith("/") else ""
        for part in parts:
            current = posixpath.join(current, part) if current else part
            try:
                self._sftp.stat(current)
            except FileNotFoundError:
                try:
                    self._sftp.mkdir(current)
                except (OSError, paramiko.SSHException) as exc:
                    raise SSHError(f"创建远端目录失败：{current}", cause=str(exc)) from None
            except (OSError, paramiko.SSHException):
                pass

    # ------------------------------------------------------------------ 下载
    def download(self, remote_path: str, local_path: str,
                 progress: ProgressCallback | None = None) -> None:
        entry = self.stat(remote_path)
        local = Path(local_path)
        if entry.is_dir:
            self._download_dir(remote_path, local, progress)
            return
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._sftp.get(remote_path, str(local), callback=progress)
        except (OSError, paramiko.SSHException) as exc:
            raise SSHError(f"下载失败：{remote_path}", cause=str(exc)) from None

    def _download_dir(self, remote_dir: str, local_dir: Path,
                      progress: ProgressCallback | None = None) -> None:
        target = local_dir / posixpath.basename(remote_dir.rstrip("/"))
        target.mkdir(parents=True, exist_ok=True)
        for entry in self.listdir(remote_dir):
            if entry.name in (".", ".."):
                continue
            if entry.is_dir:
                self._download_dir(entry.path, target, progress)
            else:
                self._sftp.get(entry.path, str(target / entry.name), callback=progress)

    # ------------------------------------------------------------------ 变更
    def mkdir(self, path: str) -> None:
        try:
            self._sftp.mkdir(path)
        except (OSError, paramiko.SSHException) as exc:
            raise SSHError(f"创建目录失败：{path}", cause=str(exc)) from None

    def remove(self, path: str, recursive: bool = False) -> None:
        entry = self.stat(path)
        try:
            if entry.is_dir:
                if not recursive:
                    self._sftp.rmdir(path)
                else:
                    self._remove_tree(path)
            else:
                self._sftp.remove(path)
        except (OSError, paramiko.SSHException) as exc:
            raise SSHError(f"删除失败：{path}", cause=str(exc)) from None

    def _remove_tree(self, path: str) -> None:
        for entry in self.listdir(path):
            if entry.is_dir:
                self._remove_tree(entry.path)
            else:
                self._sftp.remove(entry.path)
        self._sftp.rmdir(path)

    def rename(self, old_path: str, new_path: str) -> None:
        try:
            self._sftp.posix_rename(old_path, new_path)
        except (OSError, paramiko.SSHException) as exc:
            raise SSHError(f"重命名失败：{old_path} -> {new_path}", cause=str(exc)) from None

    def chmod(self, path: str, mode: int) -> None:
        try:
            self._sftp.chmod(path, mode)
        except (OSError, paramiko.SSHException) as exc:
            raise SSHError(f"修改权限失败：{path}", cause=str(exc)) from None

    def close(self) -> None:
        try:
            self._sftp.close()
        except Exception:
            pass


def is_text_file(path: str, limit: int = 2 * 1024 * 1024) -> bool:
    """粗略判断本地文件是否适合用编辑器打开（按大小与扩展名）。"""
    text_suffix = {".txt", ".log", ".conf", ".py", ".sh", ".md", ".json", ".yaml", ".yml",
                   ".ini", ".html", ".css", ".js", ".csv", ".xml"}
    ext = os.path.splitext(path)[1].lower()
    return ext in text_suffix and os.path.getsize(path) <= limit
