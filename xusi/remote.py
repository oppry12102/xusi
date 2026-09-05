"""remote —— 控制端 fan-out：多台远端 xusi 的批操作（纯 ssh/ssh+cat，零新依赖）。

主机清单 etc/hosts.toml（与 WebUI「远端机器」页同源，600 权限）：每台远端
一条 [[host]]，name 唯一。远端是零管理机器：~/work/xusi 自洽目录（全队目录
统一，与本地部署同构）由控制端推送维护（remote install），命令 = ssh 过去
执行 `<python> -m xusi <cmd>`——远端没有 serve 进程，CLI 直调 agentops
（跨进程并发由 registry.file_lock 兜底）。

设计约束：
- 零新依赖：系统 ssh + sshpass（控制端 apt 一行）；不用 paramiko。
- 控制端不存 admin token——CLI 直调不鉴权 HTTP，鉴权就是 ssh 登录。
- 推送 tar 结构性排除数据目录（etc/instances/.git/.venv），永不覆盖远端数据。
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import ROOT, get_config

REMOTE_DIR = "~/work/xusi"   # 远端自洽目录（per-host 可覆盖 dir=）——全队统一在
                             # ~/work/xusi 下，与本地部署/控制端同构（决议 2026-09-05）
REMOTE_PY = "python3.12"     # 远端 python（deadsnakes 3.12；per-host 可覆盖 python=）
SSH_TIMEOUT = "15"


class RemoteError(Exception):
    pass


def hosts_file() -> Path:
    return get_config().etc_dir / "hosts.toml"


# 清单条目白名单（save_hosts 序列化用；未知键丢弃）
HOST_FIELDS = ("name", "host", "port", "user", "password", "key", "dir", "python",
               "brains", "via", "proxy")


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


# ── ssh 通道（ControlMaster 复用 + 链路竞速）─────────────────────────────
#
# 实测（海外机直连）：新建连接 2-4s（握手+认证多轮往返），复用通道单命令
# ~0.6s。所以每条机器一条 ControlMaster 保温长连接（ControlPersist=120s）；
# 多链路候选（direct/via 跳板/proxy 代理）并行计时探测，最快者当选并缓存
# etc/link_cache.json（TTL 600s），连接层失败（ssh rc 255/超时）使缓存失效，
# 下一次调用重新竞速——代理哪天变快会自动换过去。
#
# 传输不用 scp：上传 = ssh+cat（单往返）、下载 = ssh+cat（单往返），
# 免去 scp 的第二条连接。


_COMMON_OPTS = ("-o", "BatchMode=no", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"ConnectTimeout={SSH_TIMEOUT}", "-o", "GSSAPIAuthentication=no")


def _mux_path(h: dict, kind: str, opts: dict) -> str:
    key = (f"{h.get('user')}@{h.get('host')}:{h.get('port', 22)}-{kind}-"
           f"{opts.get('proxy') or opts.get('relay') or ''}")
    return f"/tmp/xusi-mux-{hashlib.sha1(key.encode()).hexdigest()[:12]}"


def _build_ssh(h: dict, kind: str, opts: dict, remote_cmd: str) -> list[str]:
    """完整 ssh 命令：ControlMaster 复用 + 按链路（direct/via/proxy）追加选项。"""
    args: list[str] = []
    if h.get("password"):
        args += ["sshpass", "-p", h["password"]]
    args += ["ssh"] + list(_COMMON_OPTS)
    if h.get("key"):
        args += ["-i", str(Path(h["key"]).expanduser())]
    args += ["-o", "ControlMaster=auto", "-o", f"ControlPath={_mux_path(h, kind, opts)}",
             "-o", "ControlPersist=120"]
    if kind == "proxy":
        m = re.match(r"^(socks5h?|http)://(.+)$", opts.get("proxy", ""))
        if not m:
            raise RemoteError(f"proxy 格式应为 socks5h://host:port（或 socks5:// http://）："
                              f"{opts.get('proxy')!r}")
        scheme, addr = m.group(1), m.group(2)
        x = "5" if scheme.startswith("socks") else "connect"
        args += ["-o", f"ProxyCommand=nc -X {x} -x {addr} %h %p"]
    elif kind == "via":
        relay = find_host(opts["relay"])
        inner = ["ssh"] + list(_COMMON_OPTS) + ["-p", str(relay.get("port", 22)),
                                                "-W", "%h:%p",
                                                f"{relay['user']}@{relay['host']}"]
        if relay.get("key"):
            inner += ["-i", str(Path(relay["key"]).expanduser())]
        inner_str = " ".join(inner)
        if relay.get("password"):
            inner_str = f"sshpass -p {shlex.quote(relay['password'])} {inner_str}"
        args += ["-o", f"ProxyCommand={inner_str}"]
    args += ["-p", str(h.get("port", 22)), f"{h['user']}@{h['host']}", remote_cmd]
    return args


def _link_candidates(h: dict) -> list[tuple[str, dict]]:
    """链路候选：direct 恒在；via（清单里的跳板机）/ proxy（socks5h/socks5/http）
    配了才参与竞速。"""
    cands: list[tuple[str, dict]] = [("direct", {})]
    if h.get("via"):
        cands.append(("via", {"relay": h["via"]}))
    if h.get("proxy"):
        cands.append(("proxy", {"proxy": h["proxy"]}))
    return cands


_LINK_TTL = 600.0


def _link_cache_file() -> Path:
    return get_config().etc_dir / "link_cache.json"


def _load_link_cache() -> dict:
    try:
        data = json.loads(_link_cache_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_link_cache(data: dict) -> None:
    f = _link_cache_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def invalid_link(h: dict) -> None:
    """连接层失败后失效该机缓存——下一次调用重新竞速（写命令不做自动重试，
    避免双发；WebUI 30s 轮询自然重试，会走新链路）。"""
    key = h.get("name") or h.get("host")
    data = _load_link_cache()
    if key in data:
        del data[key]
        _save_link_cache(data)


def _probe(h: dict, kind: str, opts: dict, timeout: int = 18) -> float | None:
    """计时探测一条链路（冷连接全程：握手+认证+echo）。探测同时把该链路的
    ControlMaster 保温（ControlPersist）——当选链路即是暖通道，后续命令免握手。"""
    t0 = time.monotonic()
    try:
        cp = subprocess.run(_build_ssh(h, kind, opts, "echo ok"),
                            capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, RemoteError):
        return None
    if cp.returncode != 0:
        return None
    return time.monotonic() - t0


def resolve_link(h: dict) -> tuple[str, dict]:
    """链路竞速：多候选时并行探测，最快者当选并缓存（TTL 600s）；全失败回
    direct。单候选（只有直连）直接返回，不探测。"""
    cands = _link_candidates(h)
    if len(cands) == 1:
        return cands[0]
    key = h.get("name") or h.get("host")
    cache = _load_link_cache()
    ent = cache.get(key) or {}
    link = ent.get("link")
    if link and time.time() - float(ent.get("at", 0)) < _LINK_TTL:
        for kind, opts in cands:
            if kind == link and opts == ent.get("opts"):
                return (kind, opts)
    with ThreadPoolExecutor(max_workers=len(cands)) as ex:
        futs = {i: ex.submit(_probe, h, kind, opts)
                for i, (kind, opts) in enumerate(cands)}
        res = {i: f.result() for i, f in futs.items()}
    ok = [(t, i) for i, t in res.items() if t is not None]
    if not ok:
        return ("direct", {})
    best_i = min(ok)[1]
    kind, opts = cands[best_i]
    cache[key] = {"link": kind, "opts": opts, "at": time.time()}
    _save_link_cache(cache)
    return (kind, opts)


def run_remote(h: dict, cmd: str, *, timeout: int = 300) -> subprocess.CompletedProcess:
    """在远端执行一条 shell 命令（非交互，输出捕获；链路自动竞速）。"""
    kind, opts = resolve_link(h)
    try:
        cp = subprocess.run(_build_ssh(h, kind, opts, cmd), capture_output=True,
                            text=True, timeout=timeout)
    except FileNotFoundError:
        raise RemoteError("本机缺少 ssh/sshpass（或 proxy 链路缺 nc）——控制端先 "
                          "sudo apt-get install sshpass netcat-openbsd")
    except subprocess.TimeoutExpired:
        invalid_link(h)
        raise RemoteError(f"远端命令超时（{timeout}s）：{cmd[:80]}")
    if cp.returncode == 255:
        invalid_link(h)   # ssh 连接层失败（连不上/认证失败）——下次重新竞速
    return cp


def xusi_cmd(h: dict, argv: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    """远端执行 `cd <dir> && <python> -m xusi <argv>`——远端 xusi 的自洽目录
    就是它的 cwd（模块路径 + 注册表/instances 都锚定那里）。"""
    d = h.get("dir", REMOTE_DIR)
    py = h.get("python", REMOTE_PY)
    inner = " ".join(shlex.quote(a) for a in argv)
    return run_remote(h, f"cd {d} && {py} -m xusi {inner}", timeout=timeout)


def scp_to(h: dict, local: Path, remote_path: str) -> None:
    """上传：ssh + cat 单往返（mkdir 与写文件合并一条命令）。"""
    kind, opts = resolve_link(h)
    d = remote_path.rsplit("/", 1)[0] if "/" in remote_path else "."
    cmd = f"mkdir -p {shlex.quote(d)} && cat > {shlex.quote(remote_path)}"
    try:
        cp = subprocess.run(_build_ssh(h, kind, opts, cmd), input=local.read_bytes(),
                            capture_output=True, timeout=180)
    except FileNotFoundError:
        raise RemoteError("本机缺少 ssh/sshpass——控制端先 sudo apt-get install sshpass")
    if cp.returncode != 0:
        err = (cp.stderr or b"").decode(errors="replace")[:200]
        raise RemoteError(f"上传失败：{err}")


def scp_from(h: dict, remote_path: str, local: Path) -> None:
    """下载：ssh + cat 单往返（stdout 原样落盘，二进制安全）。"""
    kind, opts = resolve_link(h)
    cp = subprocess.run(_build_ssh(h, kind, opts, f"cat {shlex.quote(remote_path)}"),
                        capture_output=True, timeout=300)
    if cp.returncode != 0:
        err = (cp.stderr or b"").decode(errors="replace")[:200]
        raise RemoteError(f"下载失败：{err}")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(cp.stdout)


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
    """单机 status：`test -d`、可收编探测与 `xusi status --json` 合并成一条
    ssh 命令（免二次握手）→ {host, installed, rows} / {host, installed,
    error} / {host, installed=False, adoptable_root}。"""
    d = h.get("dir", REMOTE_DIR)
    py = h.get("python", REMOTE_PY)
    inner = " ".join(shlex.quote(a) for a in ["status", "--json"])
    cmd = (f"if [ -d {d} ]; then cd {d} && {py} -m xusi {inner}; "
           f'else wd=$(systemctl --user cat xusi.service 2>/dev/null '
           f'| grep -m1 "^WorkingDirectory=" | cut -d= -f2-); '
           f'if [ -n "$wd" ]; then echo __XUSI_ADOPTABLE__ "$wd"; '
           f'else echo __XUSI_NOT_INSTALLED__; fi; fi')
    cp = run_remote(h, cmd, timeout=timeout)
    out = cp.stdout or ""
    if "__XUSI_ADOPTABLE__" in out:
        root = out.split("__XUSI_ADOPTABLE__", 1)[1].strip().splitlines()[0]
        return {"host": h.get("name", ""), "installed": False,
                "adoptable_root": root}
    if "__XUSI_NOT_INSTALLED__" in out:
        return {"host": h.get("name", ""), "installed": False, "rows": []}
    if cp.returncode != 0:
        return {"host": h.get("name", ""), "installed": True,
                "error": (cp.stderr or cp.stdout).strip()[:200]}
    try:
        rows = json.loads(out)
    except Exception:
        return {"host": h.get("name", ""), "installed": True,
                "error": "输出不是 JSON（远端版本过旧？先 remote upgrade）"}
    return {"host": h.get("name", ""), "installed": True, "rows": rows}


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
    """打代码 tar → ssh+cat 上传 → 解压到远端自洽目录的父目录（tar 内路径以
    xusi/ 开头、目录名锚定——只覆盖代码目录；数据目录结构性免疫）。
    前提：dir 的末段必须是 xusi（零管理机用缺省 ~/work/xusi 即满足）。"""
    tar = build_code_tar()
    d = h.get("dir", REMOTE_DIR)
    parent = d.rsplit("/", 1)[0] if "/" in d else "."
    try:
        scp_to(h, tar, "/tmp/xusi-code.tgz")
    finally:
        shutil.rmtree(tar.parent, ignore_errors=True)
    cp = run_remote(h, f"mkdir -p {parent} && tar xzf /tmp/xusi-code.tgz -C {parent} "
                       f"&& rm -f /tmp/xusi-code.tgz", timeout=120)
    if cp.returncode != 0:
        raise RemoteError(f"远端解压失败：{(cp.stderr or '').strip()[:200]}")


def reset_mux(h: dict) -> None:
    """关掉该机全部链路的保温连接——强制下一命令走新会话（如刚改过用户组，
    复用中的会话还带旧组）。"""
    target = f"{h['user']}@{h['host']}"
    for kind, opts in _link_candidates(h):
        subprocess.run(["ssh", "-O", "exit", "-o",
                        f"ControlPath={_mux_path(h, kind, opts)}", target],
                       capture_output=True)


def install_host(h: dict):
    """新机接入（讨论稿 §七引导清单，含环境检查与配齐）：sudo 检查 → python3.12
    （deadsnakes）→ docker（缺省运行时：缺失则装、用户不在组则加、验证可用）
    → linger → 推代码 tar → 播种 brains → doctor 自检。幂等：已就绪的步骤跳过。
    返回步骤日志（含 doctor 输出）。"""
    def step(cmd: str, desc: str, timeout: int = 900) -> None:
        yield desc
        cp = run_remote(h, cmd, timeout=timeout)
        if cp.returncode != 0:
            out = (cp.stderr or cp.stdout).strip()[-400:]
            raise RemoteError(f"{desc} 失败：{out}")

    # ① 环境检查：sudo 可用性（免密或密码同登录密码——装 python/docker 全靠它）
    yield "环境检查：sudo…"
    cp = run_remote(h, f"{_sudo(h, 'true')}", timeout=60)
    if cp.returncode != 0:
        raise RemoteError("sudo 不可用（免密或密码同登录密码都试过）——"
                          "请先在远端配好 sudo，或把 sudo 密码写进清单 password 字段")

    # ② python3.12（deadsnakes，与主流一致可升级）
    py = h.get("python", REMOTE_PY)
    cp = run_remote(h, f"{py} --version 2>/dev/null", timeout=30)
    if cp.returncode != 0:
        yield from step(f"{_sudo(h, 'apt-get update -qq')} && "
             f"{_sudo(h, 'apt-get install -y -qq software-properties-common')} && "
             f"{_sudo(h, 'add-apt-repository -y ppa:deadsnakes/ppa')} && "
             f"{_sudo(h, f'apt-get install -y {py} {py}-venv')}",
             f"安装 {py} + venv（deadsnakes PPA）…", timeout=1200)
    else:
        yield f"{py} 已就绪，跳过安装"

    # ③ docker（缺省运行时——缺了就装、组没加就加、最终验证可用）
    cp = run_remote(h, "docker --version 2>/dev/null", timeout=30)
    if cp.returncode != 0:
        yield from step(_sudo(h, "apt-get install -y -qq docker.io docker-compose-v2"),
             "安装 docker.io + compose v2…", timeout=1200)
    else:
        yield "docker 已安装"
    cp = run_remote(h, "docker info >/dev/null 2>&1 && echo __OK__", timeout=60)
    if "__OK__" not in (cp.stdout or ""):
        yield from step(_sudo(h, "usermod -aG docker $(id -un)"), "加入 docker 组…", timeout=60)
        reset_mux(h)   # 复用中的会话还带旧组——关掉保温连接，验证走新会话
        cp = run_remote(h, "docker info >/dev/null 2>&1 && echo __OK__", timeout=60)
        if "__OK__" not in (cp.stdout or ""):
            yield "  提示：docker 组已加但当前仍不可用——创建容器 agent 前重连（新 ssh 会话即生效）"
        else:
            yield "  docker 可用 ✓"
    else:
        yield "  docker 可用 ✓"
    # compose 插件独立检查：docker 本体装了不等于有 compose（docker.io 不带）——
    # 缺省运行时渲染 compose 靠它，缺了就单独装
    cp = run_remote(h, "docker compose version >/dev/null 2>&1 && echo __OK__",
                    timeout=60)
    if "__OK__" not in (cp.stdout or ""):
        yield from step(_sudo(h, "apt-get install -y -qq docker-compose-v2"),
                        "安装 docker compose 插件…", timeout=1200)
    else:
        yield "  docker compose 插件 ✓"

    # ④ linger（ssh 断开会话死 → agent 单元死）
    yield from step(_sudo(h, "loginctl enable-linger $(id -un)"), "开启用户会话常驻（linger）…",
                     timeout=60)
    yield "推送代码包（xusi/ + docs/ + versions/）…"
    _push_code(h)
    # 播种密钥池：per-host brains 字段 > 控制端自己的 etc/brains.toml（决议② 全队同份）
    yield "播种密钥池（600）…"
    push_brains(h)
    yield "doctor --mode cli 自检："
    cp = xusi_cmd(h, ["doctor", "--mode", "cli"], timeout=300)
    yield (cp.stdout or "") + (cp.stderr or "")
    if cp.returncode != 0:
        raise RemoteError("远端 doctor 未全过（见输出）")


def push_brains(h: dict) -> None:
    """把控制端密钥池推到远端（600）：per-host brains 字段 > 控制端自己的
    etc/brains.toml（决议② 全队同份）。install 播种同源（同一条实现）——
    轮换 key / 大脑池变更后全队执行：`xusi remote brains --on H`。"""
    d = h.get("dir", REMOTE_DIR)
    seed = h.get("brains") or str(get_config().brains_file)
    scp_to(h, Path(seed).expanduser().resolve(), "/tmp/xusi-brains.toml")
    cp = run_remote(h, f"mkdir -p {d}/etc && mv /tmp/xusi-brains.toml {d}/etc/brains.toml "
                       f"&& chmod 600 {d}/etc/brains.toml", timeout=60)
    if cp.returncode != 0:
        raise RemoteError(f"落盘 brains.toml 失败：{(cp.stderr or cp.stdout).strip()[-200:]}")


def upgrade_host(h: dict) -> None:
    """升级分两种形态（自动检测）：
    - 零管理推 tar 机器（无 .git）→ 重推代码 tar（控制端 repo 即全队事实源）；
    - 收编的存量部署（git checkout）→ git pull origin main——不推 tar，
      避免覆盖它的工作树（本地 docs 等未跟踪文件）与 git 状态。"""
    d = h.get("dir", REMOTE_DIR)
    cp = run_remote(h, f"[ -d {d}/.git ] && echo __XUSI_GIT__", timeout=30)
    if cp.returncode == 0 and "__XUSI_GIT__" in (cp.stdout or ""):
        cp = run_remote(h, f"cd {d} && git pull origin main", timeout=300)
        if cp.returncode != 0:
            raise RemoteError(f"git pull 失败：{(cp.stderr or cp.stdout).strip()[-300:]}")
        return
    _push_code(h)


def _detect_root(h: dict) -> str:
    """探测既有部署根：serve 单元的 WorkingDirectory 优先（停了 serve 的机器
    单元文件仍在），退而查常见路径。找不到 = 空机（走 install）。"""
    cp = run_remote(h, 'systemctl --user cat xusi.service 2>/dev/null '
                       '| grep -m1 "^WorkingDirectory=" | cut -d= -f2-', timeout=30)
    root = (cp.stdout or "").strip()
    if root:
        return root
    for cand in (REMOTE_DIR, "~/xusi"):
        cp = run_remote(h, f"[ -d {cand} ] && echo {cand}", timeout=30)
        if (cp.stdout or "").strip():
            return (cp.stdout or "").strip()
    raise RemoteError("找不到既有 xusi 部署根——空机请用 remote install，"
                      "或手填清单 dir 字段")


def adopt_host(h: dict):
    """收编存量部署（自动化四步，幂等）：探测部署根 → 回写清单 dir/python →
    升级（git pull / 推 tar 自动分派）→ 停+禁 serve（单头原则）→ doctor 验证。

    不触碰 agent：注册表/实例目录原样接管，收编后既有 agent 出现在全队 status。"""
    yield "探测既有部署根…"
    root = _detect_root(h)
    yield f"  部署根：{root}"
    h["dir"] = root
    if not h.get("python"):
        cp = run_remote(h, f"[ -x {root}/.venv/bin/python ] && echo VENV", timeout=30)
        if "VENV" in (cp.stdout or ""):
            h["python"] = ".venv/bin/python"
    # 回写清单（dir 与缺省一致不写；python 与缺省不一致才写——保持清单最小）
    saved = dict(h)
    if saved.get("dir") == REMOTE_DIR:
        saved.pop("dir", None)
    if not saved.get("python") or saved.get("python") == REMOTE_PY:
        saved.pop("python", None)
    try:
        hosts = load_hosts()
        for i, x in enumerate(hosts):
            if x.get("name") == h.get("name"):
                hosts[i] = saved
        save_hosts(hosts)
        yield f"  清单已回写：dir={h.get('dir')} python={h.get('python')}"
    except RemoteError as e:
        yield f"  清单回写失败（可手填）：{e}"
    yield "升级代码…"
    upgrade_host(h)
    yield "停 + 禁 serve（单头原则，幂等）…"
    _stop_serve(h)
    yield "doctor --mode cli 自检："
    cp = xusi_cmd(h, ["doctor", "--mode", "cli"], timeout=300)
    yield (cp.stdout or "") + (cp.stderr or "")
    if cp.returncode != 0:
        raise RemoteError("收编机 doctor 未全过（见输出；刚停 serve 30s 内端口"
                          "TIME_WAIT 误报属预期，稍候重试）")


def _stop_serve(h: dict) -> None:
    """停 + 禁收编机的 xusi.service（单头原则：管理只走控制端）。已停/无单元
    的机器幂等无害。"""
    cp = run_remote(h, "systemctl --user disable --now xusi.service 2>&1 || true",
                    timeout=60)
    if cp.returncode != 0:
        raise RemoteError(f"停 serve 失败：{(cp.stderr or '').strip()[:200]}")


def backup_host(h: dict, agent_id: str, out_dir: Path) -> Path:
    """远端 `xusi backup` → 取最新备份包 scp 回控制端 out_dir/。"""
    cp = xusi_cmd(h, ["backup", agent_id], timeout=600)
    if cp.returncode != 0:
        raise RemoteError(f"远端备份失败：{(cp.stderr or cp.stdout).strip()[-300:]}")
    d = h.get("dir", REMOTE_DIR)
    cp = run_remote(h, f"ls -t {d}/etc/backups/*.tar.gz 2>/dev/null | head -1", timeout=30)
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


def remote_patch(h: dict, agent_id: str, body: dict, *, apply_restart: bool = False,
                 timeout: int = 180) -> dict:
    """远端改参：inline python 直调 agentops.patch_agent——远端零管理机没有
    serve（PATCH 无 HTTP 通道），与本地 WebUI 同一条实现。body 走 argv 传
    JSON（shlex.quote 双层防护），键由远端 _PATCHABLE 白名单把关
    （brains/name/note/expose/runtime），不可改字段得到同样的友好 400 文案。"""
    d = h.get("dir", REMOTE_DIR)
    py = h.get("python", REMOTE_PY)
    code = ("import json,sys; from xusi import agentops; "
            f"r = agentops.patch_agent(sys.argv[1], json.loads(sys.argv[2]), "
            f"apply_restart={bool(apply_restart)}); "
            "print(json.dumps(r, ensure_ascii=False))")
    body_json = json.dumps(body, ensure_ascii=False)
    cp = run_remote(h, f"cd {d} && {py} -c {shlex.quote(code)} "
                     f"{shlex.quote(agent_id)} {shlex.quote(body_json)}",
                    timeout=timeout)
    if cp.returncode != 0:
        # 取 stderr 最后一行 = AgentError 的可读文案（截掉 traceback 噪音）
        err = (cp.stderr or cp.stdout or "").strip().splitlines()
        msg = err[-1] if err else "未知错误"
        if msg.startswith("xusi.agentops.AgentError: "):
            msg = msg[len("xusi.agentops.AgentError: "):]
        raise RemoteError(f"远端改参失败：{msg}")
    try:
        return json.loads(cp.stdout)
    except Exception:
        raise RemoteError("远端改参输出不是 JSON（远端版本过旧？先 remote upgrade）")


# agent 家目录下允许 ssh tail 读取的只读数据文件（白名单，防路径注入）
_READABLE_FILES = ("data/sessions.jsonl", "data/outbox.jsonl", "data/mailbox_log.jsonl")
# agent id 字符级白名单：本机新生成是 agent-<4hex>，但存量老机还有 llm-N-xxxx
# 等老命名（id 前缀不统一、不可枚举）——id 只用于拼实例目录名与 ssh 参数
# （ssh 侧已 shlex.quote），字符级白名单即可防路径注入/越权；按前缀枚举反而漏掉老命名。
_AGENT_ID_RE = r"[a-z0-9][a-z0-9-]{0,63}"


def read_remote_file(h: dict, agent_id: str, rel: str, *, limit: int = 50,
                     timeout: int = 60) -> list[dict]:
    """ssh tail 读远端 agent 磁盘 JSONL（会话索引/邮箱等只读数据）——文件通道，
    与「不反代」原则一致。agent_id 与路径都走白名单，防注入。"""
    import re
    if not re.fullmatch(_AGENT_ID_RE, agent_id):
        raise RemoteError(f"非法 agent_id：{agent_id!r}")
    if rel not in _READABLE_FILES:
        raise RemoteError(f"不在可读白名单：{rel!r}")
    d = h.get("dir", REMOTE_DIR)
    cmd = (f"tail -n {int(limit)} {d}/instances/{shlex.quote(agent_id)}/{rel} "
           f"2>/dev/null")
    cp = run_remote(h, cmd, timeout=timeout)
    rows = []
    for line in (cp.stdout or "").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass   # 半行等坏 JSON 跳过，与 agentops._tail_jsonl 同构
    return rows
