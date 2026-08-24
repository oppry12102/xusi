"""智能体备份 / 恢复 —— 状态镜像 + 后台无关的 backend 抽象。

设计要点：
  1. 只在 agent 处于 sleeping 时备份（前置条件：daemon.state == "sleeping" 且
     next_wake_at 距今 >= grace_seconds）。快照窗口内 SIGSTOP/SIGCONT 单元
     保证文件系统层一致；agent 视角无感（睡久了 1-3 秒不会被察觉）。
  2. 备份包 = meta.json + config.toml + data/ + workspace/。
     排除：.venv/（重建）、xuseek-v2/（重建）、__pycache__/*.pyc（缓存）、
     webui_tokens.json（凭证，重新签发）。
  3. backend 解耦：LocalBackend 是当前实现，第三方可 drop-in 实现 S3Backend 等。
  4. 跨主机：备份包自描述（meta 含 source_version / port / brains 等），
     在另一台装了 xuseek-v2 versions 的机器上 `xusi restore` 即可起。
  5. 恢复流程：解压 → 从 versions 重建私有 xuseek-v2 副本 → 写注册表 → 启动
     （.venv 由 xuseek.sh 首次启动自建，无需打包）。
"""
from __future__ import annotations

import io
import json
import os
import platform
import socket
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import __version__, agentops, brains, node, ports, registry, systemdctl, versions
from .config import get_config


# 拒绝条件：备份触发时距下次唤醒至少这么远（秒），避免 snapshot 撞上唤醒
DEFAULT_GRACE_SECONDS = 30

# tar 内排除的路径/后缀（运行时产物或凭证）
_EXCLUDE_DIRS = {".venv", "xuseek-v2", "__pycache__", ".pytest_cache"}
_EXCLUDE_SUFFIXES = {".pyc", ".egg-info"}
_EXCLUDE_FILES = {"webui_tokens.json"}

# 包命名：xusi-<agent_id>-<UTC 时间戳>.tar.gz
_KEY_FMT = "xusi-{agent_id}-{ts}.tar.gz"


class BackupError(RuntimeError):
    """业务错误（用户可读；API/CLI 层转 4xx）。"""


# ── 后台抽象 ──────────────────────────────────────────────────────────

class BackupBackend(Protocol):
    """备份后台接口。key = 包名（同 host 内唯一即可）。"""
    name: str

    def put(self, key: str, src_path: Path) -> dict:
        """上传 src_path 到 backend；返回 {key, size_bytes, mtime, url}。"""
        ...

    def get(self, key: str, dst_path: Path) -> None:
        """下载到 dst_path；backend 缺这个 key 抛 BackupError。"""
        ...

    def list(self, prefix: str = "") -> list[dict]:
        """列 {key, size_bytes, mtime}[]；prefix 过滤。"""
        ...

    def delete(self, key: str) -> None:
        """删一个 key；不存在视为成功（幂等）。"""
        ...


