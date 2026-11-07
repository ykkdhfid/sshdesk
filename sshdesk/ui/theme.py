"""统一配色与字体。

用深色终端配色：长时间盯着终端，深底浅字的疲劳感明显低于白底。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, ttk

# 星空：深冷蓝黑 + 低饱和星光；桌面端与 Web 端保持同一套色
BG = "#070a12"
PANEL = "#0d1220"
PANEL_ALT = "#131a2b"
BORDER = "#1e2740"
FG = "#e6eaf5"
FG_MUTED = "#9aa3b8"
ACCENT = "#7b9df0"
ACCENT_2 = "#94b4ff"
GREEN = "#5fd1a0"
RED = "#f0708a"
YELLOW = "#e0b76a"
PURPLE = "#a9a0e8"

MONO_CANDIDATES = [
    "JetBrains Mono", "Cascadia Mono", "Sarasa Mono SC", "Consolas",
    "DejaVu Sans Mono", "Noto Sans Mono CJK SC", "Courier New",
]

UI_CANDIDATES = [
    "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
    "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei", "DejaVu Sans",
]


def mono_font(size: int = 10) -> tuple[str, int]:
    available = set(tkfont.families())
    for name in MONO_CANDIDATES:
        if name in available:
            return (name, size)
    return ("TkFixedFont", size)


def ui_font(size: int = 9, weight: str = "normal") -> tuple[str, int, str]:
    available = set(tkfont.families())
    for name in UI_CANDIDATES:
        if name in available:
            return (name, size, weight)
    return ("TkDefaultFont", size, weight)


def apply_theme(root: tk.Misc) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")  # clam 对深色主题的自定义支持最好
    except tk.TclError:
        pass

    root.configure(bg=BG)
    style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                    bordercolor=BORDER, focuscolor=ACCENT, font=ui_font(9))
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("Card.TFrame", background=PANEL_ALT, relief="flat")
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Muted.TLabel", background=BG, foreground=FG_MUTED)
    style.configure("Title.TLabel", background=BG, foreground=FG, font=ui_font(14, "bold"))
    style.configure("H2.TLabel", background=PANEL, foreground=FG, font=ui_font(10, "bold"))
    style.configure("CardTitle.TLabel", background=PANEL_ALT, foreground=FG_MUTED,
                    font=ui_font(9))
    style.configure("CardValue.TLabel", background=PANEL_ALT, foreground=FG,
                    font=ui_font(18, "bold"))
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    borderwidth=0, focusthickness=0, padding=(12, 6))
    style.map("Accent.TButton", background=[("active", "#a695ff"), ("disabled", "#332c4c")])
    style.configure("TButton", background=PANEL_ALT, foreground=FG, borderwidth=0, padding=(10, 5))
    style.map("TButton", background=[("active", BORDER), ("disabled", "#232036")])
    style.configure("Ghost.TButton", background=BG, foreground=FG_MUTED, borderwidth=0, padding=(8, 4))
    style.map("Ghost.TButton", background=[("active", PANEL)])
    style.configure("TEntry", fieldbackground=PANEL_ALT, foreground=FG,
                    insertcolor=FG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=PANEL, foreground=FG_MUTED, padding=(16, 8))
    style.map("TNotebook.Tab",
              background=[("selected", BG)], foreground=[("selected", FG)])
    style.configure("Treeview", background=PANEL, foreground=FG,
                    fieldbackground=PANEL, borderwidth=0, rowheight=26)
    style.configure("Treeview.Heading", background=PANEL_ALT, foreground=FG_MUTED, borderwidth=0)
    style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
    style.configure("TProgressbar", background=ACCENT, troughcolor=PANEL_ALT, borderwidth=0)
    style.configure("TSeparator", background=BORDER)
    style.configure("TCombobox", fieldbackground=PANEL_ALT, background=PANEL_ALT,
                    foreground=FG, arrowcolor=FG_MUTED)
