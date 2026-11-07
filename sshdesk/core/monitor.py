"""远程主机监控采集与解析。

采集用一条组合命令完成（一次 SSH 往返，避免多次握手开销），输出用 `#标签`
分节，解析逻辑与采集逻辑解耦，便于单元测试与「离线预览」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .client import SSHConnection

COLLECT_COMMAND = r"""
export LC_ALL=C LANG=C
echo '#STAT1'; head -1 /proc/stat
sleep 0.5
echo '#STAT2'; head -1 /proc/stat
echo '#LOAD'; cat /proc/loadavg 2>/dev/null
echo '#MEM'; cat /proc/meminfo 2>/dev/null
echo '#DISK'; df -kP / /home 2>/dev/null
echo '#CORES'; nproc 2>/dev/null
echo '#UP'; cut -d' ' -f1 /proc/uptime 2>/dev/null
echo '#OS'; grep -E '^PRETTY_NAME' /etc/os-release 2>/dev/null
echo '#TOP'; ps -eo pcpu,pmem,rss,comm --sort=-pcpu 2>/dev/null | head -6
echo '#SSH'; (systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null)
echo '#UNAME'; uname -r
""".strip()


@dataclass
class ProcessInfo:
    cpu_percent: float
    mem_percent: float
    rss_kb: int
    command: str


@dataclass
class Snapshot:
    cpu_percent: float = 0.0
    load1: float = 0.0
    load5: float = 0.0
    load15: float = 0.0
    cores: int = 0
    mem_total_mb: int = 0
    mem_available_mb: int = 0
    mem_percent: float = 0.0
    swap_total_mb: int = 0
    swap_used_mb: int = 0
    disk_total_gb: float = 0.0
    disk_used_gb: float = 0.0
    disk_percent: float = 0.0
    disk_mount: str = "/"
    uptime_seconds: int = 0
    os_name: str = ""
    kernel: str = ""
    ssh_service: str = ""
    processes: list[ProcessInfo] = field(default_factory=list)

    @property
    def uptime_text(self) -> str:
        days, remain = divmod(self.uptime_seconds, 86400)
        hours, remain = divmod(remain, 3600)
        minutes = remain // 60
        if days:
            return f"{days}天{hours}小时"
        if hours:
            return f"{hours}小时{minutes}分"
        return f"{minutes}分"

    @property
    def mem_text(self) -> str:
        used = self.mem_total_mb - self.mem_available_mb
        return f"{used/1024:.1f}G / {self.mem_total_mb/1024:.1f}G"

    @property
    def disk_text(self) -> str:
        return f"{self.disk_used_gb:.1f}G / {self.disk_total_gb:.1f}G"


def _sections(text: str) -> dict[str, str]:
    """把 '#TAG\\n...' 形式的输出切成 {标签: 内容}。"""
    result: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("#") and line[1:].strip().isalnum():
            if current is not None:
                result[current] = "\n".join(buf).strip()
            current = line[1:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        result[current] = "\n".join(buf).strip()
    return result


def _parse_cpu(stat1: str, stat2: str) -> float:
    """用两次 /proc/stat 采样算 CPU 使用率，比读 loadavg 准确。"""
    def numbers(line: str) -> list[int]:
        return [int(x) for x in line.split()[1:] if x.isdigit()]

    a, b = numbers(stat1), numbers(stat2)
    if len(a) < 4 or len(b) < 4:
        return 0.0
    idle_a = a[3] + (a[4] if len(a) > 4 else 0)
    idle_b = b[3] + (b[4] if len(b) > 4 else 0)
    total_a, total_b = sum(a), sum(b)
    total_delta = total_b - total_a
    idle_delta = idle_b - idle_a
    if total_delta <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)


def _parse_mem(meminfo: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in meminfo.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1])  # 单位 kB
    return values


def _parse_disk(df_text: str) -> tuple[float, float, float, str]:
    for line in df_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[-1] == "/":
            total_kb, used_kb = int(parts[1]), int(parts[2])
            total = round(total_kb / 1024 / 1024, 1)
            used = round(used_kb / 1024 / 1024, 1)
            percent = round((used_kb / total_kb * 100), 1) if total_kb else 0.0
            return total, used, percent, parts[-1]
    # 没有 / 行时退回第一块
    for line in df_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6:
            total_kb, used_kb = int(parts[1]), int(parts[2])
            return (round(total_kb / 1048576, 1), round(used_kb / 1048576, 1),
                    round(used_kb / total_kb * 100, 1) if total_kb else 0.0, parts[-1])
    return 0.0, 0.0, 0.0, "/"


def parse_snapshot(text: str) -> Snapshot:
    sec = _sections(text)
    snap = Snapshot()

    snap.cpu_percent = _parse_cpu(sec.get("STAT1", ""), sec.get("STAT2", ""))

    load = sec.get("LOAD", "").split()
    if len(load) >= 3:
        try:
            snap.load1, snap.load5, snap.load15 = (float(load[0]), float(load[1]), float(load[2]))
        except ValueError:
            pass

    mem = _parse_mem(sec.get("MEM", ""))
    snap.mem_total_mb = mem.get("MemTotal", 0) // 1024
    snap.mem_available_mb = mem.get("MemAvailable", mem.get("MemFree", 0)) // 1024
    if snap.mem_total_mb:
        used = snap.mem_total_mb - snap.mem_available_mb
        snap.mem_percent = round(used / snap.mem_total_mb * 100, 1)
    snap.swap_total_mb = mem.get("SwapTotal", 0) // 1024
    snap.swap_used_mb = max(0, (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) // 1024)

    total, used, percent, mount = _parse_disk(sec.get("DISK", ""))
    snap.disk_total_gb, snap.disk_used_gb = total, used
    snap.disk_percent, snap.disk_mount = percent, mount

    cores = sec.get("CORES", "").strip()
    snap.cores = int(cores) if cores.isdigit() else 0

    up = sec.get("UP", "").strip()
    try:
        snap.uptime_seconds = int(float(up))
    except ValueError:
        snap.uptime_seconds = 0

    os_line = sec.get("OS", "")
    match = re.search(r'PRETTY_NAME="?([^"]+)"?', os_line)
    if match:
        snap.os_name = match.group(1).strip()

    snap.kernel = sec.get("UNAME", "").strip()
    snap.ssh_service = sec.get("SSH", "").strip()

    for line in sec.get("TOP", "").splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) >= 4:
            try:
                snap.processes.append(
                    ProcessInfo(float(parts[0]), float(parts[1]), int(parts[2]), parts[3].strip())
                )
            except ValueError:
                continue
    return snap


def collect(connection: SSHConnection, timeout: int = 20) -> Snapshot:
    result = connection.exec_command(COLLECT_COMMAND, timeout=timeout)
    if result.error and not result.stdout:
        raise RuntimeError(f"采集失败：{result.error}")
    return parse_snapshot(result.stdout)


# 离线演示/测试用样例（Ubuntu 22.04 + 2C2G 学生机）
SAMPLE_OUTPUT = """#STAT1
cpu  10234 331 4021 128844 231 0 55 0 0 0
#STAT2
cpu  10290 331 4030 128900 232 0 55 0 0 0
#LOAD
0.08 0.12 0.09 1/128 4521
#MEM
MemTotal:        1989344 kB
MemFree:          842196 kB
MemAvailable:    1420488 kB
SwapTotal:             0 kB
SwapFree:              0 kB
#DISK
Filesystem     1024-blocks    Used Available Capacity Mounted on
/dev/vda1         41150000 8120000  30915000      21% /
#CORES
2
#UP
48213.55
#OS
PRETTY_NAME="Ubuntu 22.04.4 LTS"
#TOP
%CPU %MEM   RSS COMMAND
 0.3  1.2 24576 systemd
 0.1  0.8 16384 sshd
#SSH
active
#UNAME
5.15.0-119-generic"""
