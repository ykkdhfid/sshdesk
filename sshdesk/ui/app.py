"""主窗口：服务器列表 + 连接会话 + 诊断 / 批量执行入口。"""

from __future__ import annotations

import math
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from ..core.audit import AuditLogger
from ..core.client import SSHConnection, SSHError, build_connection
from ..core.config import ConfigStore, ServerConfig
from ..core.credentials import CredentialStore
from ..core.diagnostics import diagnose, remote_selfcheck
from ..core.executor import format_table, run_batch
from ..core.terminal_screen import ScreenBuffer, char_width
from .monitor_view import MonitorView
from .server_form import ServerFormDialog
from .sftp_view import SftpView
from .terminal_view import TERM_BG, TERM_FG, TerminalView
from .theme import (ACCENT, BG, BORDER, FG, FG_MUTED, GREEN, PANEL, PANEL_ALT, RED,
                    YELLOW, apply_theme, mono_font, ui_font)

DEMO_SCREEN = (
    "\x1b[2J\x1b[H"
    "\x1b[38;5;114m╭──────────────────────────────────────────────────────────────╮\x1b[0m\r\n"
    "\x1b[38;5;114m│\x1b[0m  \x1b[1mSSHDesk\x1b[0m · 本地桌面 SSH 运维客户端        \x1b[38;5;114m│\x1b[0m\r\n"
    "\x1b[38;5;114m│\x1b[0m  Ubuntu 22.04.4 LTS · 2 vCPU · 2 GiB · 成都       \x1b[38;5;114m│\x1b[0m\r\n"
    "\x1b[38;5;114m╰──────────────────────────────────────────────────────────────╯\x1b[0m\r\n\r\n"
    "\x1b[38;5;244mLast login: Sat Nov  8 09:12:41 2026 from 172.29.79.1\x1b[0m\r\n"
    "root@iZbam4b688h2pd:~# \x1b[1mneofetch\x1b[0m\r\n"
    "  \x1b[38;5;39mOS\x1b[0m      Ubuntu 22.04.4 LTS x86_64\r\n"
    "  \x1b[38;5;39mHost\x1b[0m    Alibaba Cloud ecs.e-c1m1.large\r\n"
    "  \x1b[38;5;39mKernel\x1b[0m  5.15.0-119-generic\r\n"
    "  \x1b[38;5;39mCPU\x1b[0m     Intel Xeon Platinum (2) @ 2.5GHz\r\n"
    "  \x1b[38;5;39mMemory\x1b[0m  612MiB / 1944MiB\r\n"
    "  \x1b[38;5;39mDisk\x1b[0m    7.8G / 39.3G (21%)\r\n\r\n"
    "root@iZbam4b688h2pd:~# \x1b[1msystemctl status ssh --no-pager\x1b[0m\r\n"
    "\x1b[32m●\x1b[0m ssh.service - OpenBSD Secure Shell server\r\n"
    "     Loaded: loaded (/lib/systemd/system/ssh.service; enabled)\r\n"
    "     \x1b[32mActive: active (running)\x1b[0m since Sat 2026-11-08 07:30:11 CST\r\n"
    "     Docs: man:sshd(8)\r\n\r\n"
    "root@iZbam4b688h2pd:~# \x1b[1mtop -bn1 | head -5\x1b[0m\r\n"
    "top - 09:15:02 up 13:23,  1 user,  load average: 0.08, 0.12, 0.09\r\n"
    "Tasks:  98 total,   1 running,  97 sleeping\r\n"
    "\x1b[38;5;203m%Cpu(s):  1.3 us,\x1b[0m  0.7 sy,  0.0 ni, \x1b[38;5;114m97.8 id\x1b[0m\r\n\r\n"
    "root@iZbam4b688h2pd:~# \x1b[7m \x1b[0m\r\n"
)


