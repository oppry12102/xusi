"""管理面配置：根目录自锚定 + etc/xusi.toml 加载。

根目录 = 本包的上级目录（目录即管理面，与 xuseek 的自锚定同构）。
etc/xusi.toml 缺失时用内置默认值起服务（首次 install 前也能 doctor）。

重要的去耦：node_id 写在 etc/node.id（单行文件、gitignored、600），
不在 toml 里——避免跨机器 git pull 共享同一 id。
etc/node.id 缺失时（首次 install 或新机器 git pull 后第一次 serve）
由 load_config 自动生成一个 URL-safe 8 字节的 id 写盘。
要临时覆盖可设环境变量 XUSI_NODE_ID。

为什么不读 /etc/machine-id：VM / 容器克隆会把 machine-id 一起复制，
Phase 1.2 时两台克隆 xusi 共享 fd410411419d 就是这个原因。/etc/machine-id
只反映"系统视角的机器"，与"xusi 节点身份"是两件事——克隆 / 镜像场景下
前者不可靠，后者必须本地生成。
"""
from __future__ import annotations

import os
import pwd
import secrets
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
    versions_dir: Path = ROOT / "versions"  # xuseek-v2 源码唯一事实源：管理员投放 xuseek-v2-<版本号>.zip
    display_timezone: str = "Asia/Shanghai"

    # —— 身份 ——
    admin_secret: str = ""       # admin token（etc/xusi.toml [admin].secret）。
                                 # 留空 = 未初始化，doctor 会提醒。
                                 # 本字段的用途仅是签发/校验；不要写到 /api/* 响应里。
    node_id: str = ""           # 节点身份 = etc/node.id 的内容（首次 serve 自动生成，
                                 # URL-safe 8 字节；gitignored、600；不可改——改它会失去
                                 # 与历史备份的关联性）。
                                 # 临时覆盖：环境变量 XUSI_NODE_ID。

    default_roots: list = field(default_factory=list)  # [[default_roots]] 缺省根智能体
                                 # [{address, token}]：创建对话框预填（可删改），
                                 # address/token 齐备才生效（与内核 [[roots]] 交割同规则）。

    # —— 双运行时 ——
    default_runtime: str = "systemd"  # 新建 agent 的缺省运行时：systemd（系统进程）
                                 # 或 docker（容器，host 网络；需内核 ≥ v2.7.19 与
                                 # docker 环境）。创建对话框预选此值，可逐次覆盖。
    docker_pip_index: str | None = None  # docker 镜像构建/运行时的 PyPI 镜像：
                                 # None = 内置默认（清华）；"" = 关闭镜像走 pypi.org。
    docker_apt_mirror: str = ""    # 可选：debian 源镜像（如 mirrors.tencentyun.com），
                                 # 仅构建期生效，不影响镜像可移植。
    docker_extras: str = ""        # 可选：能力包名（如 amem），构建期烘培其重依赖进镜像。
    docker_user: str = ""          # 容器运行用户 "<uid>:<gid>"。缺省 = 管理面进程的
                                 # uid:gid（容器内大脑与管理面同用户，能力与 systemd
                                 # 模式对齐，/data 落盘属主一致）。显式设 "0:0" =
                                 # 容器内 root（大脑近似宿主 root——对应隔离讨论的
                                 # root 档，谨慎使用）。

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
    def node_id_file(self) -> Path: return self.etc_dir / "node.id"

    def instance_home(self, agent_id: str) -> Path:
        return self.instances_dir / agent_id

    def unit_name(self, agent_id: str) -> str:
        return f"xusi-a-{agent_id}"

    @property
    def compose_dir(self) -> Path:
        """docker agent 的 compose 渲染目录：instances/.compose/<unit>/。
        在实例根（/data 挂载）之外——容器内大脑看不到改不到（dockerctl 按需 mkdir）。"""
        return self.instances_dir / ".compose"

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
    if "versions_dir" in mgr:
        cfg.versions_dir = Path(os.path.expanduser(str(mgr["versions_dir"]))).resolve()
    if "display_timezone" in mgr:
        cfg.display_timezone = str(mgr["display_timezone"])
    # 双运行时：缺省运行时 + docker 镜像构建参数（详见 dockerctl.py）
    if "default_runtime" in mgr:
        rt = str(mgr["default_runtime"]).strip()
        if rt in ("systemd", "docker"):
            cfg.default_runtime = rt
        else:
            print(f"警告：default_runtime 非法值 {rt!r}（只能是 systemd/docker），回退 systemd")
    if "docker_pip_index" in mgr:
        # 三态：键缺失 → None（dockerctl 用内置清华默认）；空串 → ""（显式关闭
        # 镜像走 pypi.org）；非空 → 指定镜像
        cfg.docker_pip_index = str(mgr["docker_pip_index"]).strip()
    if "docker_apt_mirror" in mgr:
        cfg.docker_apt_mirror = str(mgr["docker_apt_mirror"]).strip()
    if "docker_extras" in mgr:
        cfg.docker_extras = str(mgr["docker_extras"]).strip()
    if "docker_user" in mgr:
        cfg.docker_user = str(mgr["docker_user"]).strip()
    if not cfg.docker_user:
        # 缺省 = 管理面用户的 uid + 主组 gid（容器内大脑与管理面同用户——
        # /data 落盘属主一致，投信/观察台 token 签发等管理面写入不受 root
        # 属主阻塞）。主组取 passwd 而非 os.getgid()：管理面经 sg/newgrp
        # 启动时有效组会变，落盘属主要与用户身份一致才稳定
        try:
            _gid = pwd.getpwuid(os.getuid()).pw_gid
        except Exception:
            _gid = os.getgid()
        cfg.docker_user = f"{os.getuid()}:{_gid}"
    # admin token = [admin].secret（唯一键位）
    admin = raw.get("admin", {})
    if "secret" in admin:
        cfg.admin_secret = str(admin["secret"])
    # 缺省根智能体：[[default_roots]] 数组表（创建对话框预填）。齐备才生效——
    # 与内核 [[roots]] 交割同规则（address/token 缺一的条目跳过，不生效）
    cfg.default_roots = [
        {"address": str(r.get("address", "")).strip(),
         "token": str(r.get("token", "")).strip()}
        for r in (raw.get("default_roots") or [])
        if isinstance(r, dict)
    ]
    cfg.default_roots = [r for r in cfg.default_roots if r["address"] and r["token"]]
    cfg.ensure_dirs()
    # 节点身份：etc/node.id 本地单行文件（gitignored、600）。
    # 优先 XUSI_NODE_ID 环境变量（测试 / CI / 临时改名），否则读文件，
    # 文件不存在则生成 URL-safe 8 字节写盘（首次 install / 新机器 git pull
    # 后第一次 serve 自动落入）。
    cfg.node_id = _resolve_node_id(cfg.node_id_file)
    return cfg


