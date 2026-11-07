"""监控解析测试：不依赖真实 SSH，纯文本进、结构出。"""

from __future__ import annotations

import pytest

from sshdesk.core.monitor import SAMPLE_OUTPUT, Snapshot, _sections, parse_snapshot


def test_sections_split():
    text = "#A\n1\n2\n#B\n3"
    assert _sections(text) == {"A": "1\n2", "B": "3"}


def test_parse_sample_snapshot():
    snap = parse_snapshot(SAMPLE_OUTPUT)
    assert snap.cores == 2
    assert snap.mem_total_mb == 1942
    assert snap.os_name == "Ubuntu 22.04.4 LTS"
    assert snap.kernel == "5.15.0-119-generic"
    assert snap.ssh_service == "active"
    assert snap.disk_mount == "/"
    assert snap.disk_percent == pytest.approx(19.7, abs=0.2)
    assert 30 <= snap.cpu_percent <= 70  # 样例里约 53%
    assert snap.load1 == pytest.approx(0.08)


def test_uptime_text():
    snap = Snapshot(uptime_seconds=48213)
    assert snap.uptime_text == "13小时23分"
    snap2 = Snapshot(uptime_seconds=90000)
    assert snap2.uptime_text == "1天1小时"


def test_mem_and_disk_text():
    snap = Snapshot(mem_total_mb=2048, mem_available_mb=1024,
                    disk_total_gb=40.0, disk_used_gb=10.0)
    assert snap.mem_text == "1.0G / 2.0G"
    assert snap.disk_text == "10.0G / 40.0G"


def test_processes_parsed():
    snap = parse_snapshot(SAMPLE_OUTPUT)
    assert snap.processes
    assert snap.processes[0].command == "systemd"


def test_empty_output_does_not_crash():
    snap = parse_snapshot("")
    assert snap.cpu_percent == 0.0
    assert snap.cores == 0
    assert snap.processes == []


def test_garbage_lines_are_tolerated():
    snap = parse_snapshot("#CORES\nnot-a-number\n#TOP\n%CPU %MEM RSS COMMAND\n坏行")
    assert snap.cores == 0
    assert snap.processes == []


def test_cpu_calculation_ignores_zero_delta():
    snap = parse_snapshot("#STAT1\ncpu 1 1 1 1\n#STAT2\ncpu 1 1 1 1")
    assert snap.cpu_percent == 0.0
