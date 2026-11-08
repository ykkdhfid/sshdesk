"""终端视图：把 SSH 的 PTY 字节流渲染成可交互的终端。

关键点：
1. 远端字节 -> ScreenBuffer（解析 ANSI）-> tkinter Text。UI 线程只负责画，
   收数据在工作线程，中间用 queue 传递，避免阻塞事件循环。
2. tkinter 的索引按「字符」计数，而终端按「显示列」定位。行内出现中文时两者
   不一致，所以渲染每行时同时建立 列->字符索引 映射，光标才不会跳错位置。
"""

from __future__ import annotations

import queue
import socket
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont, ttk

from ..core.client import SSHConnection, SSHError
from ..core.terminal_screen import ScreenBuffer, char_width
from .theme import mono_font

TERM_BG = "#04060d"
TERM_FG = "#e6eaf5"
TERM_CURSOR = "#8fb0f5"

SPECIAL_KEYS = {
    "Up": "\x1b[A", "Down": "\x1b[B", "Right": "\x1b[C", "Left": "\x1b[D",
    "Home": "\x1b[H", "End": "\x1b[F", "Insert": "\x1b[2~", "Delete": "\x1b[3~",
    "Prior": "\x1b[5~", "Next": "\x1b[6~",
    "F1": "\x1bOP", "F2": "\x1bOQ", "F3": "\x1bOR", "F4": "\x1bOS",
    "F5": "\x1b[15~", "F6": "\x1b[17~", "F7": "\x1b[18~", "F8": "\x1b[19~",
    "F9": "\x1b[20~", "F10": "\x1b[21~", "F11": "\x1b[23~", "F12": "\x1b[24~",
}

CONTROL_KEYS = {
    "bracketleft": "\x1b", "backslash": "\x1c", "bracketright": "\x1d",
    "asciicircum": "\x1e", "underscore": "\x1f", "space": "\x00",
    "at": "\x00", "slash": "\x1f",
}