class LocalBackend:
    """本机文件系统后台：key 即 etc/backups/<key> 文件名。"""

    name = "local"

    def __init__(self, root: Path | None = None) -> None:
        self.root: Path = root or get_config().backup_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # 防路径穿越：key 必须落在 root 下
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve()) + os.sep) and p != self.root:
            raise BackupError(f"非法 key {key!r}（路径越界）")
        if not _KEY_RE.match(key):
            raise BackupError(f"非法 key {key!r}（须匹配 {_KEY_RE.pattern}）")
        return p

    def put(self, key: str, src_path: Path) -> dict:
        dst = self._path(key)
        # 同 key 存在时覆盖（旧包自动丢；list 时仍可见，新备份重新计时）
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        tmp.write_bytes(src_path.read_bytes())
        os.replace(tmp, dst)
        try:
            dst.chmod(0o600)
        except OSError:
            pass
        st = dst.stat()
        return {"key": key, "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "url": f"local://{dst}"}

    def get(self, key: str, dst_path: Path) -> None:
        src = self._path(key)
        if not src.is_file():
            raise BackupError(f"备份不存在：{key}")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst_path.with_suffix(dst_path.suffix + ".tmp")
        tmp.write_bytes(src.read_bytes())
        os.replace(tmp, dst_path)

    def list(self, prefix: str = "") -> list[dict]:
        out: list[dict] = []
        for f in sorted(self.root.iterdir()):
            if not (f.is_file() and _KEY_RE.match(f.name)):
                continue
            if prefix and not f.name.startswith(prefix):
                continue
            st = f.stat()
            out.append({"key": f.name,
                        "size_bytes": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                                   .strftime("%Y-%m-%dT%H:%M:%SZ")})
        return out

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass


# key 命名约束：仅 xusi-<id>-<timestamp>.tar.gz，<id> 限 [A-Za-z0-9_-]+
import re as _re
_KEY_RE = _re.compile(r"^xusi-[A-Za-z0-9_\-]+-\d{8}T\d{6}Z\.tar\.gz$")
_KEY_OK_RE = _re.compile(r"^[A-Za-z0-9_\-]+$")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_key(agent_id: str) -> str:
    if not _KEY_OK_RE.match(agent_id):
        raise BackupError(f"非法 agent_id {agent_id!r}")
    return _KEY_FMT.format(agent_id=agent_id, ts=_ts())


# ── 状态查询 ──────────────────────────────────────────────────────────

def _proc_active(agent_id: str) -> bool:
    """agent 进程是否正在跑（systemd 单元 active）。注册表无记录抛 BackupError。"""
    try:
        st = agentops.status(agent_id)
    except agentops.AgentError as e:
        raise BackupError(str(e)) from None
    return (st.get("process") or {}).get("active") == "active"


def _agent_state(agent_id: str) -> dict:
    """拉一次 agent 的 daemon 状态；agent 未起 / 单元 not-found 抛 BackupError。

    返回 {} 表示状态未知（如 agent 进程存在但 /v1/status 失败）。
    """
    try:
        st = agentops.status(agent_id)
    except agentops.AgentError as e:
        raise BackupError(str(e)) from None
    # agentops.status() 返回 {"agent_status": {"daemon": {...}, ...}, ...}
    inner = st.get("agent_status")
    if isinstance(inner, dict):
        d = inner.get("daemon")
        if isinstance(d, dict):
            return d
    return {}


def _check_backupable(agent_id: str, grace: int) -> None:
    """前置条件（WebUI + CLI 共用）：
      · 进程已停止（active != 'active'）—— 直接放行（无进程无 SIGSTOP 需要）；
      · 进程活跃时要求 daemon.state == 'sleeping' 且 next_wake 距今 >= grace 秒。
    旧版仅认 sleeping 排除了 stopped；webui 入口要求 stopped 可用，故放宽。
    """
    if not _proc_active(agent_id):
        return
    d = _agent_state(agent_id)
    state = d.get("state")
    if state != "sleeping":
        raise BackupError(
            f"agent {agent_id} 当前 daemon 状态 = {state or '未知'}，"
            f"仅在 daemon 休眠中或 agent 已停止时可备份。"
            f"等下一口睡眠或先 `python3 -m xusi` 走 API 停止该 agent。")
    nw = d.get("next_wake_at")
    if nw:
        try:
            t = datetime.fromisoformat(nw.replace("Z", "+00:00"))
            remain = (t - datetime.now(timezone.utc)).total_seconds()
            if remain < grace:
                raise BackupError(
                    f"agent {agent_id} 距离下次唤醒仅 {remain:.0f}s (<{grace}s)，"
                    f"本次跳过以免撞上唤醒边界。")
        except ValueError:
            pass  # 解析失败不阻塞


# 兼容旧 CLI 脚本对 _check_sleeping 的引用
_check_sleeping = _check_backupable


# ── 快照 ──────────────────────────────────────────────────────────────

def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """tar 成员过滤器：排除运行时产物与凭证。"""
    parts = tarinfo.name.split("/")
    if any(p in _EXCLUDE_DIRS for p in parts):
        return None
    if any(parts[-1].endswith(s) for s in _EXCLUDE_SUFFIXES):
        return None
    if parts[-1] in _EXCLUDE_FILES:
        return None
    return tarinfo


def _build_meta(agent_id: str, agent: dict, reason: str,
                tmp_size: int, home_size: int) -> dict:
    """包内 meta.json：注册表快照 + 备份元数据。"""
    return {
        "xusi_version": __version__,
        "agent_id": agent_id,
        "agent_name": agent.get("name", agent_id),
        "mission": agent.get("mission", ""),
        "brains": list(agent.get("brains", [])),
        "budgets": dict(agent.get("budgets") or {}),
        "source_version": agent.get("source_version", ""),
        "expose": bool(agent.get("expose", False)),
        "note": agent.get("note", ""),
        "created_at": agent.get("created_at", ""),
        "snapshot_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot_reason": reason,
        "from_host": platform.node() or socket.gethostname(),
        "xuseek_version": _detect_xuseek_version(agent_id),
        "home_size_bytes": home_size,
        "tar_size_bytes": tmp_size,
    }


def _detect_xuseek_version(agent_id: str) -> str:
    try:
        st = agentops.status(agent_id)
        return str(st.get("version", ""))
    except Exception:
        return ""


def snapshot(agent_id: str, *, reason: str = "manual",
             grace: int = DEFAULT_GRACE_SECONDS,
             backend: BackupBackend | None = None) -> dict:
    """备份 agent 的 data + workspace + config 到 backend。

    返回 {key, size_bytes, mtime, meta}。前置条件不满足抛 BackupError。
    """
    be = backend or LocalBackend()
    agent = registry.get_agent(agent_id)
    if not agent:
        raise BackupError(f"注册表中没有 agent {agent_id}")
    home = get_config().instance_home(agent_id)
    if not home.is_dir():
        raise BackupError(f"agent home 不存在：{home}")

    proc_active = _proc_active(agent_id)
    _check_backupable(agent_id, grace)
    unit = get_config().unit_name(agent_id)

    # 估算 home 大小（仅 data + workspace，excluded 之后）
    home_size = sum(
        p.stat().st_size for p in (home / "data").rglob("*") if p.is_file()) \
        + sum(p.stat().st_size for p in (home / "workspace").rglob("*") if p.is_file()) \
        + (home / "config.toml").stat().st_size

    # SIGSTOP 冻结 → tar → SIGCONT（即使 tar 抛错也解冻）
    # 进程已停止时跳过 SIGSTOP/SIGCONT（无进程可冻结，且 kill 会失败）
    if proc_active:
        systemdctl.kill_signal(unit, "SIGSTOP")
    cfg = home / "config.toml"
    try:
        with tempfile.NamedTemporaryFile(
                prefix=f"xusi-backup-{agent_id}-", suffix=".tar.gz",
                delete=False) as tf:
            tmp_path = Path(tf.name)
        try:
            # 第一遍：只写 config + data + workspace，量出 tar 体积
            with tarfile.open(tmp_path, "w:gz") as tar:
                if cfg.is_file():
                    tar.add(cfg, arcname="config.toml")
                for sub in ("data", "workspace"):
                    p = home / sub
                    if p.is_dir():
                        tar.add(p, arcname=sub, filter=_filter)
            tar_size = tmp_path.stat().st_size
            meta = _build_meta(agent_id, agent, reason, tar_size, home_size)
            # 第二遍：把 meta 写进包头（tarfile 不支持原地改头，整体重写；
            # 接受 ~2x 压缩成本，换 meta 内 tar_size_bytes 准确）
            with tarfile.open(tmp_path, "w:gz") as tar:
                meta_bytes = json.dumps(meta, ensure_ascii=False, indent=2).encode()
                ti = tarfile.TarInfo(name="meta.json")
                ti.size = len(meta_bytes)
                ti.mtime = int(time.time())
                ti.mode = 0o600
                tar.addfile(ti, io.BytesIO(meta_bytes))
                if cfg.is_file():
                    tar.add(cfg, arcname="config.toml")
                for sub in ("data", "workspace"):
                    p = home / sub
                    if p.is_dir():
                        tar.add(p, arcname=sub, filter=_filter)

            key = make_key(agent_id)
            put_info = be.put(key, tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    finally:
        if proc_active:
            try:
                systemdctl.kill_signal(unit, "SIGCONT")
            except Exception:
                pass  # manager 自己也可能崩，但单元独立；下轮 reconcile 救

    agentops.audit("backup.snapshot", agent=agent_id, key=key,
                   size=put_info["size_bytes"], reason=reason)
    return {**put_info, "meta": meta}


# ── 恢复 ──────────────────────────────────────────────────────────────

def _read_meta_from_tar(path: Path) -> dict:
    """从包内读 meta.json（不落盘）。"""
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.name == "meta.json":
                    f = tar.extractfile(m)
                    if not f:
                        raise BackupError("meta.json 不可读")
                    return json.loads(f.read().decode("utf-8"))
    except tarfile.ReadError as e:
        raise BackupError(f"备份文件读不出（{e}）") from e
    raise BackupError("备份包里没有 meta.json")


def restore(backup_path: Path, *, new_id: str | None = None,
            port: int | None = None, host: str = "127.0.0.1",
            overwrite: bool = False,
            brains: list[str] | None = None,
            note: str | None = None,
            backend: BackupBackend | None = None) -> dict:
    """从本地路径（或 backend 拉的本地路径）恢复 agent 到 instances/。

    流程：解压 → versions 重建 xuseek-v2 → 写注册表 → 启动。
    new_id 冲突时：overwrite=True 强覆盖（先停旧），否则报错。
    brains / note 若非 None，覆盖备份 meta 里的同名字段（克隆对话框用：大脑不是
    备份项目，备注自动写"从备份克隆于 …"）。
    """
    # 同 create_agent：restore 也会写入本地注册表 → 仅 worker 节点可做。
    if not node.can_register_agents():
        raise BackupError(
            f"当前节点 role={get_config().node_role} 不允许本地恢复 agent"
            f"（仅 worker 角色可；backup/portal 节点专用于备份与门户聚合）"
        )
    # 局部别名：参数 `brains` 与模块同名会遮蔽导入；池校验用模块
    from . import brains as _brains_mod
    meta = _read_meta_from_tar(backup_path)
    agent_id = new_id or meta["agent_id"]
    if not _KEY_OK_RE.match(agent_id):
        raise BackupError(f"非法 agent_id {agent_id!r}")

    # 1. 冲突检查
    existing = registry.get_agent(agent_id)
    if existing and not overwrite:
        raise BackupError(
            f"agent {agent_id} 已存在。用 --overwrite 覆盖（先停旧再恢复）"
            f"或 --new-id 改名。")
    # --overwrite 时沿用旧端口（原地复活语义；用户显式 --port 可覆盖）
    preserved_port: int | None = existing.get("port") if existing else None
    if existing and overwrite:
        try:
            agentops.delete(agent_id)  # 内部已 stop + 移 .trash
        except agentops.AgentError as e:
            raise BackupError(f"覆盖失败：{e}") from None

    cfg = get_config()
    home = cfg.instance_home(agent_id)
    if home.exists():
        raise BackupError(f"目标 home 已存在：{home}（请清理后再试）")

    # 2. 解压
    home.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(backup_path, "r:gz") as tar:
            for m in tar.getmembers():
                # 防御：tar-slip
                parts = Path(m.name).parts
                if ".." in parts or m.name.startswith(("/", "\\")):
                    raise BackupError(f"非法包成员：{m.name}")
                if m.name == "meta.json":
                    continue  # 不入 home
                tar.extract(m, home)
        # 解压产物的权限恢复（tar 默认按 mtime/uid 写入）
        ct = home / "config.toml"
        if ct.is_file():
            ct.chmod(0o600)
    except Exception:
        # 失败清理
        import shutil
        shutil.rmtree(home, ignore_errors=True)
        raise

    # 3. 从 versions 重建 xuseek-v2 副本（除非包内带）
    src_dir = home / versions.SRC_DIR_NAME
    if not (src_dir / "xuseek.sh").exists():
        sv = meta.get("source_version") or ""
        if not sv or sv == "main":
            raise BackupError(
                f"备份的 source_version={sv!r} 是过渡期字段；请管理员在 versions/"
                f"投放对应版本包或显式选可用版本（GET /api/versions）。")
        try:
            versions.extract(sv, src_dir)
        except versions.VersionError as e:
            import shutil
            shutil.rmtree(home, ignore_errors=True)
            raise BackupError(f"恢复失败：{e}") from None

    # 4. 写注册表（端口优先级：用户显式 > overwrite 沿用旧端口 > 自动分配）
    if port is not None:
        pass                            # 用户传的优先
    elif preserved_port is not None and ports.port_free(preserved_port):
        port = preserved_port
    else:
        port = ports.allocate(None)
    now = registry.now_iso()
    rec = {
        "id": agent_id,
        "name": meta.get("agent_name", agent_id),
        "mission": meta.get("mission", ""),
        "brains": brains if isinstance(brains, list) and brains else meta.get("brains", []),
        "budgets": meta.get("budgets") or {},
        "expose": bool(meta.get("expose", False)),
        "port": port,
        "desired_state": "running",
        "note": note if isinstance(note, str) else meta.get("note", ""),
        "source_version": meta.get("source_version", ""),
        "created_at": meta.get("created_at", now),
        "updated_at": now,
        "tokens": [],
    }
    # 注册大脑（pool 校验交给 agentops.create_agent；但这里只恢复，不重渲染 config——
    # config.toml 已从包里拷过来含正确 key；create_agent 会重渲染覆盖，破坏恢复一致性）
    # → 改：直接 add + 不触发 spawn，再单独 spawn。
    if not all(b in {p["name"] for p in _brains_mod.pool_summary()} for b in rec["brains"]):
        import shutil
        shutil.rmtree(home, ignore_errors=True)
        raise BackupError(f"大脑池缺失：{rec['brains']}")
    no_key = [b for b in rec["brains"]
              if not next((p for p in _brains_mod.pool_summary() if p["name"] == b), {}).get("has_key")]
    if no_key:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
        raise BackupError(f"大脑缺 key：{no_key}")
    registry.add_agent(rec)

    # 5. spawn
    try:
        from . import systemdctl as _sc
        _sc.spawn_agent(cfg.unit_name(agent_id),
                        source_dir=str(src_dir), home=str(home),
                        host=host, port=port)
    except Exception as e:
        # 启动失败回滚
        import shutil
        try:
            systemdctl.stop(cfg.unit_name(agent_id))
        except Exception:
            pass
        registry.remove_agent(agent_id)
        shutil.rmtree(home, ignore_errors=True)
        raise BackupError(f"spawn 失败：{e}") from e

    # 6. 签发新观察台 token（webui_tokens.json 备份时被排除；新 agent 启动时
    # 不会自带，需调 xuseek CLI 现场 mint + 记注册表）
    try:
        agentops._mint_token(agent_id, "restored")
    except Exception as e:
        # token 签发失败不阻塞恢复——管理员可后续补签
        rec_token_err = str(e)
    else:
        rec_token_err = ""

    agentops.audit("backup.restore", agent=agent_id, port=port,
                   source=meta.get("agent_id"), overwrite=overwrite)
    return {"id": agent_id, "port": port, "home": str(home),
            "restored_from": meta.get("snapshot_at"),
            "token_warning": rec_token_err}


# ── 列表 / 删除 ──────────────────────────────────────────────────────

def list_backups(agent_id: str | None = None,
                 backend: BackupBackend | None = None) -> list[dict]:
    be = backend or LocalBackend()
    prefix = f"xusi-{agent_id}-" if agent_id else "xusi-"
    return be.list(prefix=prefix)


def list_with_meta(agent_id: str | None = None,
                   backend: BackupBackend | None = None) -> list[dict]:
    """list_backups + meta：每行附包内 meta.json（不下载包体）。
    仅 LocalBackend 能读包头；其它 backend（未来 S3）暂留空 meta —— 列表仍可见，
    WebUI 上提示「无可读 meta」。"""
    be = backend or LocalBackend()
    rows = be.list(prefix=f"xusi-{agent_id}-" if agent_id else "xusi-")
    out: list[dict] = []
    for r in rows:
        if isinstance(be, LocalBackend):
            try:
                meta = _read_meta_from_tar(be._path(r["key"]))
            except Exception:
                meta = {}
        else:
            meta = {}
        out.append({**r, "meta": meta})
    return out


def path_of_key(key: str, backend: BackupBackend | None = None) -> Path:
    """通过 backend 解析 key → 本机路径（WebUI /api/restore 用，免下载包体）。
    非 LocalBackend 暂不支持，抛 BackupError。"""
    be = backend or LocalBackend()
    if not isinstance(be, LocalBackend):
        raise BackupError(f"backend {be.name} 尚不支持按 key 解析路径")
    return be._path(key)


def delete_backup(key: str, backend: BackupBackend | None = None) -> None:
    be = backend or LocalBackend()
    be.delete(key)
    agentops.audit("backup.delete", key=key)