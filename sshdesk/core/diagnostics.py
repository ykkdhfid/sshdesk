"""连接诊断：把「连不上」拆成可执行的检查项。

设计动机：SSH 连不上时，用户看到的只有一句 timed out，根本不知道该查哪里。
阿里云 ECS 上最常见的原因是「实例没有分配公网 IP / 公网带宽为 0」（学生机、
特惠机默认如此），此时本机无论怎么试都连不上，但控制台 Workbench（走内网
通道）却一切正常，非常容易被误判成代码或密码问题。
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field

from .client import SSHConnection
from .config import ServerConfig

# 阿里云控制台里「实例详情」的操作路径，写死在提示里比让用户自己搜更省事
ALIYUN_NO_PUBLIC_IP_HINT = (
    "实例没有公网 IP。到阿里云控制台 → 实例详情 → 「分配公网 IP」/「绑定弹性公网 IP」，"
    "计费方式选「按使用流量」，峰值带宽 1~3 Mbps 足够 SSH 使用（约 0.8 元/GB）。"
    "在开通前可以先用控制台自带的 Workbench 网页终端（走内网通道，免费且立即可用）。"
)
ALIYUN_SECURITY_GROUP_HINT = (
    "检查安全组入方向规则：需要放行 TCP 22（或你自定义的 SSH 端口），"
    "授权对象建议只填你自己的公网出口 IP，不要用 0.0.0.0/0。"
)
UBUNTU_FIREWALL_HINT = (
    "Ubuntu 自带 ufw 也可能拦：在服务器上执行 sudo ufw status，"
    "若为 active 且没有 22/tcp，执行 sudo ufw allow 22/tcp。"
)
SSHD_HINT = (
    "确认 sshd 在跑并在监听：sudo systemctl status ssh && sudo ss -tlnp | grep :22。"
    "改过 sshd_config 记得 sudo systemctl restart ssh。"
)


@dataclass
class ProbeResult:
    host: str
    port: int
    ok: bool
    latency_ms: float = 0.0
    error: str = ""
    resolved_ip: str = ""


@dataclass
class CheckItem:
    name: str
    status: str  # ok | warn | error
    detail: str = ""


@dataclass
class Report:
    target: str
    level: str = "ok"  # ok | warn | error
    items: list[CheckItem] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.items.append(CheckItem(name, status, detail))
        rank = {"ok": 0, "warn": 1, "error": 2}
        if rank[status] > rank[self.level]:
            self.level = status

    def suggest(self, text: str) -> None:
        if text and text not in self.suggestions:
            self.suggestions.append(text)

    def to_text(self) -> str:
        icons = {"ok": "✅", "warn": "⚠️", "error": "❌"}
        lines = [f"诊断目标：{self.target}", f"总体结论：{icons[self.level]} {self.level.upper()}"]
        for item in self.items:
            lines.append(f"  {icons[item.status]} {item.name}" + (f"：{item.detail}" if item.detail else ""))
        if self.suggestions:
            lines.append("建议：")
            lines.extend(f"  {i+1}. {s}" for i, s in enumerate(self.suggestions))
        return "\n".join(lines)


def is_private_host(host: str) -> bool:
    """判断是否私网/保留地址（100.64/10 是运营商级 NAT，也归入不可直连）。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # 域名，交给 DNS 判断
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return True
    if ip.is_private:
        return True
    return ip in ipaddress.ip_network("100.64.0.0/10")