class TerminalView(ttk.Frame):
    def __init__(self, master, connection: SSHConnection, cols: int = 100, rows: int = 28,
                 on_status=None, on_title=None) -> None:
        super().__init__(master, style="TFrame")
        self.conn = connection
        self.on_status = on_status or (lambda *_: None)
        self.on_title = on_title or (lambda *_: None)
        self.buffer = ScreenBuffer(cols, rows)
        self.queue: queue.Queue[bytes] = queue.Queue()
        self._stop = threading.Event()
        self._closed = False
        self._tags: dict[str, str] = {}
        self._last_signature = None
        self._render_job: str | None = None
        self._chars_per_line = cols
        # 0 = 实时画面；大于 0 表示向上回看 scrollback 的行数
        self._view_offset = 0

        self._build_ui()
        self._open_shell()
        self._start_reader()
        self._schedule_render()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        mono = mono_font(11)
        self.text = tk.Text(
            self, bg=TERM_BG, fg=TERM_FG, insertbackground=TERM_FG, font=mono,
            wrap="none", undo=False, highlightthickness=0, borderwidth=0,
            padx=8, pady=6, selectbackground="#1d2b4a", selectforeground="#ffffff",
            cursor="xterm", spacing1=0, spacing2=0, spacing3=0, insertwidth=0,
        )
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        # 用 grid 让终端铺满
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.text.tag_configure("cursor", background=TERM_CURSOR, foreground=TERM_BG)
        self.text.configure(state="normal")

        self.text.bind("<Key>", self._on_key)
        self.text.bind("<Button-1>", lambda e: self.text.focus_set())
        self.text.bind("<Control-Shift-Key-c>", self._copy)
        self.text.bind("<Control-Shift-Key-C>", self._copy)
        self.text.bind("<Control-Shift-Key-v>", self._paste)
        self.text.bind("<Control-Shift-Key-V>", self._paste)
        self.text.bind("<Control-Shift-Insert>", self._paste)
        # Windows 的习惯：Ctrl+Insert 复制、Shift+Insert 粘贴。
        # 这两个键原来没绑定，按下去会走 <Key> 把 "\x1b[2~" 当输入发给远端，
        # 屏幕上就多出一串 ^[[2~ —— 看着像"复制没反应还乱码"。
        self.text.bind("<Control-Insert>", self._copy)
        self.text.bind("<Shift-Insert>", self._paste)
        self.text.bind("<Control-c>", self._on_ctrl_c)
        self.text.bind("<Control-v>", self._paste)

        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="复制", command=self._copy)
        self._menu.add_command(label="粘贴", command=self._paste)
        self._menu.add_command(label="全选", command=self._select_all)
        self.text.bind("<Button-3>", self._popup_menu)

        self.text.bind("<Configure>", self._on_resize)
        self.text.bind("<<Paste>>", lambda e: "break")
        # 滚动历史：Windows/macOS 用 MouseWheel，X11 用 Button-4/5
        self.text.bind("<MouseWheel>", self._on_wheel)
        self.text.bind("<Button-4>", lambda e: self._scroll_history(3))
        self.text.bind("<Button-5>", lambda e: self._scroll_history(-3))
        self.text.bind("<Prior>", lambda e: self._scroll_history(self.buffer.rows - 1))
        self.text.bind("<Next>", lambda e: self._scroll_history(-(self.buffer.rows - 1)))
        self.text.focus_set()
        self._disable_windows_ime()

    def _disable_windows_ime(self) -> None:
        """把输入法从终端控件上摘掉（仅 Windows）。

        Tk 的 Text 在中文输入法激活时，字母会先被 IME 接走组词，Key 事件里的 char 是空串，
        字符根本到不了终端——表现就是回车能用、字母一个也打不进去。切断 IMContext 关联后，
        每个按键直接作为 Key 事件交给 Tk，和 PuTTY / Xshell 这类原生命令行一致。
        代价：终端里不直接支持中文输入（SSH 命令行极少需要，真要输可以在别处打好再粘贴）。
        """
        import sys
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            imm32 = ctypes.WinDLL("imm32")
            imm32.ImmAssociateContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
            imm32.ImmAssociateContext.restype = wintypes.HANDLE
            imm32.ImmAssociateContext(self.text.winfo_id(), 0)
        except Exception:
            pass

    def _open_shell(self) -> None:
        try:
            self.channel = self.conn.open_shell(self.buffer.cols, self.buffer.rows)
            self.channel.settimeout(1.0)
        except SSHError as exc:
            self.on_status(str(exc), "error")
            self.text.insert("end", f"\n连接失败：{exc}\n")
            self._closed = True

    # ------------------------------------------------------------------ 数据流
    def _start_reader(self) -> None:
        if self._closed:
            return
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.channel.recv(8192)
            except socket.timeout:
                continue
            except (OSError, EOFError):
                break
            except Exception:
                break
            if data:
                self.queue.put(data)
            elif self.channel.closed:
                break
        if not self._stop.is_set():
            self.queue.put("\r\n\x1b[33m[连接已断开]\x1b[0m\r\n".encode("utf-8"))
            self.on_status("连接已断开", "error")

    def send(self, text: str) -> None:
        if self._closed or not getattr(self, "channel", None):
            return
        try:
            self.channel.send(text.encode("utf-8"))
        except (OSError, EOFError):
            self.on_status("发送失败，连接可能已断开", "error")

    # ------------------------------------------------------------------ 渲染
    def _schedule_render(self) -> None:
        if self._closed:
            return
        self._render_job = self.after(40, self._tick)

    def _tick(self) -> None:
        drained = False
        while True:
            try:
                chunk = self.queue.get_nowait()
            except queue.Empty:
                break
            self.buffer.feed(chunk)
            drained = True
        if drained or self.buffer.dirty:
            self._render()
        if self.buffer.title:
            self.on_title(self.buffer.title)
            self.buffer.title = ""
        self._schedule_render()

    def _render(self) -> None:
        signature = self._signature()
        if signature == self._last_signature:
            self.buffer.dirty = False
            return
        self._last_signature = signature
        self.buffer.dirty = False

        self.text.delete("1.0", "end")
        cursor_index = None
        for row in range(self.buffer.rows):
            line_chars, mapping = self._render_row(self._cells_for(row))
            if self._view_offset == 0 and row == self.buffer.cursor_r:
                cursor_index = (row, mapping.get(self.buffer.cursor_c, line_chars))
            if row < self.buffer.rows - 1:
                self.text.insert("end", "\n")

        if self._view_offset == 0 and self.buffer.cursor_visible and cursor_index:
            r, c = cursor_index
            # 光标落在行尾时后面没有字符可以染色，补个空格占位。
            # 不补的话提示符后面看上去完全没有光标（Text 自带的插入符已经被 insertwidth=0 关掉了）
            line_len = int(self.text.index(f"{r + 1}.end").split(".")[1])
            if c >= line_len:
                self.text.insert(f"{r + 1}.end", " ")
            idx = f"{r + 1}.{c}"
            self.text.tag_add("cursor", idx, f"{r + 1}.{c + 1}")
            # 每个字符都挂着 ANSI tag（而且带背景色），它比 cursor 创建得晚、优先级更高，
            # 会把光标的反色背景盖掉。这里把 cursor 提到最上层，光标才看得见。
            self.text.tag_raise("cursor")
            self.text.mark_set("insert", idx)
        self.text.see(f"{cursor_index[0] + 1}.0" if cursor_index else "end")

    def _render_row(self, cells) -> tuple[int, dict[int, int]]:
        """渲染一行并返回 (该行字符数, 列->字符索引映射)。"""
        mapping: dict[int, int] = {}
        col = 0
        char_index = 0
        pending: list[str] = []
        pending_attr = None

        def flush() -> None:
            nonlocal pending
            if pending and pending_attr is not None:
                self.text.insert("end", "".join(pending), self._tag_for(pending_attr))
            pending = []

        for ch, attr in cells:
            mapping[col] = char_index
            if attr != pending_attr:
                flush()
                pending_attr = attr
            if ch == "":
                # 宽字符的占位格：不占字符位置
                col += 1
                continue
            pending.append(ch)
            width = char_width(ch)
            col += max(1, width)
            char_index += 1
        flush()
        return char_index, mapping

    def _tag_for(self, attr) -> str:
        key = f"{attr.fg}|{attr.bg}|{attr.bold}|{attr.underline}|{attr.reverse}"
        if key in self._tags:
            return self._tags[key]
        name = f"a{len(self._tags)}"
        self._tags[key] = name
        fg, bg = attr.fg, attr.bg
        if attr.reverse:
            fg, bg = (attr.bg or TERM_BG), (attr.fg or TERM_FG)
        kwargs = {
            "foreground": fg or TERM_FG,
            "background": bg or TERM_BG,
            "underline": bool(attr.underline),
        }
        if attr.bold:
            if not hasattr(self, "_bold_font"):
                base_font = tkfont.Font(font=self.text.cget("font"))
                self._bold_font = tkfont.Font(family=base_font.actual("family"),
                                              size=base_font.actual("size"), weight="bold")
            kwargs["font"] = self._bold_font
        self.text.tag_configure(name, **kwargs)
        return name

    def _signature(self):
        """轻量指纹：用于跳过无变化的重复重绘。"""
        parts = []
        for row in range(self.buffer.rows):
            for ch, attr in self._cells_for(row):
                if ch != " ":
                    parts.append((ch, attr.fg, attr.bg, attr.bold, attr.underline))
        return (self.buffer.cursor_r, self.buffer.cursor_c,
                self.buffer.cursor_visible, self._view_offset, tuple(parts))

    # ------------------------------------------------------------------ 回看
    def _cells_for(self, row: int) -> list[tuple[str, object]]:
        """第 row 个可见行对应的单元格；回看时从 scrollback 里取。

        画面 = scrollback（历史）+ grid（当前屏）拼成的连续行，
        回看 offset 行就是把窗口整体上移，底部再对齐回 grid 底部。
        """
        if self._view_offset == 0:
            return self.buffer.grid[row]
        history = self.buffer.scrollback
        index = len(history) - self._view_offset + row
        if index < len(history):
            return history[index]
        tail = index - len(history)
        if 0 <= tail < self.buffer.rows:
            return self.buffer.grid[tail]
        return self.buffer.grid[0]

    def _scroll_history(self, delta: int) -> str:
        limit = len(self.buffer.scrollback)
        target = max(0, min(limit, self._view_offset + delta))
        if target == self._view_offset:
            return "break"
        self._view_offset = target
        if target:
            self.on_status(f"回看模式：向上 {target}/{limit} 行，按任意键回到最新", "")
        else:
            self.on_status("已回到最新输出", "")
        self._render()
        return "break"

    def _on_wheel(self, event) -> str:
        # Windows 上是 ±120 的倍数，macOS 上是较小的增量
        step = 3 if event.delta > 0 else -3
        return self._scroll_history(step)

    # ------------------------------------------------------------------ 交互
    def _on_key(self, event) -> str:
        if self._closed:
            return "break"
        if self._view_offset:
            # 一旦开始输入，自动回到最新画面，和真实终端一致
            self._view_offset = 0
            self.on_status("已回到最新输出", "")
        state = event.state
        ctrl = bool(state & 0x4)
        shift = bool(state & 0x1)
        # Alt 的状态位两个平台不同：X11 上是 Mod1(0x8)，Windows 上的 Tk 用 0x20000。
        # 实测 Windows 下即使不按任何修饰键 state 也恒为 0x8；照 0x8 判 Alt 会给每个字符
        # 前面多塞一个 ESC，服务器收到的是 Alt+字母（bash 回显 ^[a），看着就是"打不进字"。
        if sys.platform == "win32":
            alt = bool(state & 0x20000)
        else:
            alt = bool(state & 0x8)
        keysym = event.keysym

        if ctrl and shift and keysym.lower() in ("c", "v"):
            return "break"  # 已由独立绑定处理

        if keysym in SPECIAL_KEYS:
            self.send(SPECIAL_KEYS[keysym])
            return "break"
        if keysym in ("Return", "KP_Enter"):
            self.send("\r")
            return "break"
        if keysym == "BackSpace":
            self.send("\x7f")
            return "break"
        if keysym == "Tab":
            self.send("\x1b[Z" if shift else "\t")
            return "break"
        if keysym == "Escape":
            self.send("\x1b")
            return "break"
        if keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
                      "Super_L", "Super_R", "Caps_Lock", "Num_Lock"):
            return "break"

        if ctrl:
            if keysym in CONTROL_KEYS:
                self.send(CONTROL_KEYS[keysym])
            elif len(keysym) == 1 and keysym.isalpha():
                self.send(chr(ord(keysym.lower()) - 96))
            elif keysym in ("Up", "Down", "Left", "Right"):
                arrow = {"Up": "A", "Down": "B", "Right": "C", "Left": "D"}[keysym]
                self.send(f"\x1b[1;5{arrow}")
            return "break"

        if event.char and (event.char.isprintable() or event.char == " "):
            self.send(("\x1b" + event.char) if alt else event.char)
            return "break"
        return "break"

    def _on_ctrl_c(self, event=None) -> str:
        """有选中内容就复制；没有选中才把 Ctrl+C 当成中断信号发给远端。"""
        if self.text.tag_ranges("sel"):
            return self._copy()
        self.send("\x03")
        return "break"

    def _select_all(self) -> None:
        self.text.tag_add("sel", "1.0", "end-1c")

    def _popup_menu(self, event) -> str:
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()
        return "break"

    def _copy(self, event=None) -> str:
        try:
            self.clipboard_clear()
            self.clipboard_append(self.text.get("sel.first", "sel.last"))
        except tk.TclError:
            pass
        return "break"

    def _paste(self, event=None) -> str:
        try:
            content = self.clipboard_get()
        except tk.TclError:
            return "break"
        # 多行粘贴：把 LF 统一成 CR，否则远端 shell 会逐行卡住
        self.send(content.replace("\r\n", "\r").replace("\n", "\r"))
        return "break"

    def _on_resize(self, event) -> None:
        """按字符宽高换算行列数，并同步给远端的 PTY。"""
        try:
            char_w = tkfont.Font(font=self.text.cget("font")).measure("0") or 8
            line_h = tkfont.Font(font=self.text.cget("font")).metrics("linespace") or 16
        except tk.TclError:
            return
        cols = max(20, (event.width - 16) // char_w)
        rows = max(6, (event.height - 12) // line_h)
        if abs(cols - self.buffer.cols) < 2 and abs(rows - self.buffer.rows) < 2:
            return
        self.buffer.resize(cols, rows)
        self.conn.resize_pty(cols, rows)

    # ------------------------------------------------------------------ 清理
    def close(self) -> None:
        self._closed = True
        self._stop.set()
        if self._render_job:
            try:
                self.after_cancel(self._render_job)
            except tk.TclError:
                pass
        try:
            channel = getattr(self, "channel", None)
            if channel is not None:
                channel.close()
        except Exception:
            pass