# 空态背景：深空 + 40px 字符网格 + 1px 硬星点。
# 桌面端不能像 CSS 那样叠背景图，只能在 Canvas 上把这些画出来；
# 位置与 Web 端保持同一套百分比，两边看起来才是同一个世界。
STARS = [
    (8, 13, .80), (31, 18, .44), (58, 11, .62), (90, 22, .74),
    (46, 34, .34), (18, 64, .32),
    (3, 45, .46), (96, 53, .48),
    (10, 87, .70), (43, 96, .42), (77, 90, .78), (93, 97, .54),
]
_GRID = "#0b0f17"


def _star_color(o: float) -> str:
    base, top = (7, 10, 18), (214, 228, 255)
    r, g, b = (int(a + (b_ - a) * o) for a, b_ in zip(base, top))
    return f"#{r:02x}{g:02x}{b:02x}"


def paint_starscape(canvas: tk.Canvas, title: str = "", body: str = "",
                    phase: float = 0.0) -> None:
    canvas.delete("all")
    w, h = canvas.winfo_width(), canvas.winfo_height()
    if w < 2 or h < 2:
        return
    for x in range(0, w, 40):
        canvas.create_line(x, 0, x, h, fill=_GRID)
    for y in range(0, h, 40):
        canvas.create_line(0, y, w, y, fill=_GRID)
    for i, (px, py, o) in enumerate(STARS):
        # 每颗的相位错开 1.7 弧度，周期约 5 秒：一颗接一颗地闪，不是整片一起呼吸
        glow = 0.18 + 0.82 * (0.5 + 0.5 * math.sin(phase * 1.2 + i * 1.7)) ** 2
        x, y = int(w * px / 100), int(h * py / 100)
        if 0 <= x < w - 1 and 0 <= y < h - 1:
            c = _star_color(o * glow)
            canvas.create_oval(x, y, x + 1, y + 1, fill=c, outline=c)
    if title:
        canvas.create_text(w // 2, h // 2 - 44, text=title, fill=FG,
                           font=ui_font(16, "bold"))
    if body:
        canvas.create_text(w // 2, h // 2 + 6, text=body, fill=FG_MUTED,
                           font=ui_font(10), justify="center", width=max(240, w - 120))



