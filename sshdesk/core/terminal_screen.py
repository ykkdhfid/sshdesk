"""轻量 VT100/xterm 屏幕缓冲。

为什么需要它：paramiko 的 invoke_shell 返回的是原始字节流，里面混着
CSI/OSC 控制序列。直接塞进 tkinter Text 会看到一堆 `^[[32m`。这里维护一个
字符网格，把控制序列解释成「光标移动 / 擦除 / 滚动 / 颜色」等操作，UI 只负责
把网格画出来，于是 vim、top、htop 这类全屏程序也能正常显示。

支持的子集（够用且可测试）：
  光标：CUP/CUU/CUD/CUF/CUB/CHA/VPA/HVP/保存恢复
  擦除：ED(0/1/2/3)、EL(0/1/2)、ECH、DCH、ICH
  行：   IL/DL、滚动区 DECSTBM、SU/SD
  样式：SGR 0/1/2/4/7/22/24/27、30-37/39/40-47/49、90-97/100-107、38/48 的 5;n 与 2;r;g;b
  模式：?25 光标显隐、?1049 备用屏、?7 自动换行（记录但按开启处理）
  其它：OSC 标题、BEL、BS、TAB、CR/LF
"""

from __future__ import annotations

import codecs
import unicodedata
from dataclasses import dataclass, replace

# xterm 256 色板
_BASE16 = [
    "#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
    "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff",
]


def ansi256_to_hex(code: int) -> str:
    if code < 16:
        return _BASE16[code % 16]
    if code < 232:
        code -= 16
        r, g, b = code // 36, (code % 36) // 6, code % 6
        levels = [0, 95, 135, 175, 215, 255]
        return f"#{levels[r]:02x}{levels[g]:02x}{levels[b]:02x}"
    gray = 8 + (code - 232) * 10
    return f"#{gray:02x}{gray:02x}{gray:02x}"


def char_width(ch: str) -> int:
    """东亚宽字符占两列，组合字符占 0 列。终端对齐全靠这个。"""
    if not ch:
        return 0
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


@dataclass(frozen=True)
class Attr:
    fg: str = ""
    bg: str = ""
    bold: bool = False
    underline: bool = False
    reverse: bool = False

    def with_reverse(self) -> "Attr":
        return replace(self, fg=self.bg or "#e5e5e5", bg=self.fg or "#000000")


DEFAULT_ATTR = Attr()


class ScreenBuffer:
    def __init__(self, cols: int = 80, rows: int = 24, scrollback_limit: int = 2000) -> None:
        self.cols = max(2, cols)
        self.rows = max(2, rows)
        self.scrollback_limit = scrollback_limit
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.reset()

    # ------------------------------------------------------------------ 状态
    def reset(self) -> None:
        self.grid: list[list[tuple[str, Attr]]] = [
            [(" ", DEFAULT_ATTR) for _ in range(self.cols)] for _ in range(self.rows)
        ]
        self.attr = DEFAULT_ATTR
        self.cursor_r = 0
        self.cursor_c = 0
        self.saved_cursor = (0, 0)
        self.scroll_top = 0
        self.scroll_bottom = self.rows - 1
        self.pending_wrap = False
        self.cursor_visible = True
        self.title = ""
        self.bell_count = 0
        self.scrollback: list[list[tuple[str, Attr]]] = []
        self.dirty = True

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(2, cols), max(2, rows)
        if cols == self.cols and rows == self.rows:
            return
        new_grid = [[(" ", DEFAULT_ATTR) for _ in range(cols)] for _ in range(rows)]
        for r in range(min(rows, self.rows)):
            for c in range(min(cols, self.cols)):
                new_grid[r][c] = self.grid[r][c]
        self.cols, self.rows, self.grid = cols, rows, new_grid
        self.cursor_r = min(self.cursor_r, rows - 1)
        self.cursor_c = min(self.cursor_c, cols - 1)
        self.scroll_top, self.scroll_bottom = 0, rows - 1
        self.dirty = True

    # ------------------------------------------------------------------ 输入
    def feed(self, data: bytes | str) -> None:
        """喂入原始字节；内部用增量解码，避免把一个 UTF-8 汉字劈成两半。"""
        if isinstance(data, bytes):
            text = self._decoder.decode(data)
        else:
            text = data
        self._parse(text)
        self.dirty = True

    def _parse(self, text: str) -> None:
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == "\x1b":
                i = self._handle_escape(text, i)
                continue
            if ch == "\r":
                self.cursor_c = 0
                self.pending_wrap = False
            elif ch == "\n":
                self._line_feed()
                self.pending_wrap = False
            elif ch == "\b":
                self.cursor_c = max(0, self.cursor_c - 1)
                self.pending_wrap = False
            elif ch == "\t":
                self.cursor_c = min(self.cols - 1, (self.cursor_c // 8 + 1) * 8)
            elif ch == "\x07":
                self.bell_count += 1
            elif ch == "\x00" or ord(ch) < 32:
                pass  # 其它控制字符忽略
            else:
                self._put_char(ch)
            i += 1

    def _handle_escape(self, text: str, i: int) -> int:
        n = len(text)
        if i + 1 >= n:
            return n
        nxt = text[i + 1]
        if nxt == "[":
            j = i + 2
            while j < n and not ("\x40" <= text[j] <= "\x7e"):
                j += 1
            if j >= n:
                return n
            self._csi(text[i + 2 : j], text[j])
            return j + 1
        if nxt == "]":
            # OSC ... BEL 或 ST
            j = i + 2
            while j < n:
                if text[j] == "\x07":
                    self._osc(text[i + 2 : j])
                    return j + 1
                if text[j] == "\x1b" and j + 1 < n and text[j + 1] == "\\":
                    self._osc(text[i + 2 : j])
                    return j + 2
                j += 1
            return n
        if nxt in "78":
            if nxt == "7":
                self.saved_cursor = (self.cursor_r, self.cursor_c)
            else:
                self.cursor_r, self.cursor_c = self.saved_cursor
            return i + 2
        if nxt == "M":
            self._reverse_index()
            return i + 2
        if nxt == "D":
            self._line_feed()
            return i + 2
        if nxt == "E":
            self.cursor_c = 0
            self._line_feed()
            return i + 2
        if nxt == "c":
            self.reset()
            return i + 2
        if nxt in "()*+,-./#":
            # 字符集选择（SCS）/ DEC 行属性：ESC <中间字节> <最终字节>，
            # 例如 ESC ( B 切回 ASCII。整段忽略，不能把末尾的字母漏成普通字符。
            return min(i + 3, n)
        return i + 2

    # ------------------------------------------------------------------ 绘制
    def _put_char(self, ch: str) -> None:
        width = char_width(ch)
        if width == 0:
            return
        if self.pending_wrap:
            self.cursor_c = 0
            self._line_feed()
            self.pending_wrap = False
        if self.cursor_c + width > self.cols:
            self.cursor_c = 0
            self._line_feed()
        self.grid[self.cursor_r][self.cursor_c] = (ch, self.attr)
        if width == 2 and self.cursor_c + 1 < self.cols:
            self.grid[self.cursor_r][self.cursor_c + 1] = ("", self.attr)
        self.cursor_c += width
        if self.cursor_c >= self.cols:
            self.cursor_c = self.cols - 1
            self.pending_wrap = True

    def _line_feed(self) -> None:
        if self.cursor_r == self.scroll_bottom:
            self._scroll_up(1)
        elif self.cursor_r < self.rows - 1:
            self.cursor_r += 1

    def _reverse_index(self) -> None:
        if self.cursor_r == self.scroll_top:
            self._scroll_down(1)
        elif self.cursor_r > 0:
            self.cursor_r -= 1

    def _scroll_up(self, n: int = 1) -> None:
        for _ in range(n):
            line = self.grid.pop(self.scroll_top)
            self.grid.insert(self.scroll_bottom, [(" ", DEFAULT_ATTR) for _ in range(self.cols)])
            if self.scroll_top == 0:
                self.scrollback.append(line)
                if len(self.scrollback) > self.scrollback_limit:
                    del self.scrollback[0]

    def _scroll_down(self, n: int = 1) -> None:
        for _ in range(n):
            self.grid.pop(self.scroll_bottom)
            self.grid.insert(self.scroll_top, [(" ", DEFAULT_ATTR) for _ in range(self.cols)])

    def _erase_range(self, row: int, start: int, end: int) -> None:
        """擦除的语义是填空格 + 当前背景色，不是删字符。"""
        blank = (" ", replace(DEFAULT_ATTR, bg=self.attr.bg))
        for c in range(max(0, start), min(self.cols, end)):
            self.grid[row][c] = blank

    # ------------------------------------------------------------------ CSI
    def _csi(self, params: str, final: str) -> None:
        private = params.startswith("?")
        if private:
            params = params[1:]
        parts = [p for p in params.split(";")] if params else []
        nums: list[int] = []
        for p in parts:
            try:
                nums.append(int(p) if p else 0)
            except ValueError:
                nums.append(0)

        def arg(idx: int, default: int = 1) -> int:
            if idx < len(nums) and nums[idx] > 0:
                return nums[idx]
            return default

        if final in ("H", "f"):
            self.cursor_r = min(self.rows - 1, max(0, arg(0) - 1))
            self.cursor_c = min(self.cols - 1, max(0, arg(1) - 1))
            self.pending_wrap = False
        elif final == "A":
            self.cursor_r = max(self.scroll_top, self.cursor_r - arg(0))
        elif final == "B":
            self.cursor_r = min(self.scroll_bottom, self.cursor_r + arg(0))
        elif final == "C":
            self.cursor_c = min(self.cols - 1, self.cursor_c + arg(0))
        elif final == "D":
            self.cursor_c = max(0, self.cursor_c - arg(0))
        elif final == "E":
            self.cursor_r = min(self.scroll_bottom, self.cursor_r + arg(0))
            self.cursor_c = 0
        elif final == "F":
            self.cursor_r = max(self.scroll_top, self.cursor_r - arg(0))
            self.cursor_c = 0
        elif final in ("G", "`"):
            self.cursor_c = min(self.cols - 1, max(0, arg(0) - 1))
        elif final == "d":
            self.cursor_r = min(self.rows - 1, max(0, arg(0) - 1))
        elif final == "J":
            mode = nums[0] if nums else 0
            if mode == 0:
                self._erase_range(self.cursor_r, self.cursor_c, self.cols)
                for r in range(self.cursor_r + 1, self.rows):
                    self._erase_range(r, 0, self.cols)
            elif mode == 1:
                for r in range(0, self.cursor_r):
                    self._erase_range(r, 0, self.cols)
                self._erase_range(self.cursor_r, 0, self.cursor_c + 1)
            else:
                for r in range(self.rows):
                    self._erase_range(r, 0, self.cols)
                self.cursor_r, self.cursor_c = 0, 0
        elif final == "K":
            mode = nums[0] if nums else 0
            if mode == 0:
                self._erase_range(self.cursor_r, self.cursor_c, self.cols)
            elif mode == 1:
                self._erase_range(self.cursor_r, 0, self.cursor_c + 1)
            else:
                self._erase_range(self.cursor_r, 0, self.cols)
        elif final == "X":
            self._erase_range(self.cursor_r, self.cursor_c, self.cursor_c + arg(0))
        elif final == "P":
            count = arg(0)
            row = self.grid[self.cursor_r]
            del row[self.cursor_c : self.cursor_c + count]
            row.extend([(" ", DEFAULT_ATTR)] * count)
        elif final == "@":
            count = arg(0)
            row = self.grid[self.cursor_r]
            for _ in range(count):
                row.insert(self.cursor_c, (" ", DEFAULT_ATTR))
            del row[self.cols :]
        elif final == "L":
            self._insert_lines(arg(0))
        elif final == "M":
            self._delete_lines(arg(0))
        elif final == "S":
            self._scroll_up(arg(0))
        elif final == "T":
            self._scroll_down(arg(0))
        elif final == "r":
            top = arg(0) - 1
            bottom = arg(1, self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self.scroll_top, self.scroll_bottom = top, bottom
                self.cursor_r, self.cursor_c = 0, 0
        elif final == "m":
            self._sgr(nums)
        elif final == "s":
            self.saved_cursor = (self.cursor_r, self.cursor_c)
        elif final == "u":
            self.cursor_r, self.cursor_c = self.saved_cursor
        elif final in ("h", "l"):
            on = final == "h"
            for num in nums:
                if num == 25:
                    self.cursor_visible = on
                elif num == 1049 and on:
                    pass  # 备用屏：只记录，主屏内容保留
        # 其它（DSR/DA/窗口缩放）忽略即可

    def _insert_lines(self, n: int) -> None:
        for _ in range(n):
            if self.scroll_top <= self.cursor_r <= self.scroll_bottom:
                self.grid.pop(self.scroll_bottom)
                self.grid.insert(self.cursor_r, [(" ", DEFAULT_ATTR) for _ in range(self.cols)])

    def _delete_lines(self, n: int) -> None:
        for _ in range(n):
            if self.scroll_top <= self.cursor_r <= self.scroll_bottom:
                self.grid.pop(self.cursor_r)
                self.grid.insert(self.scroll_bottom, [(" ", DEFAULT_ATTR) for _ in range(self.cols)])

    # ------------------------------------------------------------------ SGR
    def _sgr(self, nums: list[int]) -> None:
        attr = self.attr
        if not nums:
            self.attr = DEFAULT_ATTR
            return
        i = 0
        while i < len(nums):
            n = nums[i]
            if n == 0:
                attr = DEFAULT_ATTR
            elif n == 1:
                attr = replace(attr, bold=True)
            elif n == 2:
                attr = replace(attr, fg=attr.fg or "#7f7f7f")
            elif n == 4:
                attr = replace(attr, underline=True)
            elif n == 7:
                attr = replace(attr, reverse=True)
            elif n == 22:
                attr = replace(attr, bold=False)
            elif n == 24:
                attr = replace(attr, underline=False)
            elif n == 27:
                attr = replace(attr, reverse=False)
            elif 30 <= n <= 37:
                attr = replace(attr, fg=_BASE16[n - 30])
            elif n == 39:
                attr = replace(attr, fg="")
            elif 40 <= n <= 47:
                attr = replace(attr, bg=_BASE16[n - 40])
            elif n == 49:
                attr = replace(attr, bg="")
            elif 90 <= n <= 97:
                attr = replace(attr, fg=_BASE16[n - 90 + 8])
            elif 100 <= n <= 107:
                attr = replace(attr, bg=_BASE16[n - 100 + 8])
            elif n in (38, 48) and i + 1 < len(nums):
                target = "fg" if n == 38 else "bg"
                mode = nums[i + 1]
                if mode == 5 and i + 2 < len(nums):
                    attr = replace(attr, **{target: ansi256_to_hex(nums[i + 2])})
                    i += 2
                elif mode == 2 and i + 4 < len(nums):
                    r, g, b = nums[i + 2], nums[i + 3], nums[i + 4]
                    attr = replace(attr, **{target: f"#{r:02x}{g:02x}{b:02x}"})
                    i += 4
                else:
                    i += 1
            i += 1
        self.attr = attr

    def _osc(self, payload: str) -> None:
        # 0=标题 2=标题（不同终端习惯）
        if ";" in payload:
            code, value = payload.split(";", 1)
            if code in ("0", "2") and value:
                self.title = value

    # ------------------------------------------------------------------ 输出
    def line_segments(self, row: int) -> list[tuple[str, Attr]]:
        """把一行合并成 (文本片段, 样式) 列表，UI 直接按片段打 tag。"""
        cells = self.grid[row]
        segments: list[tuple[str, Attr]] = []
        buf: list[str] = []
        cur: Attr | None = None
        for ch, attr in cells:
            if cur is None or attr != cur:
                if buf and cur is not None:
                    segments.append(("".join(buf), cur))
                buf, cur = [ch], attr
            else:
                buf.append(ch)
        if buf and cur is not None:
            segments.append(("".join(buf), cur))
        # 去掉行尾纯空白：整行同一样式时会合并成一个大片段，必须在片段内部裁
        while segments:
            text, attr = segments[-1]
            if attr.bg:  # 有背景色的空白是内容，不能裁
                break
            stripped = text.rstrip()
            if stripped:
                segments[-1] = (stripped, attr)
                break
            segments.pop()
        return segments

    def line_text(self, row: int) -> str:
        return "".join(ch for ch, _ in self.grid[row]).rstrip()

    def all_lines(self) -> list[str]:
        return [self.line_text(r) for r in range(self.rows)]

    def visible_text(self) -> str:
        return "\n".join(self.all_lines())
