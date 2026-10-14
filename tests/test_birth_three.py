"""出生三件直写（brains.write_birth_three）契约测试。

背景：内核 ≥v2.9.3x「文件即一切」——出生三件是管理员直写的文件，config 的
mission/instance_id/[[roots]] 是死键；内核一份都不代写（xuseek2 cli._preflight）。
本套锁定：形状 / 600 / 幂等 / 空值跳过 / id 行正则契约。
"""
import json
import re
import stat

from xusi.brains import _ID_CARD_TEMPLATE, write_birth_three

# 内核侧契约的正形（xuseek2/xuseek/cli.py _ID_RE），此处按字复制——
# 两处任一改形状而不同步，测试就该红。
_ID_RE = re.compile(r"id：`([0-9a-f]{1,8})`")


def _write(home, mission="测试使命：守住一线。", instance_id="ab12", roots=None):
    return write_birth_three(home, mission, instance_id, roots)


def test_three_written_with_shapes(tmp_path):
    roots = [{"address": "http://www.oppry.com:8400", "token": "rt-test"}]
    lines = _write(tmp_path, roots=roots)
    assert len(lines) == 3
    xin = (tmp_path / "workspace/playbook/初心.md").read_text(encoding="utf-8")
    assert xin == "测试使命：守住一线。\n"
    card = (tmp_path / "workspace/playbook/我的身份.md").read_text(encoding="utf-8")
    m = _ID_RE.search(card)
    assert m and m.group(1) == "ab12"
    rj = json.loads((tmp_path / "workspace/playbook/根智能体.json").read_text(encoding="utf-8"))
    assert rj == {"roots": roots}
    mode = stat.S_IMODE((tmp_path / "workspace/playbook/根智能体.json").stat().st_mode)
    assert mode == 0o600


def test_idempotent_never_overwrite(tmp_path):
    pb = tmp_path / "workspace/playbook"
    pb.mkdir(parents=True)
    (pb / "初心.md").write_text("旧初心，勿动", encoding="utf-8")
    lines = _write(tmp_path, roots=[{"address": "http://x", "token": "t"}])
    assert (pb / "初心.md").read_text(encoding="utf-8") == "旧初心，勿动"
    assert any("未覆写" in ln and "初心" in ln for ln in lines)
    # 三件全在 → 再跑一遍，全部「未覆写」，无 ✓
    lines2 = _write(tmp_path, roots=[{"address": "http://x", "token": "t"}])
    assert not any("✓" in ln for ln in lines2)


def test_empty_values_skip(tmp_path):
    lines = write_birth_three(tmp_path, "", "", None)
    assert lines == []
    assert not (tmp_path / "workspace/playbook/初心.md").exists()
    assert not (tmp_path / "workspace/playbook/我的身份.md").exists()
    assert not (tmp_path / "workspace/playbook/根智能体.json").exists()


def test_mkdir_deep(tmp_path):
    home = tmp_path / "a/b/c"
    _write(home)
    assert (home / "workspace/playbook/我的身份.md").exists()


def test_id_line_exact_shape():
    """模板首块 id 行必须逐字含内核契约行「- id：`{i}` —— 终身不变」。"""
    card = _ID_CARD_TEMPLATE.format(id="93ac")
    assert "- id：`93ac` —— 终身不变" in card
    assert _ID_RE.search(card).group(1) == "93ac"
