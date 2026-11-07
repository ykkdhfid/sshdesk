"""多服务器批量命令执行（并发 + 结果汇总）。

CLI 的 `sshdesk exec --group 生产 "uptime"` 与 GUI 的批量面板共用这一层。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable

from .client import SSHError, build_connection
from .config import ServerConfig
from .credentials import CredentialStore


@dataclass
class BatchResult:
    server: ServerConfig
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error

    @property
    def label(self) -> str:
        return self.server.name or self.server.host


ResultCallback = Callable[[BatchResult], None]


def run_on_server(
    server: ServerConfig,
    command: str,
    credentials: CredentialStore | None = None,
    timeout: int = 30,
) -> BatchResult:
    start = time.time()
    connection = build_connection(server, credentials)
    try:
        connection.connect()
        result = connection.exec_command(command, timeout=timeout)
        return BatchResult(
            server=server,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration=time.time() - start,
            error=result.error,
        )
    except SSHError as exc:
        return BatchResult(server=server, duration=time.time() - start, error=str(exc))
    finally:
        connection.close()


def run_batch(
    servers: Iterable[ServerConfig],
    command: str,
    credentials: CredentialStore | None = None,
    max_workers: int = 6,
    timeout: int = 30,
    on_result: ResultCallback | None = None,
) -> list[BatchResult]:
    """并发在每台机器上执行同一条命令；任何一台失败都不影响其它机器。"""
    targets = list(servers)
    if not targets:
        return []
    results: list[BatchResult] = []
    workers = max(1, min(max_workers, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(run_on_server, s, command, credentials, timeout): s for s in targets
        }
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:  # 兜底，绝不让单台异常炸掉整批
                result = BatchResult(server=futures[future], error=f"未预期异常：{exc}")
            results.append(result)
            if on_result:
                on_result(result)
    order = {s.id: idx for idx, s in enumerate(targets)}
    results.sort(key=lambda r: order.get(r.server.id, 999))
    return results


def format_table(results: list[BatchResult]) -> str:
    """等宽文本汇总，CLI 与日志都能直接用。"""
    if not results:
        return "（没有执行任何主机）"
    name_width = max([len(r.label) for r in results] + [6])
    lines = [f"{'主机'.ljust(name_width)}  状态   耗时     输出"]
    lines.append("-" * (name_width + 40))
    for r in results:
        status = "成功" if r.ok else "失败"
        output = (r.stdout.strip() or r.stderr.strip() or r.error).replace("\n", " ⏎ ")
        if len(output) > 120:
            output = output[:117] + "..."
        lines.append(f"{r.label.ljust(name_width)}  {status}   {r.duration:5.1f}s  {output}")
    ok_count = sum(1 for r in results if r.ok)
    lines.append("-" * (name_width + 40))
    lines.append(f"共 {len(results)} 台，成功 {ok_count} 台，失败 {len(results) - ok_count} 台")
    return "\n".join(lines)
