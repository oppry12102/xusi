"""管理面配置：根目录自锚定 + etc/xusi.toml 加载。

根目录 = 本包的上级目录（目录即管理面，与 xuseek 的自锚定同构）。
etc/xusi.toml 缺失时用内置默认值起服务（首次 install 前也能 doctor）。
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:  # 写坏 TOML：用默认值起，不炸
        print(f"警告：{path} 解析失败（{e}），使用默认配置")
        return {}


@dataclass
class XusiConfig:
    root: Path = ROOT
    host: str = "0.0.0.0"
    port: int = 8601
    port_lo: int = 8602          # agent 端口分配下界（8601 归管理面）
    port_hi: int = 8699
    source_dir: Path = ROOT / "xuseek-v2"   # xuseek-v2 源码（自管：缺失时从 GitHub 拉取）
    source_repo: str = "https://github.com/oppry12102/xuseek-v2"
    versions_dir: Path = ROOT / "versions"  # 版本仓库：管理员投放 xuseek-v2-<版本号>.zip
    display_timezone: str = "Asia/Shanghai"

    # —— 派生路径 ——
    @property
    def etc_dir(self) -> Path: return self.root / "etc"
    @property
    def instances_dir(self) -> Path: return self.root / "instances"
    @property
    def trash_dir(self) -> Path: return self.root / "instances" / ".trash"
    @property
    def brains_file(self) -> Path: return self.etc_dir / "brains.toml"
    @property
    def agents_file(self) -> Path: return self.etc_dir / "agents.json"
    @property
    def tokens_file(self) -> Path: return self.etc_dir / "tokens.json"
    @property
    def audit_file(self) -> Path: return self.etc_dir / "audit.jsonl"
    @property
    def backup_dir(self) -> Path: return self.root / "etc" / "backups"
    @property
    def webui_dir(self) -> Path: return self.root / "xusi" / "webui"
    @property
    def docs_dir(self) -> Path: return self.root / "docs"

    def instance_home(self, agent_id: str) -> Path:
        return self.instances_dir / agent_id

    def unit_name(self, agent_id: str) -> str:
        return f"xusi-a-{agent_id}"

    def ensure_dirs(self) -> None:
        for d in (self.etc_dir, self.instances_dir, self.trash_dir,
                  self.versions_dir, self.backup_dir, self.webui_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_config() -> XusiConfig:
    raw = _load_toml(ROOT / "etc" / "xusi.toml")
    cfg = XusiConfig(root=ROOT)
    srv = raw.get("server", {})
    mgr = raw.get("manager", {})
    if "host" in srv: cfg.host = str(srv["host"])
    if "port" in srv: cfg.port = int(srv["port"])
    lo, hi = mgr.get("port_range", [cfg.port_lo, cfg.port_hi])
    cfg.port_lo, cfg.port_hi = int(lo), int(hi)
    if "source_dir" in mgr:
        cfg.source_dir = Path(os.path.expanduser(str(mgr["source_dir"]))).resolve()
    if "versions_dir" in mgr:
        cfg.versions_dir = Path(os.path.expanduser(str(mgr["versions_dir"]))).resolve()
    if "source_repo" in mgr:
        cfg.source_repo = str(mgr["source_repo"])
    if "display_timezone" in mgr:
        cfg.display_timezone = str(mgr["display_timezone"])
    cfg.ensure_dirs()
    return cfg


_CONFIG: XusiConfig | None = None


def get_config() -> XusiConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG
