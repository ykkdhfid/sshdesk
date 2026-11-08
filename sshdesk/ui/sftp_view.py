"""双栏文件浏览器：左侧本地、右侧远端，中间上下传。

网络 IO 全部放后台线程，UI 只做列表刷新，避免点一下卡三秒。
"""

from __future__ import annotations

import posixpath
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from ..core.client import build_connection, SSHError
from ..core.config import ServerConfig
from ..core.credentials import CredentialStore
from ..core.sftp import SFTPBrowser, human_size


class SftpView(ttk.Frame):
    def __init__(self, master, server: ServerConfig, credentials: CredentialStore,
                 on_status=None, demo: bool = False) -> None:
        super().__init__(master, style="TFrame", padding=12)
        self.server = server
        self.credentials = credentials
        self.on_status = on_status or (lambda *_: None)
        self.demo = demo
        self.conn = None
        self.browser: SFTPBrowser | None = None
        self.local_dir = Path.home()
        self.remote_dir = "."
        self._build()
        self.load_local()
        if not demo:
            self._connect_async()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(side="top", fill="x", pady=(0, 10))
        ttk.Label(bar, text="本地 → 远端", style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="上传选中", style="Accent.TButton",
                   command=self._upload_selected).pack(side="right", padx=(8, 0))
        ttk.Button(bar, text="刷新远端", style="TButton",
                   command=self.load_remote).pack(side="right")

        # 底部的路径行和操作按钮先 pack(side="bottom") 占位，
        # 这样窗口变矮时优先保住它们，不会被中间的文件列表挤到屏幕外。
        actions = ttk.Frame(self)
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(actions, text="下载选中", command=self._download_selected).pack(side="left")
        ttk.Button(actions, text="上级目录", command=self._go_up).pack(side="left", padx=6)
        ttk.Button(actions, text="新建目录", command=self._mkdir).pack(side="left")
        ttk.Button(actions, text="删除", command=self._delete).pack(side="left", padx=6)
        self.progress = ttk.Progressbar(actions, mode="determinate", length=180, maximum=100)
        self.progress.pack(side="right")

        self.local_path = tk.StringVar(value=str(self.local_dir))
        self.remote_path = tk.StringVar(value=self.remote_dir)
        ttk.Label(self, textvariable=self.remote_path, style="Muted.TLabel").pack(side="bottom", anchor="w")
        ttk.Label(self, textvariable=self.local_path, style="Muted.TLabel").pack(side="bottom", anchor="w", pady=(8, 0))

        panes = ttk.Frame(self)
        panes.pack(side="top", fill="both", expand=True)
        panes.columnconfigure(0, weight=1, uniform="pane")
        panes.columnconfigure(1, weight=1, uniform="pane")
        panes.rowconfigure(0, weight=1)

        self.local_tree = self._make_tree(panes, "本地文件")
        self.local_tree.master.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.remote_tree = self._make_tree(panes, "远端文件")
        self.remote_tree.master.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self.local_tree.bind("<Double-1>", lambda e: self._open_local())
        self.remote_tree.bind("<Double-1>", lambda e: self._open_remote())

    def _make_tree(self, parent, title: str) -> ttk.Treeview:
        wrapper = ttk.Frame(parent, style="Panel.TFrame")
        ttk.Label(wrapper, text=title, style="CardTitle.TLabel").pack(anchor="w", padx=8, pady=(8, 4))
        tree = ttk.Treeview(wrapper, columns=("size", "time"), show="tree headings", height=16)
        tree.heading("#0", text="名称")
        tree.heading("size", text="大小")
        tree.heading("time", text="修改时间")
        tree.column("#0", width=260)
        tree.column("size", width=90, anchor="e")
        tree.column("time", width=130, anchor="center")
        sb = ttk.Scrollbar(wrapper, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        sb.pack(side="right", fill="y", pady=(0, 8), padx=(0, 4))
        return tree

    # ------------------------------------------------------------------ 连接
    def _connect_async(self) -> None:
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self) -> None:
        try:
            conn = build_connection(self.server, self.credentials,
                                    on_unknown_host=lambda *_: True)
            conn.connect()
            self.conn = conn
            self.browser = SFTPBrowser(conn)
            self.remote_dir = self.browser.home_dir() or "/"
            self.after(0, self.load_remote)
            self.after(0, lambda: self.on_status("SFTP 已连接", "ok"))
        except Exception as exc:
            self.after(0, lambda err=exc: self.on_status(f"SFTP 连接失败：{err}", "error"))

    # ------------------------------------------------------------------ 列表
    def load_local(self) -> None:
        self.local_tree.delete(*self.local_tree.get_children())
        try:
            items = sorted(self.local_dir.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as exc:
            self.on_status(f"读取本地目录失败：{exc}", "error")
            return
        for item in items[:500]:
            try:
                stat = item.stat()
            except OSError:
                continue
            self.local_tree.insert(
                "", "end", text=("📁 " if item.is_dir() else "📄 ") + item.name,
                values=(human_size(stat.st_size) if not item.is_dir() else "—",
                        _fmt_time(stat.st_mtime))
            )
        self.local_path.set(str(self.local_dir))

    def load_remote(self) -> None:
        if not self.browser:
            return
        try:
            entries = self.browser.listdir(self.remote_dir)
        except SSHError as exc:
            self.on_status(str(exc), "error")
            return
        self.remote_tree.delete(*self.remote_tree.get_children())
        for entry in entries[:500]:
            self.remote_tree.insert(
                "", "end", text=("📁 " if entry.is_dir else "📄 ") + entry.name,
                values=(entry.size_text if not entry.is_dir else "—", entry.mtime_text),
                tags=("dir" if entry.is_dir else "file",),
            )
        self.remote_path.set(self.remote_dir)

    # ------------------------------------------------------------------ 交互
    def _open_local(self) -> None:
        selection = self.local_tree.selection()
        if not selection:
            return
        label = self.local_tree.item(selection[0], "text")
        path = self.local_dir / label.split(" ", 1)[-1]
        if path.is_dir():
            self.local_dir = path
            self.load_local()

    def _open_remote(self) -> None:
        selection = self.remote_tree.selection()
        if not selection:
            return
        name = self.remote_tree.item(selection[0], "text").split(" ", 1)[-1]
        target = posixpath.join(self.remote_dir, name)
        try:
            if self.browser and self.browser.stat(target).is_dir:
                self.remote_dir = target
                self.load_remote()
        except SSHError:
            pass

    def _selected_local(self) -> Path | None:
        selection = self.local_tree.selection()
        if not selection:
            return None
        name = self.local_tree.item(selection[0], "text").split(" ", 1)[-1]
        return self.local_dir / name

    def _selected_remote(self) -> str | None:
        selection = self.remote_tree.selection()
        if not selection:
            return None
        name = self.remote_tree.item(selection[0], "text").split(" ", 1)[-1]
        return posixpath.join(self.remote_dir, name)

    def _upload_selected(self) -> None:
        if not self.browser:
            self.on_status("尚未连接远端，无法上传", "error")
            return
        path = self._selected_local()
        if path is None:
            self.on_status("先在左侧选中要上传的文件", "warn")
            return
        remote = posixpath.join(self.remote_dir, path.name)
        self._run_transfer(lambda: self.browser.upload(str(path), remote, self._progress_cb),
                           f"上传完成：{path.name}")

    def _download_selected(self) -> None:
        if not self.browser:
            self.on_status("尚未连接远端，无法下载", "error")
            return
        remote = self._selected_remote()
        if remote is None:
            self.on_status("先在右侧选中要下载的文件", "warn")
            return
        self._run_transfer(lambda: self.browser.download(remote, str(self.local_dir / posixpath.basename(remote)),
                                                         self._progress_cb),
                           f"下载完成：{posixpath.basename(remote)}")

    def _progress_cb(self, transferred: int, total: int) -> None:
        percent = (transferred / total * 100) if total else 0
        self.after(0, lambda: self.progress.configure(value=percent))

    def _run_transfer(self, task, success_message: str) -> None:
        def worker() -> None:
            try:
                task()
                self.after(0, lambda: self.progress.configure(value=0))
                self.after(0, self.load_local)
                self.after(0, self.load_remote)
                self.after(0, lambda: self.on_status(success_message, "ok"))
            except Exception as exc:
                self.after(0, lambda err=exc: self.on_status(f"传输失败：{err}", "error"))

        threading.Thread(target=worker, daemon=True).start()

    def _go_up(self) -> None:
        # 本地和远端各自退一层：原来只动远端，本地停在原地看着像"点了没反应"。
        if self.local_dir.parent != self.local_dir:
            self.local_dir = self.local_dir.parent
            self.load_local()
        if self.browser:
            self.remote_dir = posixpath.dirname(self.remote_dir.rstrip("/")) or "/"
            self.load_remote()

    def _mkdir(self) -> None:
        if not self.browser:
            return
        from tkinter import simpledialog

        name = simpledialog.askstring("新建目录", "目录名：", parent=self)
        if name:
            try:
                self.browser.mkdir(posixpath.join(self.remote_dir, name))
                self.load_remote()
            except SSHError as exc:
                self.on_status(str(exc), "error")

    def _delete(self) -> None:
        if not self.browser:
            return
        remote = self._selected_remote()
        if remote is None:
            return
        if not messagebox.askyesno("确认删除", f"确定删除 {remote}？目录会递归删除。"):
            return
        try:
            self.browser.remove(remote, recursive=True)
            self.load_remote()
            self.on_status(f"已删除 {remote}", "ok")
        except SSHError as exc:
            self.on_status(str(exc), "error")

    def close(self) -> None:
        if self.conn:
            self.conn.close()


def _fmt_time(ts: float) -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
