"""~/.ssh/config 解析测试。"""

from __future__ import annotations

from pathlib import Path

from sshdesk.core.sshconfig import (
    expand_identity_path,
    load_ssh_config,
    parse_ssh_config,
    to_server_config,
)

SAMPLE = """
# 注释行
Host prod
    HostName 47.100.1.2
    User root
    Port 2222
    IdentityFile ~/.ssh/id_ed25519

Host staging web2          # 多个别名，取第一个
    HostName staging.example.com
    User deploy

Host *
    User ops
    Port 2200

Host *.wild.example.com
    User wild

Host=inline
    HostName=10.0.0.9
    IdentityFile=%d/.ssh/id_rsa
"""


def test_parse_basic_fields():
    entries = parse_ssh_config(SAMPLE)
    prod = entries[0]
    assert prod.alias == "prod"
    assert prod.hostname == "47.100.1.2"
    assert prod.user == "root"
    assert prod.port == 2222
    assert prod.identity_file == "~/.ssh/id_ed25519"


def test_multi_alias_uses_first():
    entries = parse_ssh_config(SAMPLE)
    assert entries[1].alias == "staging"
    assert entries[1].hostname == "staging.example.com"


def test_wildcard_hosts_are_skipped():
    entries = parse_ssh_config(SAMPLE)
    names = [e.alias for e in entries]
    assert "*" not in names
    assert "*.wild.example.com" not in names


def test_defaults_from_host_star():
    entries = parse_ssh_config(SAMPLE)
    inline = entries[2]
    # inline 只写了 HostName/IdentityFile，User 和 Port 应吃到 Host * 的默认值
    assert inline.user == "ops"
    assert inline.port == 2200


def test_inline_equals_syntax():
    entries = parse_ssh_config(SAMPLE)
    inline = entries[2]
    assert inline.alias == "inline"
    assert inline.hostname == "10.0.0.9"
    assert inline.identity_file == "%d/.ssh/id_rsa"


def test_expand_identity_path():
    assert expand_identity_path("%d/.ssh/id_rsa").endswith("/.ssh/id_rsa")
    assert expand_identity_path("~/.ssh/id_rsa").startswith("/")


def test_to_server_config_falls_back_to_agent(tmp_path):
    entry = parse_ssh_config(SAMPLE)[0]
    config = to_server_config(entry)
    # 私钥是 ~/.ssh/id_ed25519 这种不一定存在的路径，不存在时应退回 agent
    if Path(expand_identity_path(entry.identity_file)).exists():
        assert config.auth_type == "key"
    else:
        assert config.auth_type == "agent"
        assert config.key_path == ""
    assert config.name == "prod"
    assert config.host == "47.100.1.2"
    assert config.port == 2222
    assert config.note.startswith("从 ~/.ssh/config 导入")


def test_to_server_config_with_real_key(tmp_path):
    key = tmp_path / "id_test"
    key.write_text("dummy", encoding="utf-8")
    entry = parse_ssh_config(f"Host box\n HostName 1.1.1.1\n IdentityFile {key}\n")[0]
    config = to_server_config(entry)
    assert config.auth_type == "key"
    assert config.key_path == str(key)


def test_load_missing_file_returns_empty(tmp_path):
    assert load_ssh_config(tmp_path / "nope") == []


def test_load_from_file(tmp_path):
    path = tmp_path / "config"
    path.write_text(SAMPLE, encoding="utf-8")
    assert len(load_ssh_config(path)) == 3


def test_empty_and_comment_only():
    assert parse_ssh_config("") == []
    assert parse_ssh_config("# nothing\n\n   \n") == []
