"""批量执行测试：一台失败不影响整批，结果可读。"""

from __future__ import annotations

from sshdesk.core.config import ServerConfig
from sshdesk.core.executor import BatchResult, format_table, run_batch


def dead_server(name: str, port: int = 1) -> ServerConfig:
    return ServerConfig(name=name, host="127.0.0.1", username="root", port=port,
                        auth_type="agent", timeout=1)


def test_format_table_shows_status_and_summary():
    servers = [dead_server("a"), dead_server("b")]
    results = [
        BatchResult(server=servers[0], stdout="uptime ok", exit_code=0, duration=0.3),
        BatchResult(server=servers[1], error="连接超时", duration=2.0),
    ]
    table = format_table(results)
    assert "成功" in table
    assert "失败" in table
    assert "共 2 台，成功 1 台，失败 1 台" in table


def test_format_table_handles_empty():
    assert "没有执行任何主机" in format_table([])


def test_run_batch_isolates_failures():
    servers = [dead_server("坏机器1"), dead_server("坏机器2")]
    results = run_batch(servers, "uptime", max_workers=2, timeout=2)
    assert len(results) == 2
    assert all(not r.ok for r in results)
    assert all(r.error for r in results)


def test_run_batch_keeps_input_order():
    servers = [dead_server("z"), dead_server("a"), dead_server("m")]
    results = run_batch(servers, "true", max_workers=3, timeout=2)
    assert [r.server.name for r in results] == ["z", "a", "m"]


def test_run_batch_empty_list():
    assert run_batch([], "true") == []
