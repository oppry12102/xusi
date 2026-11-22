"""本地共享（localshare）契约测试。

背景：同一服务器、同一用户名下的容器智能体共用管理面目录下的 share/——
容器里挂在世界根的 share/，并在 playbook 种一份用法指南（xuseek2 内核零改动）。
本套锁定：就位三件 / 挂载点在世界根外面（与 workspace 并排）/ 幂等不覆写 /
挂载点处有实例自己的内容则不挂不种 / compose 只在可挂时才渲染那一行 /
共享目录建不出来只告警不拦。
"""
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from xusi import dockerctl, localshare

MOUNT = "/data/share"   # 容器内真实路径（与 CONTAINER_MOUNT 同值——指南里写的就是它；与世界根并排）


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """隔离的配置：共享目录指到临时处，其余取缺省。"""
    from xusi.config import XusiConfig
    share = tmp_path / "xusi-share"
    c = XusiConfig()
    c.share_dir = share
    monkeypatch.setattr("xusi.config.get_config", lambda: c)
    return c


def _home(tmp_path) -> Path:
    h = tmp_path / "instance"
    (h / "workspace" / "playbook").mkdir(parents=True)
    return h


def test_first_ensure_puts_three_things_in_place(tmp_path, cfg):
    home = _home(tmp_path)
    lines = localshare.ensure(home)
    assert cfg.share_dir.is_dir()                               # ① 共享目录
    assert localshare.MOUNT_REL == Path("share")                 # ② 挂载点与 workspace 并排、在它外面
    assert (home / localshare.MOUNT_REL).is_dir()                 #    （空）
    assert not any((home / localshare.MOUNT_REL).iterdir())
    guide = home / "workspace/playbook" / localshare.GUIDE_NAME  # ③ playbook 指南
    text = guide.read_text(encoding="utf-8")
    assert len(lines) == 1 and "种入" in lines[0]
    assert localshare.CONTAINER_MOUNT == MOUNT and MOUNT in text   # 指南写的是容器内真实路径
    for must in ("人人可读可写", "不在管理面备份里", "挂载点", "世界根", "并排", "绝对路径"):
        assert must in text
    # 指南不是凭证：不收成 600（与出生三件里含 token 的根智能体.json 不同）
    assert stat.S_IMODE(guide.stat().st_mode) != 0o600


def test_idempotent_and_never_overwrites(tmp_path, cfg):
    home = _home(tmp_path)
    localshare.ensure(home)
    guide = home / "workspace/playbook" / localshare.GUIDE_NAME
    guide.write_text("我自己改过的版本，勿动", encoding="utf-8")
    assert localshare.ensure(home) == []                        # 照旧时静默（每次 restart 都会经过）
    assert guide.read_text(encoding="utf-8") == "我自己改过的版本，勿动"


def test_private_nonempty_share_is_left_alone(tmp_path, cfg):
    """实例目录里已有非空的 share/（如裸机形态时自建）→ 挂载会盖住它：不挂、不种、如实告警。"""
    home = _home(tmp_path)
    mine = home / "share/mine.txt"
    mine.parent.mkdir(parents=True)
    mine.write_text("x", encoding="utf-8")
    lines = localshare.ensure(home)
    assert len(lines) == 1 and "未挂载" in lines[0]
    assert not localshare.mountable(home, cfg.share_dir)
    assert not (home / "workspace/playbook" / localshare.GUIDE_NAME).exists()
    assert mine.read_text(encoding="utf-8") == "x"


def test_symlink_at_mountpoint_is_not_touched(tmp_path, cfg):
    home = _home(tmp_path)
    cfg.share_dir.mkdir(parents=True)
    (home / "share").symlink_to(cfg.share_dir, target_is_directory=True)
    assert not localshare.mountable(home, cfg.share_dir)
    assert "未挂载" in localshare.ensure(home)[0]
    assert not (home / "workspace/playbook" / localshare.GUIDE_NAME).exists()


def test_unwritable_share_dir_warns_but_does_not_raise(tmp_path, cfg, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root 不受目录权限约束")
    jail = tmp_path / "jail"
    jail.mkdir()
    jail.chmod(0o500)
    cfg.share_dir = jail / "share"
    lines = localshare.ensure(_home(tmp_path))
    assert len(lines) == 1 and "建不出来" in lines[0]
    assert not (tmp_path / "instance/workspace/playbook" / localshare.GUIDE_NAME).exists()


def _compose(home: Path) -> str:
    return dockerctl._render_compose("xusi-a-test", Path("/src"), home, "127.0.0.1", 8602, "v0.0.0")


def test_compose_renders_share_volume_only_when_mountable(tmp_path, cfg):
    home = _home(tmp_path)
    line = f'      - "{cfg.share_dir}:{localshare.CONTAINER_MOUNT}"'
    assert line not in _compose(home)                          # 共享目录还不存在 → 不挂
    localshare.ensure(home)
    y = _compose(home)
    assert line in y                                           # 可挂 → home 一行、share 一行
    assert [l for l in y.splitlines() if l.lstrip().startswith("- ") and ":/data" in l] == [
        f'      - "{home}:/data"', line]
    (home / "share/priv").write_text("x", encoding="utf-8")   # 实例目录里已有私有内容 → 不挂
    assert line not in _compose(home)
    assert f'      - "{home}:/data"' in _compose(home)          # home 挂载照旧
