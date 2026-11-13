"""VT100 屏幕缓冲测试：这是终端能正常显示的核心，必须逐条钉住。"""

from __future__ import annotations

from sshdesk.core.terminal_screen import ScreenBuffer, ansi256_to_hex, char_width


def feed(text: str, cols: int = 40, rows: int = 10) -> ScreenBuffer:
    buffer = ScreenBuffer(cols, rows)
    buffer.feed(text)
    return buffer


def test_plain_text_and_newline():
    buf = feed("hello\r\nworld")
    assert buf.line_text(0) == "hello"
    assert buf.line_text(1) == "world"


def test_carriage_return_overwrites_line():
    buf = feed("abc\rX")
    assert buf.line_text(0) == "Xbc"


def test_backspace_moves_cursor_left():
    buf = feed("abc\b\bZ")
    assert buf.line_text(0) == "aZc"


def test_sgr_color_is_recorded():
    buf = feed("\x1b[31mred\x1b[0m normal")
    segments = buf.line_segments(0)
    red_segment = segments[0]
    assert red_segment[0].startswith("red")
    assert red_segment[1].fg == "#cd0000"
    plain = [s for s in segments if s[0].strip().startswith("normal")]
    assert plain and plain[0][1].fg == ""


def test_bold_and_underline_flags():
    buf = feed("\x1b[1;4m强调\x1b[22;24m普通")
    first = buf.line_segments(0)[0]
    assert first[1].bold and first[1].underline
    last = [s for s in buf.line_segments(0) if s[0].startswith("普通")][0]
    assert not last[1].bold and not last[1].underline


def test_clear_screen():
    buf = feed("第一屏内容\r\n\x1b[2J\x1b[H清空后")
    assert buf.line_text(0) == "清空后"
    assert buf.line_text(1) == ""


def test_cursor_position():
    buf = feed("A\r\nB\x1b[2;3H插")
    # CUP 2;3 => 第 2 行第 3 列（从 1 开始），即索引 (1, 2)，宽字符占两格
    assert buf.line_text(1) == "B 插"
    assert buf.grid[1][2][0] == "插"
    assert buf.cursor_r == 1 and buf.cursor_c == 4


def test_erase_line_to_end():
    buf = feed("abcdef\x1b[3G\x1b[K")
    assert buf.line_text(0) == "ab"


def test_chinese_width_counts_as_two_columns():
    assert char_width("中") == 2
    assert char_width("a") == 1
    buf = ScreenBuffer(10, 3)
    buf.feed("中文")
    assert buf.cursor_c == 4  # 两个字各占两列
    # 占位格不产生额外字符
    assert buf.line_text(0) == "中文"


def test_wrap_on_overflow():
    buf = feed("0123456789", cols=10)
    assert buf.line_text(0) == "0123456789"
    buf.feed("A")
    assert buf.line_text(1).startswith("A")


def test_scroll_into_scrollback():
    buf = feed("\r\n".join(str(i) for i in range(6)), cols=20, rows=3)
    assert len(buf.scrollback) >= 3
    assert buf.line_text(2) == "5"


def test_scroll_region_respected():
    buf = ScreenBuffer(20, 8)
    buf.feed("\x1b[3;6r")  # 3~6 行滚动
    buf.feed("\x1b[6;1H" + "\n" + "X")
    assert buf.line_text(0) == ""
    assert "X" in buf.visible_text()


def test_osc_sets_title():
    buf = feed("\x1b]0;root@server: ~\x07")
    assert buf.title == "root@server: ~"


def test_charset_select_sequence_swallowed():
    # nano 等全屏程序发 ESC ( B 切回 ASCII 字符集，末尾的 B 不能漏到屏幕上
    buf = feed("\x1b(B[Unit]\x1b(BDescription=demo")
    assert buf.line_text(0) == "[Unit]Description=demo"


def test_ansi256_palette():
    assert ansi256_to_hex(0) == "#000000"
    assert ansi256_to_hex(196) == "#ff0000"
    assert ansi256_to_hex(232) == "#080808"
    assert ansi256_to_hex(16) == "#000000"


def test_256_color_sgr():
    buf = feed("\x1b[38;5;196mX\x1b[0m")
    assert buf.grid[0][0][1].fg == "#ff0000"


def test_truecolor_sgr():
    buf = feed("\x1b[38;2;18;52;86mX")
    assert buf.grid[0][0][1].fg == "#123456"


def test_reverse_video():
    buf = feed("\x1b[7mX")
    assert buf.grid[0][0][1].reverse is True


def test_resize_keeps_content():
    buf = feed("abcdef", cols=10, rows=4)
    buf.resize(20, 8)
    assert buf.line_text(0) == "abcdef"
    assert buf.cols == 20 and buf.rows == 8


def test_line_segments_merges_same_attr():
    buf = feed("abcdef")
    segments = buf.line_segments(0)
    assert len(segments) == 1
    assert segments[0][0] == "abcdef"


def test_erase_clears_default_bg():
    buf = feed("\x1b[44m背景\x1b[K")
    assert buf.grid[0][2][1].bg == "#0000ee"


def test_incremental_utf8_decode():
    buf = ScreenBuffer(20, 3)
    raw = "中文".encode("utf-8")
    buf.feed(raw[:1])  # 半个汉字
    buf.feed(raw[1:])
    assert buf.line_text(0) == "中文"