class App(tk.Tk):
    def __init__(self, demo: bool = False) -> None:
        super().__init__()
        self.title("SSHDesk · 远程服务器工作台")
        self.geometry("1200x780")
        self.minsize(980, 640)
        apply_theme(self)

        self.demo = demo
        self.store = ConfigStore()
        self.credentials = CredentialStore()
        self.audit = AuditLogger()
        self.sessions: dict[str, dict] = {}
        self._closing = False

        self._build_layout()
        self._refresh_servers()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if demo:
            self._open_demo()

    # -------------------------------------------------------------- 星空动画
    def _tick_stars(self, phase: float = 0.0) -> None:
        """空态页的星空每 120ms 重画一帧。Tk 的 Canvas 没有透明度，
        所以用颜色插值模拟闪烁——亮度低时点会沉回底色，看起来就是暗了。"""
        if not self.empty_canvas.winfo_exists():
            return
        if self._empty_visible:
            paint_starscape(self.empty_canvas, "还没有打开的会话",
                            "在左侧选中一台服务器，点「连接」或直接双击。\n"
                            "首次连接会核对主机指纹；连不上时点「连接诊断」按步骤排查。",
                            phase)
        self._star_job = self.after(120, lambda: self._tick_stars(phase + 0.12))

    # ------------------------------------------------------------------ 布局
    def _build_layout(self) -> None:
        header = tk.Frame(self, bg=PANEL, height=56)
        header.pack(fill="x")
        header.pack_propagate(False)
        # 产品名用等宽字：这套界面的第一句话就该是"这是个文本系统"
        tk.Label(header, text="SSHDesk", bg=PANEL, fg=ACCENT,
                 font=mono_font(15, "bold")).pack(side="left", padx=(18, 8))
        tk.Label(header, text="本地桌面 SSH 运维客户端", bg=PANEL, fg=FG_MUTED,
                 font=ui_font(9)).pack(side="left", pady=(4, 0))
        ttk.Button(header, text="批量执行", style="Ghost.TButton",
                   command=self._open_batch).pack(side="right", padx=(0, 14), pady=12)
        ttk.Button(header, text="连接诊断", style="Ghost.TButton",
                   command=self._open_diagnose).pack(side="right", padx=(0, 8), pady=12)

        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True)

        sidebar = tk.Frame(main, bg=PANEL, width=272)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)

        self.notebook = ttk.Notebook(main)
        self.notebook.pack(side="left", fill="both", expand=True, padx=12, pady=12)

        self.empty_state = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.empty_state, text="   ")
        self.empty_canvas = tk.Canvas(self.empty_state, bg=BG, highlightthickness=0, bd=0)
        self.empty_canvas.pack(fill="both", expand=True)
        self._empty_visible = True
        self._star_job = None
        self.empty_canvas.bind("<Configure>", lambda _e: self._tick_stars(0.0))
        self.after(300, self._tick_stars)
        # ttk.Notebook 只有 hide()，没有 show()，所以空状态页用 add/forget 动态管理

        status_bar = tk.Frame(self, bg=PANEL, height=30)
        status_bar.pack(fill="x", side="bottom")
        status_bar.pack_propagate(False)
        self.status_dot = tk.Label(status_bar, text="●", bg=PANEL, fg=FG_MUTED)
        self.status_dot.pack(side="left", padx=(16, 4))
        self.status_text = tk.Label(status_bar, text="就绪", bg=PANEL, fg=FG_MUTED,
                                    font=ui_font(9))
        self.status_text.pack(side="left")
        self.count_text = tk.Label(status_bar, text="", bg=PANEL, fg=FG_MUTED,
                                   font=ui_font(9))
        self.count_text.pack(side="right", padx=16)

    def _build_sidebar(self, sidebar: tk.Frame) -> None:
        top = tk.Frame(sidebar, bg=PANEL)
        top.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(top, text="服务器", bg=PANEL, fg=FG,
                 font=ui_font(10, "bold")).pack(side="left")
        ttk.Button(top, text="+ 新增", style="Ghost.TButton",
                   command=self._add_server).pack(side="right")

        search = tk.Frame(sidebar, bg=PANEL_ALT, highlightthickness=1,
                          highlightbackground=BORDER)
        search.pack(fill="x", padx=14, pady=(0, 8))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refresh_servers())
        entry = tk.Entry(search, textvariable=self.search_var, bg=PANEL_ALT, fg=FG,
                         insertbackground=FG, relief="flat", font=ui_font(9))
        entry.pack(fill="x", padx=8, pady=6)
        entry.insert(0, "")
        tk.Label(search, text="搜索名称 / 地址 / 分组", bg=PANEL_ALT, fg=FG_MUTED,
                 font=ui_font(8)).pack(anchor="w", padx=8, pady=(0, 6))

        tree_wrap = tk.Frame(sidebar, bg=PANEL)
        tree_wrap.pack(fill="both", expand=True, padx=14)
        self.server_tree = ttk.Treeview(tree_wrap, columns=("addr",), show="tree headings",
                                        height=16, selectmode="browse")
        self.server_tree.heading("#0", text="名称")
        self.server_tree.heading("addr", text="地址")
        self.server_tree.column("#0", width=118, anchor="w")
        self.server_tree.column("addr", width=112, anchor="w")
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.server_tree.yview)
        self.server_tree.configure(yscrollcommand=sb.set)
        self.server_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.server_tree.bind("<Double-1>", lambda e: self._connect_selected())
        self.server_tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        btns = tk.Frame(sidebar, bg=PANEL)
        btns.pack(fill="x", padx=14, pady=12)
        self.connect_btn = ttk.Button(btns, text="连接", style="Accent.TButton",
                                     command=self._connect_selected)
        self.connect_btn.pack(fill="x")
        row = tk.Frame(btns, bg=PANEL)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="编辑", style="TButton", command=self._edit_selected).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="删除", style="TButton", command=self._delete_selected).pack(
            side="left", fill="x", expand=True, padx=(8, 0))

    # ------------------------------------------------------------------ 列表
    def _filtered_servers(self) -> list[ServerConfig]:
        keyword = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
        servers = self.store.all()
        if not keyword:
            return servers
        return [s for s in servers
                if keyword in s.name.lower() or keyword in s.host.lower()
                or keyword in s.group.lower()]

    def _refresh_servers(self) -> None:
        if not hasattr(self, "server_tree"):
            return
        self.server_tree.delete(*self.server_tree.get_children())
        for server in self._filtered_servers():
            state = "🟢 " if server.id in self.sessions else "⚪ "
            self.server_tree.insert("", "end", iid=server.id,
                                    text=state + server.name,
                                    values=(f"{server.host}:{server.port}",))
        self.count_text.configure(text=f"共 {len(self.store.all())} 台服务器")

    def _selected_server(self) -> ServerConfig | None:
        selection = self.server_tree.selection()
        if not selection:
            return None
        return self.store.get(selection[0])

    def _on_select(self) -> None:
        server = self._selected_server()
        if server:
            self.set_status(f"{server.name} · {server.endpoint} · {server.auth_type} 认证", "info")

    # ------------------------------------------------------------------ 增删改
    def _add_server(self) -> None:
        dialog = ServerFormDialog(self, self.credentials)
        self.wait_window(dialog)
        if dialog.result:
            self.store.upsert(dialog.result)
            self.audit.log("add_server", dialog.result.endpoint)
            self._refresh_servers()
            self.set_status(f"已添加 {dialog.result.name}", "ok")

    def _edit_selected(self) -> None:
        server = self._selected_server()
        if not server:
            self.set_status("先在左侧选中一台服务器", "warn")
            return
        dialog = ServerFormDialog(self, self.credentials, server)
        self.wait_window(dialog)
        if dialog.result:
            self.store.upsert(dialog.result)
            self._refresh_servers()
            self.set_status(f"已更新 {dialog.result.name}", "ok")

    def _delete_selected(self) -> None:
        server = self._selected_server()
        if not server:
            self.set_status("先在左侧选中一台服务器", "warn")
            return
        if not messagebox.askyesno("删除服务器", f"确定删除「{server.name}」？本地保存的密码也会一并清除。"):
            return
        self._close_session(server.id)
        self.store.delete(server.id)
        if server.password_ref:
            self.credentials.delete_password(server.password_ref)
        self._refresh_servers()
        self.set_status(f"已删除 {server.name}", "ok")

    # ------------------------------------------------------------------ 连接
    def _connect_selected(self) -> None:
        server = self._selected_server()
        if not server:
            self.set_status("先在左侧选中一台服务器", "warn")
            return
        self._connect(server)

    def _connect(self, server: ServerConfig) -> None:
        if server.id in self.sessions:
            self._focus_session(server.id)
            return

        password = self._obtain_password(server)
        if server.auth_type == "password" and password is None:
            self.set_status("未提供密码，已取消连接", "warn")
            return

        self.set_status(f"正在连接 {server.endpoint} …", "info")
        self.status_dot.configure(fg=YELLOW)
        self.audit.log("connect", server.endpoint)
        threading.Thread(target=self._connect_worker, args=(server, password), daemon=True).start()

    def _obtain_password(self, server: ServerConfig) -> str | None:
        if server.auth_type != "password":
            return None
        if server.password_ref and self.credentials.has_password(server.password_ref):
            return self.credentials.get_password(server.password_ref)
        return simpledialog.askstring(
            "输入密码", f"{server.username}@{server.host} 的登录密码：",
            show="•", parent=self,
        )

    def _connect_worker(self, server: ServerConfig, password: str | None) -> None:
        connection = build_connection(server, self.credentials, self._ask_host_key_threadsafe)
        try:
            connection.connect(password=password)
        except SSHError as exc:
            # 用默认参数把异常对象绑进闭包：except 块结束后 exc 会被删除
            self.after(0, lambda err=exc: self._on_connect_error(server, err))
            return
        self.after(0, lambda: self._on_connected(server, connection))

    def _ask_host_key_threadsafe(self, hostname: str, key_type: str, fingerprint: str) -> bool:
        """工作线程里弹出确认框：用 after 切回 UI 线程，再用 Event 等待结果。"""
        event = threading.Event()
        answer = {"ok": False}

        def ask() -> None:
            answer["ok"] = messagebox.askyesno(
                "确认主机指纹",
                f"首次连接 {hostname}\n\n算法：{key_type}\n指纹：{fingerprint}\n\n"
                "指纹只会在服务器重装或换密钥时变化。确认与你预期一致后再信任。",
                parent=self,
            )
            event.set()

        self.after(0, ask)
        if not event.wait(timeout=120):
            return False
        return bool(answer["ok"])

    def _on_connect_error(self, server: ServerConfig, exc: SSHError) -> None:
        self.set_status(f"连接失败：{exc.message}", "error")
        self.status_dot.configure(fg=RED)
        detail = f"{exc.message}\n\n{exc.hint}" if exc.hint else exc.message
        if messagebox.askyesno("连接失败", f"{detail}\n\n要现在运行网络诊断吗？", parent=self):
            self._show_diagnose(server)

    def _on_connected(self, server: ServerConfig, connection: SSHConnection) -> None:
        self.store.touch_connected(server.id)
        self.set_status(f"已连接 {server.name}", "ok")
        self.status_dot.configure(fg=GREEN)
        self.audit.log("connected", server.endpoint)

        holder = ttk.Frame(self.notebook, style="TFrame")
        tabs = ttk.Notebook(holder)
        tabs.pack(fill="both", expand=True, padx=2, pady=2)

        terminal = TerminalView(tabs, connection, on_status=self.set_status,
                                on_title=lambda title: self._set_tab_title(holder, server, title))
        tabs.add(terminal, text="终端")
        monitor = MonitorView(tabs, server, self.credentials, on_status=self.set_status)
        tabs.add(monitor, text="监控")
        sftp = SftpView(tabs, server, self.credentials, on_status=self.set_status)
        tabs.add(sftp, text="文件")

        self.notebook.add(holder, text=server.name)
        self._hide_empty()
        self.sessions[server.id] = {
            "connection": connection, "frame": holder, "tabs": tabs,
            "terminal": terminal, "monitor": monitor, "sftp": sftp,
        }
        self._refresh_servers()
        self.notebook.select(holder)

    def _hide_empty(self) -> None:
        if getattr(self, "_empty_visible", False):
            try:
                self.notebook.forget(self.empty_state)
            except tk.TclError:
                pass
            self._empty_visible = False

    def _show_empty(self) -> None:
        if not getattr(self, "_empty_visible", False):
            self.notebook.add(self.empty_state, text="   ", sticky="nsew")
            self.notebook.select(self.empty_state)
            self._empty_visible = True

    def _set_tab_title(self, frame, server: ServerConfig, title: str) -> None:
        clean = title.strip()[:24]
        if clean:
            try:
                self.notebook.tab(frame, text=f"{clean}")
            except tk.TclError:
                pass

    def _focus_session(self, server_id: str) -> None:
        session = self.sessions.get(server_id)
        if session:
            self.notebook.select(session["frame"])

    def _close_session(self, server_id: str) -> None:
        session = self.sessions.pop(server_id, None)
        if not session:
            return
        for view in (session["terminal"], session["monitor"], session["sftp"]):
            try:
                view.close()
            except Exception:
                pass
        try:
            session["connection"].close()
        except Exception:
            pass
        try:
            self.notebook.forget(session["frame"])
        except tk.TclError:
            pass
        if not self.sessions:
            self._show_empty()

    # ------------------------------------------------------------------ 诊断
    def _open_diagnose(self) -> None:
        server = self._selected_server()
        if not server:
            self.set_status("先在左侧选中一台服务器", "warn")
            return
        self._show_diagnose(server)

    def _show_diagnose(self, server: ServerConfig) -> None:
        window = tk.Toplevel(self)
        window.title(f"连接诊断 · {server.name}")
        window.configure(bg=BG)
        window.geometry("760x520")
        text = tk.Text(window, bg=TERM_BG, fg=TERM_FG, font=mono_font(10),
                       relief="flat", padx=14, pady=12, wrap="word")
        text.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        text.insert("end", f"正在诊断 {server.host}:{server.port} …\n")
        footer = ttk.Frame(window, style="TFrame")
        footer.pack(fill="x", padx=12, pady=10)
        ttk.Button(footer, text="关闭", style="Ghost.TButton", command=window.destroy).pack(side="right")

        def worker() -> None:
            report = diagnose(server)
            content = report.to_text()
            if server.id in self.sessions:
                conn = self.sessions[server.id]["connection"]
                try:
                    content += "\n\n" + remote_selfcheck(conn, server.port).to_text()
                except Exception as exc:
                    content += f"\n\n服务器体检失败：{exc}"
            self.after(0, lambda: (text.delete("1.0", "end"), text.insert("end", content)))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ 批量
    def _open_batch(self) -> None:
        window = tk.Toplevel(self)
        window.title("批量执行命令")
        window.configure(bg=BG)
        window.geometry("860x560")
        window.transient(self)

        top = ttk.Frame(window, style="TFrame", padding=(14, 12))
        top.pack(fill="x")
        ttk.Label(top, text="范围", style="Muted.TLabel").pack(side="left")
        groups = sorted({s.group for s in self.store.all()}) or ["默认"]
        scope = tk.StringVar(value="全部")
        combo = ttk.Combobox(top, textvariable=scope, state="readonly", width=14,
                            values=["全部"] + groups)
        combo.pack(side="left", padx=(6, 14))
        ttk.Label(top, text="命令", style="Muted.TLabel").pack(side="left")
        command = tk.StringVar(value="uptime && df -h /")
        entry = ttk.Entry(top, textvariable=command)
        entry.pack(side="left", fill="x", expand=True, padx=(6, 10))
        output = tk.Text(window, bg=TERM_BG, fg=TERM_FG, font=mono_font(10),
                         relief="flat", padx=12, pady=10, wrap="none")
        output.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        footer = ttk.Frame(window, style="TFrame", padding=(14, 0, 14, 12))
        footer.pack(fill="x")

        def run() -> None:
            targets = self.store.all() if scope.get() == "全部" else [
                s for s in self.store.all() if s.group == scope.get()]
            if not targets:
                output.insert("end", "没有匹配的服务器\n")
                return
            output.delete("1.0", "end")
            output.insert("end", f"正在 {len(targets)} 台服务器上执行：{command.get()}\n\n")
            run_btn.configure(state="disabled")

            def worker() -> None:
                results = run_batch(targets, command.get(), credentials=self.credentials)
                table = format_table(results)
                details = "\n\n" + "\n\n".join(
                    f"=== {r.label} ===\n{r.stdout.strip() or r.stderr.strip() or r.error}"
                    for r in results)
                self.after(0, lambda: (output.insert("end", table + details),
                                       run_btn.configure(state="normal")))
                self.audit.log("batch_exec", scope.get(), command.get())

            threading.Thread(target=worker, daemon=True).start()

        run_btn = ttk.Button(footer, text="执行", style="Accent.TButton", command=run)
        run_btn.pack(side="right")
        ttk.Button(footer, text="关闭", style="Ghost.TButton",
                   command=window.destroy).pack(side="right", padx=(0, 8))
        entry.focus_set()

    # ------------------------------------------------------------------ 演示
    def _open_demo(self) -> None:
        """不连真实服务器，用内置样例展示界面（也用于自动化截图验证）。"""
        holder = ttk.Frame(self.notebook, style="TFrame")
        tabs = ttk.Notebook(holder)
        tabs.pack(fill="both", expand=True, padx=2, pady=2)

        term = ttk.Frame(tabs, style="TFrame")
        text = tk.Text(term, bg=TERM_BG, fg=TERM_FG, font=mono_font(11), wrap="none",
                       relief="flat", padx=8, pady=6, highlightthickness=0)
        text.pack(fill="both", expand=True)
        buffer = ScreenBuffer(96, 26)
        buffer.feed(DEMO_SCREEN)
        self._paint(text, buffer)
        tabs.add(term, text="终端")

        demo_server = ServerConfig(name="演示主机", host="172.29.79.212", username="root")
        monitor = MonitorView(tabs, demo_server, self.credentials, demo=True,
                              on_status=self.set_status)
        tabs.add(monitor, text="监控")

        self.notebook.add(holder, text="演示连接")
        self.notebook.select(holder)
        self._hide_empty()
        self.sessions["demo"] = {"connection": None, "frame": holder, "tabs": tabs,
                                 "terminal": None, "monitor": monitor, "sftp": None}
        self.set_status("演示模式：界面用内置样例数据渲染，不建立真实连接", "warn")

    def _paint(self, text_widget: tk.Text, buffer: ScreenBuffer) -> None:
        tags: dict[str, str] = {}

        def tag_for(attr) -> str:
            key = f"{attr.fg}|{attr.bg}|{attr.bold}|{attr.underline}|{attr.reverse}"
            if key in tags:
                return tags[key]
            name = f"a{len(tags)}"
            tags[key] = name
            fg, bg = attr.fg, attr.bg
            if attr.reverse:
                fg, bg = (attr.bg or TERM_BG), (attr.fg or TERM_FG)
            text_widget.tag_configure(name, foreground=fg or TERM_FG, background=bg or TERM_BG,
                                      underline=bool(attr.underline))
            return name

        text_widget.configure(state="normal")
        text_widget.delete("1.0", "end")
        for row in range(buffer.rows):
            col = 0
            pending: list[str] = []
            pending_attr = None

            def flush() -> None:
                nonlocal pending
                if pending and pending_attr is not None:
                    text_widget.insert("end", "".join(pending), tag_for(pending_attr))
                pending = []

            for ch, attr in buffer.grid[row]:
                if attr != pending_attr:
                    flush()
                    pending_attr = attr
                if ch:
                    pending.append(ch)
                    col += max(1, char_width(ch))
            flush()
            if row < buffer.rows - 1:
                text_widget.insert("end", "\n")
        text_widget.configure(state="disabled")

    # ------------------------------------------------------------------ 状态栏
    def set_status(self, message: str, level: str = "info") -> None:
        colors = {"ok": GREEN, "warn": YELLOW, "error": RED, "info": FG_MUTED}
        try:
            self.status_text.configure(text=message, fg=colors.get(level, FG_MUTED))
            if level in ("ok", "error", "warn"):
                self.status_dot.configure(fg=colors.get(level))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ 退出
    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for server_id in list(self.sessions):
            self._close_session(server_id)
        self.destroy()


def run(demo: bool = False) -> int:
    app = App(demo=demo)
    app.mainloop()
    return 0


if __name__ == "__main__":
    run()
