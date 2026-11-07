"""新增 / 编辑服务器对话框。"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from ..core.config import ServerConfig
from ..core.credentials import CredentialStore
from .theme import BG, FG, FG_MUTED, RED


class ServerFormDialog(tk.Toplevel):
    def __init__(self, master, credentials: CredentialStore, server: ServerConfig | None = None) -> None:
        super().__init__(master)
        self.credentials = credentials
        self.server = server
        self.result: ServerConfig | None = None
        self.title("编辑服务器" if server else "添加服务器")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self._build()
        self._center(master)

    # ------------------------------------------------------------------ 布局
    def _build(self) -> None:
        body = ttk.Frame(self, style="TFrame", padding=18)
        body.pack(fill="both", expand=True)

        self.vars = {
            "name": tk.StringVar(value=self.server.name if self.server else ""),
            "host": tk.StringVar(value=self.server.host if self.server else ""),
            "port": tk.StringVar(value=str(self.server.port if self.server else 22)),
            "username": tk.StringVar(value=self.server.username if self.server else "root"),
            "auth": tk.StringVar(value=self.server.auth_type if self.server else "key"),
            "key_path": tk.StringVar(value=self.server.key_path if self.server else
                                     str(Path.home() / ".ssh" / "id_ed25519")),
            "group": tk.StringVar(value=self.server.group if self.server else "默认"),
            "tags": tk.StringVar(value=",".join(self.server.tags) if self.server else ""),
            "jump": tk.StringVar(value=self.server.jump_host if self.server else ""),
            "note": tk.StringVar(value=self.server.note if self.server else ""),
            "password": tk.StringVar(),
        }
        self.save_password = tk.BooleanVar(value=False)

        rows = [
            ("名称", "name", "给这台机器起个认得出的名字，如 阿里云成都"),
            ("主机", "host", "公网 IP 或域名；只有私网 IP 时请先看诊断建议"),
            ("端口", "port", "默认 22"),
            ("用户名", "username", "Ubuntu 镜像通常是 root 或 ubuntu"),
        ]
        for idx, (label, key, hint) in enumerate(rows):
            ttk.Label(body, text=label, style="Muted.TLabel", width=8).grid(
                row=idx, column=0, sticky="w", pady=4)
            entry = ttk.Entry(body, textvariable=self.vars[key], width=38)
            entry.grid(row=idx, column=1, sticky="we", pady=4)
            ttk.Label(body, text=hint, style="Muted.TLabel").grid(
                row=idx, column=2, sticky="w", padx=(10, 0))

        ttk.Label(body, text="认证", style="Muted.TLabel", width=8).grid(row=4, column=0, sticky="w", pady=4)
        combo = ttk.Combobox(body, textvariable=self.vars["auth"], state="readonly", width=12,
                             values=("key", "password", "agent"))
        combo.grid(row=4, column=1, sticky="w", pady=4)
        combo.bind("<<ComboboxSelected>>", lambda e: self._sync_auth_ui())

        ttk.Label(body, text="私钥", style="Muted.TLabel", width=8).grid(row=5, column=0, sticky="w", pady=4)
        self.key_frame = ttk.Frame(body, style="TFrame")
        self.key_frame.grid(row=5, column=1, columnspan=2, sticky="we", pady=4)
        ttk.Entry(self.key_frame, textvariable=self.vars["key_path"], width=32).pack(side="left", fill="x", expand=True)
        ttk.Button(self.key_frame, text="浏览", style="Ghost.TButton",
                   command=self._browse_key).pack(side="left", padx=(6, 0))

        self.pw_label = ttk.Label(body, text="密码", style="Muted.TLabel", width=8)
        self.pw_label.grid(row=6, column=0, sticky="w", pady=4)
        self.pw_frame = ttk.Frame(body, style="TFrame")
        self.pw_frame.grid(row=6, column=1, columnspan=2, sticky="we", pady=4)
        ttk.Entry(self.pw_frame, textvariable=self.vars["password"], width=32, show="•").pack(
            side="left", fill="x", expand=True)
        ttk.Checkbutton(self.pw_frame, text="记住（加密存储）",
                        variable=self.save_password).pack(side="left", padx=(8, 0))

        ttk.Label(body, text="分组", style="Muted.TLabel", width=8).grid(row=7, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.vars["group"], width=38).grid(row=7, column=1, sticky="we", pady=4)
        ttk.Label(body, text="用于批量执行时按组筛选", style="Muted.TLabel").grid(
            row=7, column=2, sticky="w", padx=(10, 0))

        ttk.Label(body, text="跳板机", style="Muted.TLabel", width=8).grid(row=8, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.vars["jump"], width=38).grid(row=8, column=1, sticky="we", pady=4)
        ttk.Label(body, text="可留空；格式 user@host:port", style="Muted.TLabel").grid(
            row=8, column=2, sticky="w", padx=(10, 0))

        ttk.Label(body, text="标签", style="Muted.TLabel", width=8).grid(row=9, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.vars["tags"], width=38).grid(row=9, column=1, sticky="we", pady=4)
        ttk.Label(body, text="逗号分隔，如 生产,nginx", style="Muted.TLabel").grid(
            row=9, column=2, sticky="w", padx=(10, 0))

        ttk.Label(body, text="备注", style="Muted.TLabel", width=8).grid(row=10, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.vars["note"], width=38).grid(row=10, column=1, sticky="we", pady=4)

        self.hint = ttk.Label(body, text="", style="Muted.TLabel", foreground=RED, wraplength=420)
        self.hint.grid(row=11, column=0, columnspan=3, sticky="w", pady=(10, 0))

        footer = ttk.Frame(body, style="TFrame")
        footer.grid(row=12, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(footer, text="取消", style="Ghost.TButton", command=self.destroy).pack(side="right")
        ttk.Button(footer, text="保存", style="Accent.TButton", command=self._save).pack(
            side="right", padx=(0, 8))

        self._sync_auth_ui()

    def _sync_auth_ui(self) -> None:
        auth = self.vars["auth"].get()
        state_key = "normal" if auth == "key" else "disabled"
        for child in self.key_frame.winfo_children():
            child.configure(state=state_key)
        self.pw_label.configure(foreground=FG if auth == "password" else FG_MUTED)
        for child in self.pw_frame.winfo_children():
            try:
                child.configure(state="normal" if auth == "password" else "disabled")
            except tk.TclError:
                pass
        if auth == "agent":
            self.hint.configure(text="将使用本机 ssh-agent 中已加载的密钥", foreground=FG_MUTED)
        else:
            self.hint.configure(text="")

    def _browse_key(self) -> None:
        path = filedialog.askopenfilename(
            title="选择私钥",
            initialdir=str(Path.home() / ".ssh"),
            filetypes=[("私钥", "*"), ("所有文件", "*.*")],
        )
        if path:
            self.vars["key_path"].set(path)

    def _center(self, master) -> None:
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + max(0, (master.winfo_height() - h) // 2)
        self.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------ 保存
    def _save(self) -> None:
        try:
            port = int(self.vars["port"].get().strip())
        except ValueError:
            self.hint.configure(text="端口必须是数字")
            return
        tags = [t.strip() for t in self.vars["tags"].get().split(",") if t.strip()]
        server = ServerConfig(
            name=self.vars["name"].get().strip(),
            host=self.vars["host"].get().strip(),
            username=self.vars["username"].get().strip(),
            port=port,
            auth_type=self.vars["auth"].get(),
            key_path=self.vars["key_path"].get().strip(),
            group=self.vars["group"].get().strip() or "默认",
            tags=tags,
            jump_host=self.vars["jump"].get().strip(),
            note=self.vars["note"].get().strip(),
        )
        if self.server:
            server.id = self.server.id
            server.created_at = self.server.created_at
            server.last_connected_at = self.server.last_connected_at
            server.password_ref = self.server.password_ref

        try:
            server.validate()
        except ValueError as exc:
            self.hint.configure(text=str(exc))
            return

        password = self.vars["password"].get()
        if server.auth_type == "password" and password and self.save_password.get():
            if not server.password_ref:
                server.password_ref = f"pw-{server.id}"
            self.credentials.set_password(server.password_ref, password)
        elif server.auth_type == "password" and password and not self.save_password.get():
            # 不落盘：仅本次会话使用，由调用方通过内存传递
            self.session_password = password

        self.result = server
        self.destroy()