def _resolve_node_id(node_id_file: Path) -> str:
    """读 etc/node.id；空则生成写盘。三级优先：

    ① XUSI_NODE_ID 环境变量（最高——测试 / CI / 临时改名场景用）
    ② etc/node.id 的内容（持久化本机身份；首次 install 自动生成）
    ③ 自动生成 URL-safe 8 字节写盘（首次 serve 走这条）

    为什么不用 /etc/machine-id：VM / 容器克隆会把 machine-id 一起复制，
    两台克隆 xusi 会撞同一 id（Phase 1.2 的 fd410411419d 案例）。"""
    env = os.environ.get("XUSI_NODE_ID")
    if env:
        return env.strip()
    try:
        existing = node_id_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        pass
    # 生成：URL-safe 8 字节 ≈ 11 字符，无 padding、无歧义字符
    new_id = secrets.token_urlsafe(8)
    node_id_file.parent.mkdir(parents=True, exist_ok=True)
    node_id_file.write_text(new_id + "\n", encoding="utf-8")
    try:
        node_id_file.chmod(0o600)
    except OSError:
        pass
    return new_id


_CONFIG: XusiConfig | None = None


def get_config() -> XusiConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def live_default_roots() -> list:
    """每次直读 etc/xusi.toml 的 [[default_roots]]——预填数据要跟盘面走：
    换根 token 只改 toml 即生效，不必重启管理面（get_config 的进程级缓存
    会把旧 token 焐在内存里，新 agent 出生就带旧根）。"""
    try:
        raw = _load_toml(ROOT / "etc" / "xusi.toml")
    except Exception:
        return []
    roots = [
        {"address": str(r.get("address", "")).strip(),
         "token": str(r.get("token", "")).strip()}
        for r in (raw.get("default_roots") or [])
        if isinstance(r, dict)
    ]
    return [r for r in roots if r["address"] and r["token"]]
