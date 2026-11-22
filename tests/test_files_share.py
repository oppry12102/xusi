"""文件页 × 本地共享（files.py 的开放根 share/ 映射）契约测试。

挂了共享目录的实例，容器里的 /data/share（与世界根并排）就是共享目录本身——
管理面文件页（详情页「文件」tab / fs-* 命令）的开放根 share/ 映射到同一处：
根视图多一个 share 行、看得到同伴放的文件、仍是只读、里面的符号链接不许出
共享目录（它的上一级是管理面根，etc/ 里有凭据）。
本套锁定：根视图与映射 / 未挂不映射 / 只读 / 逃逸拒绝（读与打包）/ mounted_dir 判据。
"""
import json
from pathlib import Path

import pytest

from xusi import files, localshare
from xusi.agentops import AgentError

AGENT = "agent-aa00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离环境：实例 home + 共享目录；挂没挂由 state['mounted'] 控制。"""
    from xusi.config import XusiConfig
    cfg = XusiConfig()
    cfg.root = tmp_path                       # instances/ 等派生路径全落在临时处
    cfg.share_dir = tmp_path / "share"
    home = cfg.instance_home(AGENT)
    (home / "share").mkdir(parents=True)      # 实例侧挂载点（与世界根并排）：宿主上是空目录
    (home / "workspace/playbook").mkdir(parents=True)
    cfg.share_dir.mkdir()
    (cfg.share_dir / "bb11").mkdir()
    (cfg.share_dir / "bb11/for-aa00.txt").write_text("hello from bb11\n", encoding="utf-8")
    state = {"mounted": True}
    monkeypatch.setattr("xusi.config.get_config", lambda: cfg)
    monkeypatch.setattr("xusi.files.get_config", lambda: cfg)   # files.py 在 import 时就绑了名字
    monkeypatch.setattr("xusi.files.get_agent", lambda aid: {"id": aid, "runtime": "docker"})
    monkeypatch.setattr(localshare, "mounted_dir",
                        lambda aid: cfg.share_dir if state["mounted"] else None)
    return cfg, home, state


def test_root_view_has_share_row_and_share_root_lists_the_shared_dir(env):
    cfg, home, _ = env
    assert not any((home / "share").iterdir())                      # 宿主视角：挂载点是空的
    root = files.list_dir(AGENT, "")
    assert [e["name"] for e in root["entries"]] == ["upload", "workspace", "share"]
    row = root["entries"][-1]
    assert row["dir"] and "本地共享" in row["note"] and localshare.CONTAINER_MOUNT in row["note"]
    assert "share" not in [e["name"] for e in files.list_dir(AGENT, "workspace")["entries"]]  # 不在私人世界里
    assert [e["name"] for e in files.list_dir(AGENT, "share")["entries"]] == ["bb11"]
    assert [e["name"] for e in files.list_dir(AGENT, "share/bb11")["entries"]] == ["for-aa00.txt"]
    r = files.read_text(AGENT, "share/bb11/for-aa00.txt")
    assert r["text"] == "hello from bb11\n" and not r["writable"] and not r["editable"]


def test_unmounted_instance_has_no_share_root(env):
    cfg, home, state = env
    state["mounted"] = False
    assert [e["name"] for e in files.list_dir(AGENT, "")["entries"]] == ["upload", "workspace"]
    with pytest.raises(AgentError, match="没有挂本地共享"):
        files.list_dir(AGENT, "share")
    with pytest.raises(AgentError, match="没有挂本地共享"):
        files.read_text(AGENT, "share/bb11/for-aa00.txt")


def test_share_stays_read_only_for_the_admin_plane(env):
    cfg, home, _ = env
    for call in (
        lambda: files.put_bytes(AGENT, "share/x.bin", b"x"),
        lambda: files.write_text(AGENT, "share/x.txt", "x"),
        lambda: files.mkdir(AGENT, "share/newdir"),
        lambda: files.delete(AGENT, "share/bb11/for-aa00.txt"),
        lambda: files.move(AGENT, "share/bb11/for-aa00.txt", "upload/a.txt"),
    ):
        with pytest.raises(AgentError):
            call()
    assert (cfg.share_dir / "bb11/for-aa00.txt").read_text(encoding="utf-8") == "hello from bb11\n"


def test_symlink_in_share_cannot_escape_on_read_or_zip(env, tmp_path):
    cfg, home, _ = env
    secret = tmp_path / "secret.toml"                              # 共享目录之外（如管理面 etc/）
    secret.write_text("password = 'x'\n", encoding="utf-8")
    (cfg.share_dir / "bb11/ln").symlink_to(secret)
    (cfg.share_dir / "bb11/alias").symlink_to(cfg.share_dir / "bb11")   # 指向共享目录之内：允许
    with pytest.raises(AgentError, match="越界"):
        files.read_text(AGENT, "share/bb11/ln")
    with pytest.raises(AgentError, match="越界"):
        files.open_raw(AGENT, "share/bb11/ln")
    for rel in ("share", "share/bb11"):        # 打包：整个共享目录也不许带出区外内容
        with pytest.raises(AgentError, match="符号链接指向开放区外"):
            files.zip_dir(AGENT, rel)
    assert files.read_text(AGENT, "share/bb11/alias/for-aa00.txt")["text"].startswith("hello")


def test_mounted_dir_reads_the_rendered_compose(tmp_path, monkeypatch):
    from xusi import dockerctl
    cf = tmp_path / "compose.yaml"
    monkeypatch.setattr("xusi.registry.get_agent", lambda aid: {"id": aid, "runtime": "docker"})
    monkeypatch.setattr("xusi.config.get_config", lambda: type("C", (), {"unit_name": staticmethod(lambda aid: "u")})())
    monkeypatch.setattr(dockerctl, "compose_file_for", lambda unit: cf)
    assert localshare.mounted_dir(AGENT) is None                   # 还没渲染过 compose
    cf.write_text('    volumes:\n      - "/h/inst:/data"\n', encoding="utf-8")
    assert localshare.mounted_dir(AGENT) is None                   # 渲染过但没那一行（存量实例没重启）
    host_dir = tmp_path / "共 享"
    cf.write_text('    volumes:\n      - "/h/inst:/data"\n      - '
                  + json.dumps(f"{host_dir}:{localshare.CONTAINER_MOUNT}", ensure_ascii=False) + "\n",
                  encoding="utf-8")
    assert localshare.CONTAINER_MOUNT == "/data/share"
    assert localshare.mounted_dir(AGENT) == host_dir                # 挂了：宿主上共享目录的真实路径
    monkeypatch.setattr("xusi.registry.get_agent", lambda aid: {"id": aid, "runtime": "systemd"})
    assert localshare.mounted_dir(AGENT) is None                   # 非容器形态永远不挂
