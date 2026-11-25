"""统一收信面：管理员信 = inbox.jsonl（投递）+ facts bell（醒铃）+ facts mail（审计）。"""
import json
import sqlite3

import pytest

from xusi import agentops


@pytest.fixture
def fake_agent(tmp_path, monkeypatch):
    home = tmp_path / "agent-test"
    (home / "workspace").mkdir(parents=True)
    (home / "data").mkdir(parents=True)
    monkeypatch.setattr(agentops, "get_agent_or_404", lambda aid: {"id": aid})
    monkeypatch.setattr(agentops, "_home", lambda a: home)
    monkeypatch.setattr(agentops, "_kernel_version", lambda h: (2, 9, 52))
    monkeypatch.setattr(agentops, "audit", lambda *a, **k: None)
    return home


def test_mail_three_landings(fake_agent):
    r = agentops.mail("agent-test", "测试信正文")
    assert r["posted"] is True
    # ① 投递：inbox.jsonl 一行，形状与 seed 收信服务一致
    inbox = fake_agent / "workspace" / "inbox.jsonl"
    lines = [json.loads(l) for l in inbox.read_text(encoding="utf-8").splitlines() if l]
    assert len(lines) == 1
    assert lines[0]["from"] == "admin"
    assert lines[0]["text"] == "测试信正文"
    assert lines[0]["at"].endswith("Z")
    # ②③ facts：mail 审计行 + bell 醒铃行成对
    db = sqlite3.connect(fake_agent / "data" / "facts.db")
    types = [r[0] for r in db.execute("SELECT type FROM facts ORDER BY n")]
    assert types.count("mail") == 1
    assert types.count("bell") == 1
    assert types.index("bell") > types.index("mail")  # 先落信后敲铃


def test_mail_empty_rejected(fake_agent):
    with pytest.raises(agentops.AgentError):
        agentops.mail("agent-test", "   ")


def test_mail_legacy_kernel_gets_latch(fake_agent, monkeypatch):
    monkeypatch.setattr(agentops, "_kernel_version", lambda h: (2, 9, 30))
    agentops.mail("agent-test", "旧内核信")
    latch = fake_agent / "data" / "mail.txt"
    assert latch.is_file()
    row = json.loads(latch.read_text(encoding="utf-8").splitlines()[-1])
    assert row["sender"] == "admin"
    assert row["text"] == "旧内核信"
    # inbox 投递与 bell 照发（信必读的地方不因版本打折）
    assert (fake_agent / "workspace" / "inbox.jsonl").is_file()
