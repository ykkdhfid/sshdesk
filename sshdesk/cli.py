"""命令行入口：无图形界面也能完成连接、诊断、批量执行、监控、传文件。

用法示例：
    python -m sshdesk list
    python -m sshdesk add -n 阿里云成都 -H 47.108.x.x -u ubuntu -a key -k ~/.ssh/id_ed25519
    python -m sshdesk test 阿里云成都
    python -m sshdesk exec 阿里云成都 "df -h"
    python -m sshdesk exec --group 生产 "systemctl status nginx"
    python -m sshdesk monitor 阿里云成都 --watch 3
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from .core.audit import AuditLogger
from .core.client import SSHConnection, SSHError, build_connection
from .core.config import ConfigStore, ServerConfig
from .core.credentials import CredentialError, CredentialStore
from .core.diagnostics import diagnose, remote_selfcheck
from .core.executor import format_table, run_batch
from .core.monitor import collect
from .core.sftp import SFTPBrowser

OK, WARN, ERR = "✅", "⚠️", "❌"


def _store() -> ConfigStore:
    return ConfigStore()


def _credentials() -> CredentialStore:
    return CredentialStore()


def _resolve_server(store: ConfigStore, name: str) -> ServerConfig:
    server = store.find_by_name(name) or store.get(name)
    if not server:
        names = "、".join(s.name for s in store.all()) or "（暂无）"
        raise SystemExit(f"找不到服务器「{name}」，已保存的有：{names}")
    return server


def _password_for(server: ServerConfig, args: argparse.Namespace, creds: CredentialStore) -> str | None:
    if server.auth_type != "password":
        return None
    if getattr(args, "password", None):
        return args.password
    env = os.environ.get("SSHDESK_PASSWORD")
    if env:
        return env
    try:
        return creds.get_password(server.password_ref)
    except CredentialError:
        return None


def _connect(server: ServerConfig, args: argparse.Namespace, trust: bool = False) -> SSHConnection:
    creds = _credentials()
    auto_trust = trust or getattr(args, "trust_host", False)

    def on_unknown(hostname: str, key_type: str, fingerprint: str) -> bool:
        print(f"首次连接 {hostname}，主机密钥指纹：{fingerprint}（{key_type}）")
        if auto_trust:
            print("已按 --trust-host 自动信任")
            return True
        if not sys.stdin.isatty():
            raise SystemExit("非交互环境无法确认主机指纹，请加 --trust-host（确认该指纹可信后）")
        answer = input("是否信任并记录该指纹？[y/N] ").strip().lower()
        return answer in ("y", "yes")

    connection = build_connection(server, creds, on_unknown)
    password = _password_for(server, args, creds)
    connection.connect(password=password)
    return connection


def _pad(text: str, width: int) -> str:
    """按终端显示宽度补空格：中文/全角算 2 列，避免表格错位。"""
    from unicodedata import east_asian_width

    w = sum(2 if east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - w)


# ---------------------------------------------------------------------- 命令
def cmd_list(args: argparse.Namespace) -> int:
    store = _store()
    servers = store.all()
    if not servers:
        print("还没有服务器。用 `python -m sshdesk add` 添加一台，或运行 `python -m sshdesk gui`。")
        return 0
    print(_pad("名称", 16) + _pad("地址", 28) + _pad("认证", 10) + _pad("分组", 10) + "最后连接")
    print("-" * 78)
    for s in servers:
        last = time.strftime("%m-%d %H:%M", time.localtime(s.last_connected_at)) if s.last_connected_at else "从未"
        print(_pad(s.name, 16) + _pad(s.endpoint, 28) + _pad(s.auth_type, 10)
              + _pad(s.group, 10) + last)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    store = _store()
    creds = _credentials()
    if args.name is None:
        # 没有参数时走交互式向导，降低手写参数的门槛
        args.name = input("服务器名称（如 阿里云成都）：").strip()
        args.host = input("公网 IP 或域名：").strip()
        args.username = input("登录用户名 [root]：").strip() or "root"
        port_raw = input("端口 [22]：").strip() or "22"
        args.port = int(port_raw)
        auth = input("认证方式 key/password/agent [key]：").strip() or "key"
        args.auth = auth
        if auth == "key":
            args.key_path = input("私钥路径 [~/.ssh/id_ed25519]：").strip() or "~/.ssh/id_ed25519"

    server = ServerConfig(
        name=args.name,
        host=args.host,
        username=args.username,
        port=args.port,
        auth_type=args.auth,
        key_path=args.key_path or "",
        group=args.group,
    )
    try:
        server.validate()
    except ValueError as exc:
        print(f"{ERR} {exc}")
        return 1
    if server.auth_type == "password":
        password = getattr(args, "password", None) or os.environ.get("SSHDESK_PASSWORD")
        if password:
            server.password_ref = f"pw-{server.id}"
            creds.set_password(server.password_ref, password)
            print("密码已加密保存到本地凭据库")
        else:
            print(f"{WARN} 未提供密码，连接时需用 --password 或环境变量 SSHDESK_PASSWORD")
    store.upsert(server)
    AuditLogger().log("add_server", server.endpoint, ok=True)
    print(f"{OK} 已添加：{server.name} -> {server.endpoint}")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    store = _store()
    server = _resolve_server(store, args.name)
    store.delete(server.id)
    if server.password_ref:
        _credentials().delete_password(server.password_ref)
    print(f"{OK} 已删除：{server.name}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    store = _store()
    server = _resolve_server(store, args.name)
    print(diagnose(server).to_text())
    if args.login:
        try:
            conn = _connect(server, args)
        except SSHError as exc:
            print(f"\n{ERR} 登录失败：{exc}")
            return 1
        try:
            print("\n" + remote_selfcheck(conn, server.port).to_text())
        finally:
            conn.close()
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    store = _store()
    creds = _credentials()
    if args.group:
        targets = [s for s in store.all() if s.group == args.group]
    elif args.all:
        targets = store.all()
    else:
        if not args.name:
            raise SystemExit("请指定服务器名称，或用 --group / --all")
        targets = [_resolve_server(store, args.name)]
    if not targets:
        print("没有匹配的服务器")
        return 1
    results = run_batch(targets, args.command, credentials=creds, max_workers=args.workers)
    print(format_table(results))
    AuditLogger().log("batch_exec", args.group or args.name or "all", args.command,
                      ok=all(r.ok for r in results))
    return 0 if all(r.ok for r in results) else 2


def cmd_monitor(args: argparse.Namespace) -> int:
    store = _store()
    server = _resolve_server(store, args.name)
    rounds = 0
    while True:
        try:
            conn = _connect(server, args)
        except SSHError as exc:
            print(f"{ERR} {exc}")
            return 1
        try:
            snap = collect(conn)
        finally:
            conn.close()
        bar = lambda p: "█" * int(p / 5) + "░" * (20 - int(p / 5))  # noqa: E731
        print(f"\n{server.name}  {snap.os_name}  内核 {snap.kernel}  SSH {snap.ssh_service}")
        print(f"CPU   {snap.cpu_percent:5.1f}% {bar(snap.cpu_percent)}  负载 {snap.load1}/{snap.load5}/{snap.load15}  {snap.cores} 核")
        print(f"内存  {snap.mem_percent:5.1f}% {bar(snap.mem_percent)}  {snap.mem_text}")
        print(f"磁盘  {snap.disk_percent:5.1f}% {bar(snap.disk_percent)}  {snap.disk_text}  挂载 {snap.disk_mount}")
        print(f"运行  {snap.uptime_text}")
        if snap.processes:
            print("进程  " + " | ".join(f"{p.command}({p.cpu_percent}%)" for p in snap.processes[:4]))
        rounds += 1
        if not args.watch or rounds >= args.times:
            break
        time.sleep(args.watch)
    return 0


def cmd_sftp(args: argparse.Namespace) -> int:
    store = _store()
    server = _resolve_server(store, args.name)
    conn = _connect(server, args)
    try:
        browser = SFTPBrowser(conn)
        path = args.path or "."
        if args.action == "ls":
            print(f"目录：{path}")
            for e in browser.listdir(path):
                kind = "d" if e.is_dir else "-"
                print(f"{kind} {e.perms:<11} {e.size_text:>10}  {e.mtime_text}  {e.name}")
        elif args.action == "put":
            browser.upload(args.local, args.remote)
            print(f"{OK} 已上传 {args.local} -> {args.remote}")
            AuditLogger().log("sftp_put", server.endpoint, f"{args.local} -> {args.remote}")
        elif args.action == "get":
            browser.download(args.remote, args.local)
            print(f"{OK} 已下载 {args.remote} -> {args.local}")
            AuditLogger().log("sftp_get", server.endpoint, f"{args.remote} -> {args.local}")
    finally:
        conn.close()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from .core.audit import read_log

    for entry in read_log(limit=args.limit):
        print(f"{entry.get('ts')}  {entry.get('event'):<12} {entry.get('target', ''):<24} "
              f"{'OK' if entry.get('ok') else 'FAIL'}  {entry.get('detail', '')[:80]}")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .ui.app import run

    return run(demo=args.demo)


def cmd_import_ssh(args: argparse.Namespace) -> int:
    from .core.sshconfig import DEFAULT_GROUP, load_ssh_config, to_server_config

    store = _store()
    entries = load_ssh_config(args.path)
    if not entries:
        print(f"没有读到可导入的主机。检查 {args.path or '~/.ssh/config'} 是否存在。")
        return 0

    added = skipped = 0
    for entry in entries:
        existing = store.find_by_name(entry.alias)
        if existing and not args.force:
            skipped += 1
            print(f"跳过 {entry.alias}：已存在同名配置（加 --force 覆盖）")
            continue
        config = to_server_config(entry, group=args.group or DEFAULT_GROUP)
        if existing:
            store.delete(existing.id)
        if args.dry_run:
            print(f"[预览] {config.name:<18}{config.endpoint:<26}{config.auth_type}")
            continue
        store.upsert(config)
        added += 1
        print(f"已导入 {config.name:<18}{config.endpoint:<26}{config.auth_type}")

    if args.dry_run:
        print(f"共 {len(entries)} 条可导入")
    else:
        print(f"完成：新增 {added} 台，跳过 {skipped} 台")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web.server import main as web_main

    argv = ["--host", args.host, "--port", str(args.port)]
    if args.token:
        argv += ["--token", args.token]
    return web_main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshdesk", description="本地桌面 SSH 运维客户端")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出已保存的服务器")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("add", help="添加服务器")
    p.add_argument("-n", "--name")
    p.add_argument("-H", "--host")
    p.add_argument("-u", "--username", default="root")
    p.add_argument("-p", "--port", type=int, default=22)
    p.add_argument("-a", "--auth", default="key", choices=["key", "password", "agent"])
    p.add_argument("-k", "--key-path", default="")
    p.add_argument("-g", "--group", default="默认")
    p.add_argument("--password", help="密码（会加密保存；也可用环境变量 SSHDESK_PASSWORD）")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("rm", help="删除服务器")
    p.add_argument("name")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("test", help="连通性诊断")
    p.add_argument("name")
    p.add_argument("--login", action="store_true", help="连通后继续做服务器体检")
    p.add_argument("--password")
    p.add_argument("--trust-host", action="store_true")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("exec", help="执行命令（支持批量）")
    p.add_argument("name", nargs="?")
    p.add_argument("command")
    p.add_argument("--group")
    p.add_argument("--all", action="store_true")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--password")
    p.add_argument("--trust-host", action="store_true")
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("monitor", help="采集主机监控")
    p.add_argument("name")
    p.add_argument("--watch", type=int, default=0, help="每隔 N 秒刷新")
    p.add_argument("--times", type=int, default=5)
    p.add_argument("--password")
    p.add_argument("--trust-host", action="store_true")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("sftp", help="文件传输")
    p.add_argument("action", choices=["ls", "put", "get"])
    p.add_argument("name")
    p.add_argument("path", nargs="?", default="")
    p.add_argument("--local", default="")
    p.add_argument("--remote", default="")
    p.add_argument("--password")
    p.add_argument("--trust-host", action="store_true")
    p.set_defaults(func=cmd_sftp)

    p = sub.add_parser("audit", help="查看审计日志")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("gui", help="启动图形界面")
    p.add_argument("--demo", action="store_true", help="用内置演示数据启动（无需真实服务器）")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("import-ssh", help="从 ~/.ssh/config 导入服务器")
    p.add_argument("--path", default="", help="指定 ssh config 路径")
    p.add_argument("--group", default="", help="导入后的分组名")
    p.add_argument("--force", action="store_true", help="覆盖同名配置")
    p.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    p.set_defaults(func=cmd_import_ssh)

    p = sub.add_parser("web", help="启动浏览器版终端（手机也能用）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--token", default="", help="访问令牌，留空则读取/生成本地 token")
    p.set_defaults(func=cmd_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except SSHError as exc:
        print(f"{ERR} {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())
