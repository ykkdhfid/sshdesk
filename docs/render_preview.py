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
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

DEMO = (
    "\x1b[2J\x1b[H"
    "\x1b[38;5;114m╭────────────────────────────────────────────────────────────────────╮\x1b[0m\r\n"
    "\x1b[38;5;114m│\x1b[0m  \x1b[1mSSHDesk\x1b[0m  ·  本地桌面 SSH 运维客户端                    \x1b[38;5;114m│\x1b[0m\r\n"
    "\x1b[38;5;114m│\x1b[0m  Ubuntu 22.04.4 LTS  ·  2 vCPU  ·  2 GiB  ·  成都             \x1b[38;5;114m│\x1b[0m\r\n"
    "\x1b[38;5;114m╰────────────────────────────────────────────────────────────────────╯\x1b[0m\r\n\r\n"
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
    "     \x1b[32mActive: \x1b[1mactive (running)\x1b[0m since Sat 2026-11-08 07:30:11 CST\r\n"
    "     Docs: man:sshd(8)\r\n\r\n"
    "root@iZbam4b688h2pd:~# \x1b[1mtop -bn1 | head -5\x1b[0m\r\n"
    "top - 09:15:02 up 13:23,  1 user,  load average: 0.08, 0.12, 0.09\r\n"
    "Tasks:  98 total,   1 running,  97 sleeping,   0 stopped,   0 zombie\r\n"
    "\x1b[38;5;203m%Cpu(s):  1.3 us,\x1b[0m  0.7 sy,  0.0 ni, \x1b[38;5;114m97.8 id\x1b[0m\r\n\r\n"
    "root@iZbam4b688h2pd:~# "
)


def render(buffer: ScreenBuffer, cw: int = 11, line_h: int = 22, pad: int = 18) -> Image.Image:
    width = pad * 2 + buffer.cols * cw
    height = pad * 2 + buffer.rows * line_h
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    try:
        mono = ImageFont.truetype(MONO, 16)
        cjk = ImageFont.truetype(CJK, 19, index=0)  # 中文占两格，字号按 2 格宽取
    except OSError:
        mono = cjk = ImageFont.load_default()

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
