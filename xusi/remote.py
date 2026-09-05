"""remote —— 控制端 fan-out：多台远端 xusi 的批操作（纯 ssh/scp，零新依赖）。

主机清单 etc/hosts.toml（与 WebUI「远端机器」页同源，600 权限）：每台远端
一条 [[host]]，name 唯一。远端是零管理机器：~/xusi 自洽目录由控制端推送维护
（remote install），命令 = ssh 过去执行 `<python> -m xusi <cmd>`——远端没有
serve 进程，CLI 直调 agentops（跨进程并发由 registry.file_lock 兜底）。

设计约束：
- 零新依赖：系统 ssh/scp + sshpass（控制端 apt 一行）；不用 paramiko。
- 控制端不存 admin token——CLI 直调不鉴权 HTTP，鉴权就是 ssh 登录。
- 推送 tar 结构性排除数据目录（etc/instances/.git/.venv），永不覆盖远端数据。
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import ROOT, get_config

REMOTE_DIR = "~/xusi"        # 远端自洽目录（per-host 可覆盖 dir=）
REMOTE_PY = "python3.12"     # 远端 python（deadsnakes 3.12；per-host 可覆盖 python=）
SSH_TIMEOUT = "15"


class RemoteError(Exception):
    pass


def hosts_file() -> Path:
    return get_config().etc_dir / "hosts.toml"


# 清单条目白名单（save_hosts 序列化用；未知键丢弃）
HOST_FIELDS = ("name", "host", "port", "user", "password", "key", "dir", "python", "brains")


def load_hosts(*, missing_ok: bool = False) -> list[dict]:
    f = hosts_file()
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return []
        raise RemoteError(f"主机清单不存在：{f}——先建 etc/hosts.toml"
                          f"（格式见 docs/remote.md，或用 WebUI「远端机器」页）")
    except Exception as e:
        raise RemoteError(f"主机清单解析失败：{e}")
    hosts = data.get("host", [])
    if not isinstance(hosts, list):
        raise RemoteError("hosts.toml 需要 [[host]] 数组")
    for h in hosts:
        for key in ("name", "host", "user"):
            if not h.get(key):
                raise RemoteError(f"host 条目缺 {key!r} 字段：{h}")
    return [dict(h) for h in hosts]


def _dump_hosts(hosts: list[dict]) -> str:
    lines = [
        "# 远端机器清单（控制端 fan-out；WebUI「远端机器」页同源维护）",
        "# password 先明文（文件 600）；有 key 则 ssh 优先走 key。",
        "",
    ]
    for h in hosts:
        lines.append("[[host]]")
        for key in HOST_FIELDS:
            v = h.get(key)
            if v is None or v == "":
                continue
            if isinstance(v, str):
                lines.append(f'{key} = {json.dumps(v, ensure_ascii=False)}')  # TOML 基本串
            else:
                lines.append(f"{key} = {v}")
        lines.append("")
    return "\n".join(lines)


def save_hosts(hosts: list[dict]) -> None:
    """整表替换写盘（原子 + 600）。条目字段白名单（HOST_FIELDS）之外丢弃；
    port 归一为 int；name/host/user 三者缺一报错。CLI 与 WebUI 共用。"""
    norm = []
    for h in hosts:
        rec: dict = {}
        for k in HOST_FIELDS:
            v = h.get(k)
            if v is None or v == "":
                continue
            rec[k] = int(v) if k == "port" else str(v)
        if not rec.get("name") or not rec.get("host") or not rec.get("user"):
            raise RemoteError(f"host 条目缺 name/host/user 字段：{rec.get('name', rec)}")
        norm.append(rec)
    f = hosts_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".toml.tmp")
    tmp.write_text(_dump_hosts(norm), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(f)


def find_host(name: str) -> dict:
    for h in load_hosts():
        if h.get("name") == name:
            return h
    raise RemoteError(f"清单里没有这台机器：{name}")


# ── ssh/scp 通道 ────────────────────────────────────────────────────────


def _ssh_prefix(h: dict) -> list[str]:
    args: list[str] = []
    if h.get("password"):
        args += ["sshpass", "-p", h["password"]]
    args += ["ssh", "-o", "BatchMode=no", "-o", "StrictHostKeyChecking=accept-new",
             "-o", f"ConnectTimeout={SSH_TIMEOUT}", "-p", str(h.get("port", 22))]
    if h.get("key"):
        args += ["-i", str(Path(h["key"]).expanduser())]
    args.append(f"{h['user']}@{h['host']}")
    return args


def _scp_prefix(h: dict, direction: str) -> list[str]:
    """direction: "to" → scp 本地→远端；"from" → scp 远端→本地。"""
    args: list[str] = []
    if h.get("password"):
        args += ["sshpass", "-p", h["password"]]
    args += ["scp", "-o", "StrictHostKeyChecking=accept-new",
             "-o", f"ConnectTimeout={SSH_TIMEOUT}", "-P", str(h.get("port", 22))]
    if h.get("key"):
        args += ["-i", str(Path(h["key"]).expanduser())]
    if direction == "to":
        args.append("-q")
    args.append("")
    # 占位：调用方替换 args[-1] 为 [src, dst]
    return args


def run_remote(h: dict, cmd: str, *, timeout: int = 300) -> subprocess.CompletedProcess:
    """在远端执行一条 shell 命令（非交互，输出捕获）。"""
    try:
        return subprocess.run(_ssh_prefix(h) + [cmd], capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        raise RemoteError("本机缺少 ssh/sshpass——控制端先 sudo apt-get install sshpass")
    except subprocess.TimeoutExpired:
        raise RemoteError(f"远端命令超时（{timeout}s）：{cmd[:80]}")


def xusi_cmd(h: dict, argv: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    """远端执行 `cd <dir> && <python> -m xusi <argv>`——远端 xusi 的自洽目录
    就是它的 cwd（模块路径 + 注册表/instances 都锚定那里）。"""
    d = h.get("dir", REMOTE_DIR)
    py = h.get("python", REMOTE_PY)
    inner = " ".join(shlex.quote(a) for a in argv)
    return run_remote(h, f"cd {d} && {py} -m xusi {inner}", timeout=timeout)


def scp_to(h: dict, local: Path, remote_path: str) -> None:
    d = remote_path.rsplit("/", 1)[0] if "/" in remote_path else "."
    cp = run_remote(h, f"mkdir -p {shlex.quote(d)}", timeout=30)
    if cp.returncode != 0:
        raise RemoteError(f"远端建目录失败：{(cp.stderr or '').strip()[:200]}")
    args = _scp_prefix(h, "to")
    args[-1] = f"{h['user']}@{h['host']}:{remote_path}"
    cp = subprocess.run(args[:-1] + [str(local), args[-1]], capture_output=True, text=True)
    if cp.returncode != 0:
        raise RemoteError(f"scp 上传失败：{(cp.stderr or '').strip()[:200]}")


def scp_from(h: dict, remote_path: str, local: Path) -> None:
    args = _scp_prefix(h, "from")
    args[-1] = f"{h['user']}@{h['host']}:{remote_path}"
    local.parent.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(args[:-1] + [args[-1], str(local)], capture_output=True, text=True)
    if cp.returncode != 0:
        raise RemoteError(f"scp 下载失败：{(cp.stderr or '').strip()[:200]}")


# ── 代码 tar（推送载体）─────────────────────────────────────────────────


def build_code_tar() -> Path:
    """打代码包：xusi/ + docs/ + versions/（结构性排除 etc/instances/.git/.venv
    ——数据目录永远不进 tar，推送不可能覆盖远端数据）。"""
    root = ROOT.parent
    tar_path = Path(tempfile.mkdtemp(prefix="xusi-tar-")) / "xusi-code.tgz"
    with tarfile.open(tar_path, "w:gz") as tf:
        for sub in ("xusi/xusi", "xusi/docs", "xusi/versions"):
            for p in sorted((root / sub).rglob("*")):
                if "__pycache__" in p.parts or p.suffix == ".pyc":
                    continue
                tf.add(p, arcname=p.relative_to(root))
    return tar_path


# ── 单机操作（远端 argv 组装）───────────────────────────────────────────


def _files_to_remote(h: dict, argv: list[str]) -> list[str]:
    """把指向本地文件的参数搬到远端：@file（mission/extra-config）与 --spec 的
    路径先 scp 到远端临时目录，再改写为远端路径。其余原样透传。"""
    out = argv[:]
    for i, a in enumerate(out):
        if a.startswith("@") and len(a) > 1:
            local = Path(a[1:]).expanduser().resolve()
            remote_path = f"/tmp/xusi-remote/{local.name}"
            scp_to(h, local, remote_path)
            out[i] = "@" + remote_path
        elif a == "--spec" and i + 1 < len(out):
            local = Path(out[i + 1]).expanduser().resolve()
            remote_path = f"/tmp/xusi-remote/{local.name}"
            scp_to(h, local, remote_path)
            out[i + 1] = remote_path
    return out


def remote_status(h: dict, *, timeout: int = 60) -> dict:
    """单机 status：远端 `xusi status --json` → {host, rows} 或 {host, error}。"""
    cp = xusi_cmd(h, ["status", "--json"], timeout=timeout)
    if cp.returncode != 0:
        return {"host": h.get("name", ""), "error": (cp.stderr or cp.stdout).strip()[:200]}
    try:
        rows = json.loads(cp.stdout)
    except Exception:
        return {"host": h.get("name", ""), "error": "输出不是 JSON（远端版本过旧？）"}
    return {"host": h.get("name", ""), "rows": rows}


def fan_out(fn, hosts: list[dict]) -> list[dict]:
    """并行 fan-out：保持清单顺序返回。"""
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(hosts)))) as ex:
        return list(ex.map(fn, hosts))


def remote_create(h: dict, argv: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess:
    return xusi_cmd(h, ["create"] + _files_to_remote(h, argv), timeout=timeout)


def remote_agent_op(h: dict, op: str, argv: list[str], *, timeout: int = 300
                    ) -> subprocess.CompletedProcess:
    return xusi_cmd(h, [op] + argv, timeout=timeout)


# ── 接入 / 升级 / 备份 / 恢复 ────────────────────────────────────────────


def _sudo(h: dict, cmd: str) -> str:
    """sudo 包装：免密用 -n；有密码（清单里那份，ubuntu 默认同 sudo 密码）则
    echo | sudo -S（先明文阶段；进程列表短暂可见，接受）。"""
    if h.get("password"):
        return f"echo {shlex.quote(h['password'])} | sudo -S {cmd}"
    return f"sudo -n {cmd}"


def _push_code(h: dict) -> None:
    """打代码 tar → scp → 解压到 ~/xusi（只覆盖代码目录；数据目录结构性免疫）。"""
    tar = build_code_tar()
    try:
        scp_to(h, tar, "/tmp/xusi-code.tgz")
    finally:
        shutil.rmtree(tar.parent, ignore_errors=True)
    cp = run_remote(h, "tar xzf /tmp/xusi-code.tgz -C ~ && rm -f /tmp/xusi-code.tgz",
                    timeout=120)
    if cp.returncode != 0:
        raise RemoteError(f"远端解压失败：{(cp.stderr or '').strip()[:200]}")


def install_host(h: dict) -> list[str]:
    """新机接入（讨论稿 §七引导清单）：python3.12（deadsnakes，与主流一致可升级）
    → linger → 推代码 tar → 播种 brains → doctor 自检。幂等：已就绪的步骤跳过。
    返回步骤日志（含 doctor 输出）。"""
    logs: list[str] = []

    def step(cmd: str, desc: str, timeout: int = 900) -> None:
        logs.append(desc)
        cp = run_remote(h, cmd, timeout=timeout)
        if cp.returncode != 0:
            out = (cp.stderr or cp.stdout).strip()[-400:]
            raise RemoteError(f"{desc} 失败：{out}")

    py = h.get("python", REMOTE_PY)
    cp = run_remote(h, f"{py} --version 2>/dev/null", timeout=30)
    if cp.returncode != 0:
        step(f"{_sudo(h, 'apt-get update -qq')} && "
             f"{_sudo(h, 'apt-get install -y -qq software-properties-common')} && "
             f"{_sudo(h, 'add-apt-repository -y ppa:deadsnakes/ppa')} && "
             f"{_sudo(h, f'apt-get install -y {py} {py}-venv')}",
             f"安装 {py} + venv（deadsnakes PPA）…", timeout=1200)
    else:
        logs.append(f"{py} 已就绪，跳过安装")
    step(_sudo(h, "loginctl enable-linger $(id -un)"), "开启用户会话常驻（linger）…",
         timeout=60)
    logs.append("推送代码包（xusi/ + docs/ + versions/）…")
    _push_code(h)
    # 播种密钥池：per-host brains 字段 > 控制端自己的 etc/brains.toml（决议② 全队同份）
    seed = h.get("brains") or str(get_config().brains_file)
    logs.append("播种密钥池（600）…")
    scp_to(h, Path(seed).expanduser().resolve(), "/tmp/xusi-brains.toml")
    step("mkdir -p ~/xusi/etc && mv /tmp/xusi-brains.toml ~/xusi/etc/brains.toml "
         "&& chmod 600 ~/xusi/etc/brains.toml", "落盘 brains.toml…", timeout=60)
    logs.append("doctor --mode cli 自检：")
    cp = xusi_cmd(h, ["doctor", "--mode", "cli"], timeout=300)
    logs.append((cp.stdout or "") + (cp.stderr or ""))
    if cp.returncode != 0:
        raise RemoteError("远端 doctor 未全过（见输出）")
    return logs


def upgrade_host(h: dict) -> None:
    """重推代码 tar（xusi/ + docs/ + versions/）：管理面升级与内核版本发布
    都是这一条——控制端 repo 即全队事实源。"""
    _push_code(h)


def backup_host(h: dict, agent_id: str, out_dir: Path) -> Path:
    """远端 `xusi backup` → 取最新备份包 scp 回控制端 out_dir/。"""
    cp = xusi_cmd(h, ["backup", agent_id], timeout=600)
    if cp.returncode != 0:
        raise RemoteError(f"远端备份失败：{(cp.stderr or cp.stdout).strip()[-300:]}")
    cp = run_remote(h, "ls -t ~/xusi/etc/backups/*.tar.gz 2>/dev/null | head -1", timeout=30)
    remote_path = cp.stdout.strip()
    if not remote_path:
        raise RemoteError("远端备份目录里没有包（backup 命令未产出？）")
    out_dir.mkdir(parents=True, exist_ok=True)
    local = out_dir / Path(remote_path).name
    scp_from(h, remote_path, local)
    return local


def restore_host(h: dict, local_tar: Path, argv: list[str],
                 *, timeout: int = 600) -> subprocess.CompletedProcess:
    """备份包推上远端 → 远端 `xusi restore --from <远端路径> <argv>`。"""
    local = local_tar.expanduser().resolve()
    if not local.is_file():
        raise RemoteError(f"备份文件不存在：{local}")
    remote_path = f"/tmp/xusi-remote/{local.name}"
    scp_to(h, local, remote_path)
    return xusi_cmd(h, ["restore", "--from", remote_path] + list(argv), timeout=timeout)
