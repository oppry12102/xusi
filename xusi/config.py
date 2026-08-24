"""管理面配置：根目录自锚定 + etc/xusi.toml 加载。

根目录 = 本包的上级目录（目录即管理面，与 xuseek 的自锚定同构）。
etc/xusi.toml 缺失时用内置默认值起服务（首次 install 前也能 doctor）。

重要的去耦：node_id 不再写到 toml（避免跨机器 git pull 共享同一 id 导致重号；
Phase 1 的 IUyWwGI3 撞车就是这么来的）。节点身份从 /etc/machine-id 派生，
想要自定义可设环境变量 XUSI_NODE_ID 覆盖。
"""
from __future__ import annotations

import hashlib
import os
import socket
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

    # —— 集群身份（Phase1：仅 config 字段；2026-08-24 起的互联基础）——
    cluster_secret: str = ""     # [cluster].secret 留空 = 单节点模式（今天的行为）；
                                 # 设值 = 同密钥的所有 xusi 互信，token 用 HS256-JWT 跨节点通用。
                                 # 本字段的用途仅是签发/校验；不要写到 /api/* 响应里。
    node_role: str = "worker"   # [node].role：worker | backup | portal；
                                 # 改它要重启语义；agent 启停路径依赖本字段。
    node_public_url: str = ""   # [node].public_url 显式覆盖（推荐）——peer 名册要拿这个；
                                 # 空时按 host:port 自动探测。

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
    @property
    def node_file(self) -> Path: return self.etc_dir / "node.json"
    @property
    def peers_file(self) -> Path: return self.etc_dir / "peers.toml"

    def instance_home(self, agent_id: str) -> Path:
        return self.instances_dir / agent_id

    def unit_name(self, agent_id: str) -> str:
        return f"xusi-a-{agent_id}"

    @property
    def public_url(self) -> str:
        """本节点的对外访问地址（peer 列表相互引用的形态）。

        三级优先：
        ① [node].public_url 显式覆盖（推荐——明确可控）；
        ② `host:port`，host 是 0.0.0.0/::/空 时自动探测本机出站 IP；
        ③ 全失败时回退 'localhost'（明显坏但不会让服务起不来——前端会立即看见歧义）。

        Phase1 用于 /api/peer/id 自报与对等名册快照。Phase2 peer 转发按此 url 反向调用。"""
        if self.node_public_url:
            return self.node_public_url
        host = self.host
        if host in ("0.0.0.0", "", "::"):
            host = _detect_outbound_ip() or "localhost"
        return f"http://{host}:{self.port}"

    def ensure_dirs(self) -> None:
        for d in (self.etc_dir, self.instances_dir, self.trash_dir,
                  self.versions_dir, self.backup_dir, self.webui_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def node_id(self) -> str:
        """节点身份——三级优先级：

        ① 环境变量 XUSI_NODE_ID（最高，专用于测试 / CI / 临时改名）
        ② /etc/machine-id 前 12 字符（systemd 自动生成的机器 ID，
           每台机器首次启动时生成，跨机器天然分离、跨重启稳定——这是正路）
        ③ sha256(socket.gethostname()) 前 12 字符（无 machine-id 的环境兜底）

        注意：我们 **故意不** 从 etc/xusi.toml 读 id——etc/xusi.toml 是
        跨机器共享的配置文件，Phase 1 时把 id 写进去会导致 'git pull 拿走过期
        id 然后两台机器撞号'。要自定义请用环境变量。
        """
        env = os.environ.get("XUSI_NODE_ID")
        if env:
            return env
        return _derive_node_id()


def _derive_node_id() -> str:
    """机器身份——从 /etc/machine-id 或 hostname 哈希派生。"""
    p = Path("/etc/machine-id")
    if p.exists():
        try:
            mid = p.read_text().strip()
            if mid:
                return mid[:12]
        except OSError:
            pass
    return hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:12]


def load_config() -> XusiConfig:
    raw = _load_toml(ROOT / "etc" / "xusi.toml")
    cfg = XusiConfig(root=ROOT)
    srv = raw.get("server", {})
    mgr = raw.get("manager", {})
    cluster = raw.get("cluster", {})
    node = raw.get("node", {})
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
    if "secret" in cluster:
        cfg.cluster_secret = str(cluster["secret"])
    if "role" in node:
        cfg.node_role = str(node["role"])
    if "public_url" in node:
        cfg.node_public_url = str(node["public_url"])
    cfg.ensure_dirs()
    return cfg


def _detect_outbound_ip() -> str | None:
    """UDP socket trick：不真正发包；从系统路由表反推本机出站 IP。
    8.8.8.8 是路由探测的经典目标（Google DNS，不可达也无妨——connect 不发数据）。
    失败（无外网/无默认路由/容器内受限）返回 None，由 caller 兜底。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


_CONFIG: XusiConfig | None = None


def get_config() -> XusiConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG
