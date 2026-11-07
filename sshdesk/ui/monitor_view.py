"""监控面板：CPU / 内存 / 磁盘 / 负载 / 进程，带自动刷新。

采集每次都新建一条短连接、采完即关，避免和终端、SFTP 抢同一个 transport。
对 2C2G 的小机器来说，一次握手的开销远小于长期挂着一条空闲连接占内存。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from ..core.client import build_connection
from ..core.config import ServerConfig
from ..core.credentials import CredentialStore
from ..core.monitor import SAMPLE_OUTPUT, Snapshot, collect, parse_snapshot
from .theme import ACCENT, FG, GREEN, PANEL_ALT, RED, YELLOW, mono_font


class MetricCard(ttk.Frame):
    def __init__(self, master, title: str, unit: str = "%") -> None:
        super().__init__(master, style="Card.TFrame", padding=(14, 12))
        self.unit = unit
        ttk.Label(self, text=title, style="CardTitle.TLabel").pack(anchor="w")
        self.value = ttk.Label(self, text="—", style="CardValue.TLabel")
        self.value.pack(anchor="w", pady=(2, 8))
        self.bar = tk.Canvas(self, height=6, bg=PANEL_ALT, highlightthickness=0, bd=0)
        self.bar.pack(fill="x")
        self.sub = ttk.Label(self, text="", style="CardTitle.TLabel")
        self.sub.pack(anchor="w", pady=(6, 0))
        self._canvas_w = 200
        self.bar.bind("<Configure>", self._on_resize)

    def _on_resize(self, event) -> None:
        self._canvas_w = max(20, event.width)
        self._draw(self.bar.percent if hasattr(self.bar, "percent") else 0)

    def set(self, percent: float, sub: str = "", text: str | None = None,
            color: str | None = None) -> None:
        color = color or _level_color(percent)
        self.bar.percent = percent
        self.value.configure(text=text if text is not None else f"{percent:.1f}{self.unit}",
                             foreground=FG)
        self.sub.configure(text=sub)
        self._draw(percent, color)

    def _draw(self, percent: float, color: str = ACCENT) -> None:
        self.bar.delete("all")
        width = self._canvas_w
        self.bar.create_rectangle(0, 0, width, 6, fill=PANEL_ALT, outline="")
        clamped = max(0.0, min(100.0, percent))
        self.bar.create_rectangle(0, 0, int(width * clamped / 100), 6, fill=color, outline="")


def _level_color(percent: float) -> str:
    if percent >= 90:
        return RED
    if percent >= 75:
        return YELLOW
    return ACCENT


class MonitorView(ttk.Frame):
    def __init__(self, master, server: ServerConfig, credentials: CredentialStore,
                 interval: int = 5, demo: bool = False, on_status=None) -> None:
        super().__init__(master, style="TFrame", padding=16)
        self.server = server
        self.credentials = credentials
        self.interval = interval
        self.demo = demo
        self.on_status = on_status or (lambda *_: None)
        self.results: queue.Queue[Snapshot | str] = queue.Queue()
        self._job = None
        self._running = False
        self._auto = tk.BooleanVar(value=not demo)
        self._build()
        self.refresh()
        self._poll_job = self.after(150, self._poll)
        self._tick_job = None if demo else self.after(self.interval * 1000, self._tick)

    def _build(self) -> None:
        header = ttk.Frame(self, style="TFrame")
        header.pack(fill="x", pady=(0, 14))
        self.title = ttk.Label(header, text=self.server.name, style="Title.TLabel")
        self.title.pack(side="left")
        self.meta = ttk.Label(header, text="正在采集…", style="Muted.TLabel")
        self.meta.pack(side="left", padx=14, pady=(4, 0))
        ttk.Button(header, text="立即刷新", style="Ghost.TButton",
                   command=self.refresh).pack(side="right")
        ttk.Checkbutton(header, text="自动刷新", variable=self._auto,
                        command=self._on_toggle_auto).pack(side="right", padx=(0, 10))

        cards = ttk.Frame(self, style="TFrame")
        cards.pack(fill="x")
        self.card_cpu = MetricCard(cards, "CPU 使用率")
        self.card_mem = MetricCard(cards, "内存")
        self.card_disk = MetricCard(cards, "磁盘 /")
        self.card_load = MetricCard(cards, "负载 (1/5/15 分钟)", unit="")
        for idx, card in enumerate((self.card_cpu, self.card_mem, self.card_disk, self.card_load)):
            card.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 10, 0))
            cards.columnconfigure(idx, weight=1, uniform="card")

        info = ttk.Frame(self, style="Card.TFrame", padding=12)
        info.pack(fill="x", pady=(14, 0))
        self.info_text = ttk.Label(info, text="—", style="CardTitle.TLabel",
                                   font=(mono_font(10)[0], 10))
        self.info_text.pack(anchor="w")

        ttk.Label(self, text="资源占用 TOP", style="H2.TLabel").pack(anchor="w", pady=(16, 6))
        columns = ("cpu", "mem", "rss", "cmd")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=6)
        for col, text, width in (
            ("cpu", "CPU%", 70), ("mem", "内存%", 70), ("rss", "RSS(KB)", 90), ("cmd", "进程", 300)
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w" if col == "cmd" else "center")
        self.tree.pack(fill="both", expand=True)
        self.empty_hint = ttk.Label(self, text="", style="Muted.TLabel")
        self.empty_hint.pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------------ 采集
    def refresh(self) -> None:
        if self._running:
            return
        if self.demo:
            self._apply(parse_snapshot(SAMPLE_OUTPUT))
            return
        self._running = True
        self.meta.configure(text="正在采集…")
        threading.Thread(target=self._collect_worker, daemon=True).start()

    def _collect_worker(self) -> None:
        connection = build_connection(self.server, self.credentials,
                                      on_unknown_host=lambda *_: True)
        try:
            connection.connect()
            snapshot = collect(connection)
            self.results.put(snapshot)
        except Exception as exc:  # 采集失败只影响本面板
            self.results.put(str(exc))
        finally:
            connection.close()

    def _apply(self, snap: Snapshot) -> None:
        self._running = False
        self.meta.configure(
            text=f"{snap.os_name or '未知系统'} · 内核 {snap.kernel or '?'} · "
                 f"{snap.cores} 核 · 运行 {snap.uptime_text} · SSH {snap.ssh_service or '?'}"
        )
        self.card_cpu.set(snap.cpu_percent, f"负载 {snap.load1:.2f}")
        self.card_mem.set(snap.mem_percent, snap.mem_text,
                          text=f"{snap.mem_percent:.0f}%")
        self.card_disk.set(snap.disk_percent, snap.disk_text,
                           text=f"{snap.disk_percent:.0f}%")
        self.card_load.set(
            min(100.0, (snap.load1 / snap.cores) * 100) if snap.cores else 0,
            f"{snap.load1:.2f} / {snap.load5:.2f} / {snap.load15:.2f}",
            text=f"{snap.load1:.2f}", color=GREEN if snap.load1 < (snap.cores or 1) else YELLOW
        )
        self.info_text.configure(
            text=f"内存 {snap.mem_text}    Swap {snap.swap_used_mb}M/{snap.swap_total_mb}M    "
                 f"磁盘 {snap.disk_text} ({snap.disk_percent}%)    运行 {snap.uptime_text}"
        )
        self.tree.delete(*self.tree.get_children())
        for proc in snap.processes:
            self.tree.insert("", "end", values=(f"{proc.cpu_percent:.1f}", f"{proc.mem_percent:.1f}",
                                                proc.rss_kb, proc.command))
        self.empty_hint.configure(text="" if snap.processes else "没有取到进程信息")

    def _poll(self) -> None:
        while True:
            try:
                item = self.results.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, Snapshot):
                self._apply(item)
                self.on_status("监控数据已更新", "ok")
            else:
                self._running = False
                self.meta.configure(text=f"采集失败：{item}")
                self.on_status("监控采集失败", "error")
        self._poll_job = self.after(150, self._poll)

    def _tick(self) -> None:
        if self._auto.get():
            self.refresh()
        self._tick_job = self.after(self.interval * 1000, self._tick)

    def _on_toggle_auto(self) -> None:
        if self._auto.get():
            self.refresh()

    def close(self) -> None:
        for job in (getattr(self, "_poll_job", None), getattr(self, "_tick_job", None)):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
