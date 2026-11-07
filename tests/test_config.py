"""配置层测试：CRUD、原子写入、损坏恢复、校验。"""

from __future__ import annotations

import json

import pytest

from sshdesk.core.config import ConfigStore, ServerConfig, app_dir, parse_jump


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHDESK_HOME", str(tmp_path))
    return ConfigStore(tmp_path / "servers.json")


def make_server(**kwargs) -> ServerConfig:
    base = dict(name="成都测试机", host="47.108.1.1", username="root",
                auth_type="agent", group="生产", tags=["nginx"])
    base.update(kwargs)
    return ServerConfig(**base)


def test_upsert_and_reload(store):
    server = make_server()
    store.upsert(server)
    assert store.get(server.id).name == "成都测试机"
    reloaded = ConfigStore(store.path)
    assert len(reloaded.all()) == 1
    assert reloaded.all()[0].tags == ["nginx"]


def test_update_in_place(store):
    server = make_server()
    store.upsert(server)
    server.name = "改名后"
    store.upsert(server)
    assert len(store.all()) == 1
    assert store.get(server.id).name == "改名后"


def test_delete(store):
    server = make_server()
    store.upsert(server)
    assert store.delete(server.id) is True
    assert store.all() == []
    assert store.delete("不存在的id") is False


def test_find_by_endpoint(store):
    server = make_server()
    store.upsert(server)
    assert store.find_by_name("root@47.108.1.1:22").id == server.id


def test_atomic_write_leaves_no_tmp_file(store):
    store.upsert(make_server())
    assert not (store.path.with_suffix(".tmp")).exists()
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["version"] == 1


def test_corrupt_config_is_backed_up_not_lost(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHDESK_HOME", str(tmp_path))
    path = tmp_path / "servers.json"
    path.write_text("{这不是JSON", encoding="utf-8")
    store = ConfigStore(path)
    assert store.all() == []
    backups = list(tmp_path.glob("servers.broken.*"))
    assert backups, "损坏的配置应被备份而不是直接覆盖"


def test_validate_rejects_bad_input():
    with pytest.raises(ValueError):
        ServerConfig(name="", host="1.2.3.4", username="root", auth_type="agent").validate()
    with pytest.raises(ValueError):
        ServerConfig(name="a", host="1.2.3.4", username="root", port=70000,
                     auth_type="agent").validate()
    with pytest.raises(ValueError):
        ServerConfig(name="a", host="1.2.3.4", username="root", auth_type="nope").validate()
    with pytest.raises(ValueError):
        ServerConfig(name="a", host="1.2.3.4", username="root", auth_type="key",
                     key_path="/不存在/id_rsa").validate()


@pytest.mark.parametrize("expr,expected", [
    ("root@10.0.0.5:2222", ("10.0.0.5", 2222)),
    ("10.0.0.5", ("10.0.0.5", 22)),
    ("ubuntu@jump.example.com", ("jump.example.com", 22)),
    ("", ("", 0)),
    ("host:notaport", ("host:notaport", 22)),
])
def test_parse_jump(expr, expected):
    assert parse_jump(expr) == expected


def test_app_dir_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHDESK_HOME", str(tmp_path / "custom"))
    assert app_dir() == tmp_path / "custom"
    assert app_dir().exists()


def test_touch_connected(store):
    server = make_server()
    store.upsert(server)
    store.touch_connected(server.id)
    assert store.get(server.id).last_connected_at > 0