def probe_tcp(host: str, port: int = 22, timeout: float = 5.0) -> ProbeResult:
    start = time.time()
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        resolved = infos[0][4][0] if infos else ""
    except socket.gaierror as exc:
        return ProbeResult(host, port, False, error=f"域名解析失败：{exc}")
    sock = socket.socket(socket.AF_INET6 if ":" in resolved else socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((resolved, port))
        # 尝试读取 SSH banner：能读到 "SSH-2.0-..." 说明对端确实是 sshd，而不是别的服务占用端口
        banner = b""
        try:
            sock.settimeout(2.0)
            banner = sock.recv(64)
        except OSError:
            pass
        latency = round((time.time() - start) * 1000, 1)
        if banner.startswith(b"SSH-"):
            return ProbeResult(host, port, True, latency, resolved_ip=resolved)
        return ProbeResult(
            host, port, True, latency, resolved_ip=resolved,
            error="端口开放但未返回 SSH 欢迎语，可能不是 sshd 或中间有代理",
        )
    except socket.timeout:
        return ProbeResult(host, port, False, error="TCP 连接超时（数据包没能到达主机或没有回包）",
                           resolved_ip=resolved)
    except ConnectionRefusedError:
        return ProbeResult(host, port, False, error="对方明确拒绝连接（端口无监听或安全组拒绝）",
                           resolved_ip=resolved)
    except OSError as exc:
        return ProbeResult(host, port, False, error=str(exc), resolved_ip=resolved)
    finally:
        sock.close()


def diagnose(server: ServerConfig, timeout: float = 6.0) -> Report:
    """不登录、只做网络层诊断，因此服务器没开公网 IP 也能跑出结论。"""
    report = Report(target=f"{server.host}:{server.port}")
    private = is_private_host(server.host)

    if private:
        report.add("地址类型", "warn", f"{server.host} 是私网地址，只能在同 VPC / VPN / 跳板机内访问")
        report.suggest(
            "客户端在你自己的电脑上运行，无法路由到私网地址。要么给实例分配公网 IP / 绑定 EIP，"
            "要么通过跳板机（本项目支持 jump_host）或 VPN 接入。"
        )

    probe = probe_tcp(server.host, server.port, timeout)
    if probe.ok and not probe.error:
        report.add("SSH 端口", "ok", f"{probe.latency_ms} ms，SSH 服务已响应")
    elif probe.ok:
        report.add("SSH 端口", "warn", probe.error)
        report.suggest("端口被其它服务占用时，把实例的 SSH 端口改成实际端口，或排查该服务。")
    else:
        report.add("SSH 端口", "error", probe.error or "无法连接")
        if "超时" in probe.error or "timed out" in probe.error.lower():
            if private or not probe.resolved_ip:
                report.suggest(ALIYUN_NO_PUBLIC_IP_HINT)
            report.suggest(ALIYUN_SECURITY_GROUP_HINT)
            report.suggest(UBUNTU_FIREWALL_HINT)
        elif "拒绝" in probe.error or "refused" in probe.error.lower():
            report.suggest(SSHD_HINT)
            report.suggest(UBUNTU_FIREWALL_HINT)

    if server.auth_type == "key":
        report.add("认证方式", "ok", f"密钥 {server.key_path}")
    elif server.auth_type == "password":
        report.add("认证方式", "warn", "密码认证：Ubuntu 默认允许，但建议改用密钥并关闭密码登录")
        report.suggest(
            "在服务器上确认 /etc/ssh/sshd_config 中 PasswordAuthentication yes；"
            "若只为省事，可用 ssh-keygen + ssh-copy-id 换成密钥登录，安全性更高。"
        )
    else:
        report.add("认证方式", "ok", "SSH Agent")

    if server.jump_host:
        report.add("跳板机", "ok", server.jump_host)

    return report


REMOTE_CHECK_COMMAND = r"""
export LC_ALL=C
echo '#SSH'; (systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null)
echo '#LISTEN'; ss -tlnp 2>/dev/null | grep -E ':(22|%s)\b' | head -2
echo '#UFW'; (ufw status 2>/dev/null | head -4) || echo 'ufw 未安装'
echo '#SSHDCONF'; (grep -REi '^(PasswordAuthentication|PermitRootLogin|PubkeyAuthentication|Port)' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/ 2>/dev/null | head -8)
echo '#FAIL2BAN'; (systemctl is-active fail2ban 2>/dev/null || echo '未安装')
echo '#DISK'; df -h / 2>/dev/null | tail -1
echo '#MEM'; free -m 2>/dev/null | sed -n '2p'
echo '#SWAP'; free -m 2>/dev/null | sed -n '3p'
echo '#KERNEL'; uname -sr
""".strip()


def remote_selfcheck(connection: SSHConnection, port: int = 22) -> Report:
    """登录成功后的体检：看服务、防火墙、认证配置、资源水位。"""
    report = Report(target=connection.server.endpoint)
    result = connection.exec_command(REMOTE_CHECK_COMMAND % port, timeout=20)
    if result.error and not result.stdout:
        report.add("命令执行", "warn", result.error)
        return report

    sections = _split_sections(result.stdout)

    ssh_state = sections.get("SSH", "").strip()
    report.add("sshd 状态", "ok" if ssh_state == "active" else "error", ssh_state or "未检测到")

    listen = sections.get("LISTEN", "").strip()
    report.add("端口监听", "ok" if listen else "error", listen.splitlines()[0] if listen else "未发现监听")

    ufw = sections.get("UFW", "").lower()
    if "inactive" in ufw or "未安装" in ufw or "not installed" in ufw:
        report.add("ufw 防火墙", "ok", "未启用或不影响 22 端口")
    elif "22/tcp" in ufw and "allow" in ufw:
        report.add("ufw 防火墙", "ok", "已放行 22/tcp")
    elif "active" in ufw:
        report.add("ufw 防火墙", "warn", "已启用但未明确看到放行 22/tcp")
        report.suggest(UBUNTU_FIREWALL_HINT)

    conf = sections.get("SSHDCONF", "").lower()
    if "passwordauthentication no" in conf and connection.server.auth_type == "password":
        report.add("密码登录", "error", "sshd 已禁用密码登录，但当前配置用的是密码")
        report.suggest("改用密钥认证，或在 sshd_config 里改为 PasswordAuthentication yes 后重启 ssh。")
    elif "permitrootlogin yes" in conf or "permitrootlogin without-password" in conf:
        report.add("root 登录", "warn", "允许 root 直接登录，建议改为 prohibit-password")

    fail2ban = sections.get("FAIL2BAN", "").lower()
    report.add("fail2ban", "ok" if fail2ban == "active" else "warn",
               "已启用" if fail2ban == "active" else "未启用（暴力破解防护缺位）")
    if fail2ban != "active":
        report.suggest("可选加固：sudo apt install fail2ban，并只允许密钥登录。")

    disk = sections.get("DISK", "").strip()
    if disk:
        parts = disk.split()
        if len(parts) >= 5:
            percent = int(parts[4].rstrip("%")) if parts[4].rstrip("%").isdigit() else 0
            report.add("磁盘 /", "warn" if percent >= 85 else "ok", f"{parts[2]} 已用 / {parts[1]}（{parts[4]}）")
            if percent >= 85:
                report.suggest("磁盘水位偏高：清理 apt 缓存（sudo apt clean）并检查 /var/log 日志。")

    mem_line = sections.get("MEM", "").split()
    if len(mem_line) >= 4:
        total, used = int(mem_line[1]), int(mem_line[2])
        report.add("内存", "warn" if total and used / total > 0.85 else "ok", f"{used}M / {total}M")
    swap_line = sections.get("SWAP", "").split()
    if len(swap_line) >= 4 and int(swap_line[1]) == 0:
        report.add("Swap", "warn", "未配置 swap，2G 小内存机器容易 OOM")
        report.suggest("2G 内存实例建议加 2G swap：sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && "
                       "sudo mkswap /swapfile && sudo swapon /swapfile（再写入 /etc/fstab 持久化）。")

    kernel = sections.get("KERNEL", "").strip()
    if kernel:
        report.add("内核", "ok", kernel)
    return report


def _split_sections(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("#") and line[1:].strip().isalnum():
            if current is not None:
                result[current] = "\n".join(buf).strip()
            current, buf = line[1:].strip(), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        result[current] = "\n".join(buf).strip()
    return result
