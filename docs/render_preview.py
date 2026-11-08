"""把 ScreenBuffer 渲染成 PNG（用于文档配图，也验证 ANSI 解析是否正确）。

不依赖 tkinter，因此在无显示环境里也能验收终端渲染效果。
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sshdesk.core.terminal_screen import ScreenBuffer, char_width  # noqa: E402

BG = "#04060d"
DEFAULT_FG = "#e6eaf5"
# 字体按平台选：Linux 用系统自带的 DejaVu / Noto，Windows 用 Consolas / 雅黑。
# 之前写死了 Linux 路径，在 Windows 上跑会拿不到字体、退回点阵字体，图就糊了。
if sys.platform == "win32":
    MONO_CANDIDATES = [
        r"C:\Windows\Fonts\CascadiaMono.ttf",   # 含圆角制表符，观感最接近终端
        r"C:\Windows\Fonts\consola.ttf",
        r"C:\Windows\Fonts\msgothic.ttc",
    ]
    CJK_CANDIDATES = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc"]
else:
    MONO_CANDIDATES = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]
    CJK_CANDIDATES = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]


def _first_font(candidates):
    for path in candidates:
        if Path(path).exists():
            return path
    return candidates[0]


MONO = _first_font(MONO_CANDIDATES)
CJK = _first_font(CJK_CANDIDATES)

DEMO = (
    "\x1b[2J\x1b[H"
    "\x1b[38;5;114m╭────────────────────────────────────────────────────────────────────╮\x1b[0m\r\n"
    "\x1b[38;5;114m│\x1b[0m  \x1b[1mSSHDesk\x1b[0m  ·  本地桌面 SSH 运维客户端                    \x1b[38;5;114m│\x1b[0m\r\n"
    "\x1b[38;5;114m│\x1b[0m  Ubuntu 22.04.4 LTS  ·  2 vCPU  ·  2 GiB  ·  成都             \x1b[38;5;114m│\x1b[0m\r\n"
    "\x1b[38;5;114m╰────────────────────────────────────────────────────────────────────╯\x1b[0m\r\n\r\n"
    "\x1b[38;5;244mLast login: Sat Nov  8 09:12:41 2026 from 172.16.0.1\x1b[0m\r\n"
    "root@demo-host:~# \x1b[1mneofetch\x1b[0m\r\n"
    "  \x1b[38;5;39mOS\x1b[0m      Ubuntu 22.04.4 LTS x86_64\r\n"
    "  \x1b[38;5;39mHost\x1b[0m    Alibaba Cloud ecs.e-c1m1.large\r\n"
    "  \x1b[38;5;39mKernel\x1b[0m  5.15.0-119-generic\r\n"
    "  \x1b[38;5;39mCPU\x1b[0m     Intel Xeon Platinum (2) @ 2.5GHz\r\n"
    "  \x1b[38;5;39mMemory\x1b[0m  612MiB / 1944MiB\r\n"
    "  \x1b[38;5;39mDisk\x1b[0m    7.8G / 39.3G (21%)\r\n\r\n"
    "root@demo-host:~# \x1b[1msystemctl status ssh --no-pager\x1b[0m\r\n"
    "\x1b[32m●\x1b[0m ssh.service - OpenBSD Secure Shell server\r\n"
    "     Loaded: loaded (/lib/systemd/system/ssh.service; enabled)\r\n"
    "     \x1b[32mActive: \x1b[1mactive (running)\x1b[0m since Sat 2026-11-08 07:30:11 CST\r\n"
    "     Docs: man:sshd(8)\r\n\r\n"
    "root@demo-host:~# \x1b[1mtop -bn1 | head -5\x1b[0m\r\n"
    "top - 09:15:02 up 13:23,  1 user,  load average: 0.08, 0.12, 0.09\r\n"
    "Tasks:  98 total,   1 running,  97 sleeping,   0 stopped,   0 zombie\r\n"
    "\x1b[38;5;203m%Cpu(s):  1.3 us,\x1b[0m  0.7 sy,  0.0 ni, \x1b[38;5;114m97.8 id\x1b[0m\r\n\r\n"
    "root@demo-host:~# "
)


def render(buffer: ScreenBuffer, cw: int = None, line_h: int = None,
           pad: int = 18, size: int = 16) -> Image.Image:
    try:
        mono = ImageFont.truetype(MONO, size)
        cjk = ImageFont.truetype(CJK, int(size * 1.15), index=0)  # 中文占两格，字号按 2 格宽取
    except OSError:
        mono = cjk = ImageFont.load_default()
    # 格宽必须等于等宽字体的实际字宽。写死 11 的时候，16px 的 "─" 只有 8~9px，
    # 每格之间留缝，整条框线看着就是断续的虚线。
    if cw is None:
        cw = max(6, int(round(mono.getlength("M"))))
    if line_h is None:
        line_h = max(10, int(round(size * 1.35)))
    width = pad * 2 + buffer.cols * cw
    height = pad * 2 + buffer.rows * line_h
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    for row in range(buffer.rows):
        y = pad + row * line_h
        for col, (ch, attr) in enumerate(buffer.grid[row]):
            if not ch:
                continue
            x = pad + col * cw
            if attr.bg:
                draw.rectangle([x, y, x + cw - 1, y + line_h - 1], fill=attr.bg)
            fg = attr.fg or DEFAULT_FG
            if attr.reverse:
                draw.rectangle([x, y, x + cw * max(1, char_width(ch)) - 1, y + line_h - 1], fill=fg)
                fg = BG
            font = mono if ord(ch) < 128 else cjk
            if ch == "─":
                # 框线的横线在不同字体里的墨迹位置不一致（常偏上/偏下，甚至只画出一小段）。
                # 直接用几何线铺满整格，框线才是连续的，也不会随字体变。
                draw.line([(x, y + line_h // 2), (x + cw - 1, y + line_h // 2)], fill=fg)
            elif ch == "│":
                # 同理：字形高度只有字号那么大，行距比它高，竖线会断成一节一节
                draw.line([(x + cw // 2, y), (x + cw // 2, y + line_h - 1)], fill=fg)
            elif ch in "╭╮╰╯":
                # 圆角也不能交给字体：字体的竖线贴着格子边缘，跟上面格心的竖线错开几像素
                cx, cy = x + cw // 2, y + line_h // 2
                if ch in "╭╮":
                    draw.line([(cx, y), (cx, cy)], fill=fg)
                else:
                    draw.line([(cx, cy), (cx, y + line_h - 1)], fill=fg)
                if ch in "╭╰":
                    draw.line([(cx, cy), (x + cw - 1, cy)], fill=fg)
                else:
                    draw.line([(x, cy), (cx, cy)], fill=fg)
            else:
                draw.text((x, y + 1), ch, font=font, fill=fg)

    # 光标
    cx = pad + buffer.cursor_c * cw
    cy = pad + buffer.cursor_r * line_h
    draw.rectangle([cx, cy, cx + cw - 1, cy + line_h - 1], fill="#a78bfa")
    return image


def main() -> None:
    buffer = ScreenBuffer(72, 27)
    buffer.feed(DEMO)
    out = Path(__file__).parent / "terminal-preview.png"
    render(buffer).save(out)
    print(f"已生成 {out}（{buffer.cols}x{buffer.rows}）")


if __name__ == "__main__":
    main()
