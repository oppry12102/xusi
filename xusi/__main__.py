"""墟司 CLI：

    python -m xusi serve                 # 前台跑管理面（调试用；常驻走 install）
    python -m xusi install               # 建 venv → 装 systemd 用户服务 → 启动
    python -m xusi init                  # 首次安装 / 轮换 admin token（写 [admin].secret）
    python -m xusi uninstall             # 停止并移除管理面服务（不动 agent 数据）
    python -m xusi status                # 全部 agent 一览
    python -m xusi doctor                # 环境自检
    python -m xusi create|delete         # agent 增删（进程内直调 agentops，免 HTTP）
    python -m xusi start|stop|pause|resume|restart
    python -m xusi mail|mailbox          # 投信/收信（与 agent 的唯一写通道）
    python -m xusi observe-token         # 签发观察台 token（CLI-only 机器用）

CRUD 直调 agentops 是为了「远端零管理」：CLI-only 机器没有 serve 进程，
CLI 与 serve 同一条实现（跨进程并发由 registry.file_lock 互斥）。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import ROOT, get_config

DEPS = ["fastapi>=0.110", "uvicorn[standard]>=0.27", "httpx[socks]>=0.27"]


def _ensure_venv() -> Path:
    """管理面自己的 .venv（与 xuseek 源码的 .venv 互不依赖）。"""
    venv = ROOT / ".venv"
    py = venv / "bin" / "python"
    if not py.exists():
        print(f"==> 创建虚拟环境 {venv}")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(venv / "bin" / "pip"), "install", "--quiet",
                        "--disable-pip-version-check", *DEPS], check=True)
    # 依赖只装在 venv 里：若本进程不在 venv（系统 python 直接 `python -m xusi install`），
    # 重建 venv 后重进 venv 再执行——后续 cmd_install 会 import httpx 等第三方库。
    # 注意 venv 的 python 是指向系统 python 的符号链接，不能用 resolve() 比对。
    if Path(sys.prefix) != venv.resolve():
        os.execv(str(py), [str(py), "-m", "xusi", *sys.argv[1:]])
    return py


# ── install ──────────────────────────────────────────────────────────

UNIT_TEMPLATE = """\
# 由 `python -m xusi install` 生成
[Unit]
Description=墟司 xusi —— xuseek 智能体管理面
After=network-online.target

[Service]
WorkingDirectory={root}
ExecStart={py} -m xusi serve
Restart=always
RestartSec=3
TimeoutStopSec=15

[Install]
WantedBy=default.target
"""


def _install_xusi_toml() -> None:
    """若 etc/xusi.toml 不存在，从 example 拷一份落盘——这是新机器安装的最小引导。

    Phase 1.2 起 etc/xusi.toml 已 gitignored；空 install 仍是健全的（load_config 全
    用默认值），但常见情形（改 port / 加源路径）需要从模板起手，所以 install 给一份。
    """
    from . import config as _cfg_mod
    cfg = _cfg_mod.get_config()
    toml = cfg.root / "etc" / "xusi.toml"
    if toml.exists():
        return
    example = cfg.root / "etc" / "xusi.toml.example"
    if example.exists():
        import shutil as _sh
        _sh.copy(example, toml)
        print(f"==> 已从模板创建 etc/xusi.toml（按需改 port / 源路径等）")


def cmd_install(args) -> int:
    py = _ensure_venv()
    # xuseek-v2 源码唯一事实源 = versions/ 里的 zip 包：新建 agent 一律从版本仓库
    # 解压成实例私有副本。
    from . import versions as _versions
    cfg = get_config()
    _install_xusi_toml()
    vs = _versions.list_versions()
    if vs:
        print(f"==> 版本仓库就位：{cfg.versions_dir}（{len(vs)} 个版本包："
              + "、".join(v['version'] for v in vs) + "）——新建 agent 将取最新版作实例私有副本")
    else:
        print(f"==> 版本仓库为空：{cfg.versions_dir}——请投放 xuseek-v2-<版本号>.zip"
              f"（见 docs/versions.md），否则无法创建 agent")
    # 密钥池起手：etc/brains.toml 不存在时从模板复制（空 key，600）——clone 后的第一步引导
    cfg = get_config()
    if not cfg.brains_file.exists():
        example = cfg.etc_dir / "brains.toml.example"
        if example.exists():
            import shutil as _sh
            _sh.copy(example, cfg.brains_file)
            cfg.brains_file.chmod(0o600)
            print("==> 已从模板创建 etc/brains.toml（空 key）——请填入至少一家 api_key 后再建 agent")
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "xusi.service"
    unit.write_text(UNIT_TEMPLATE.format(root=ROOT, py=py), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "xusi.service"], check=True)

    # 首次安装：生成 [admin].secret（管理面 admin token）。已存在则不动
    # （重跑 install 不轮换）。
    if not cfg.admin_secret:
        secret = secrets.token_urlsafe(32)
        toml_path = cfg.root / "etc" / "xusi.toml"
        from . import authtok
        authtok.write_secret(toml_path, secret)
        cfg.admin_secret = secret
        print("\n════════════════════════════════════════════════════")
        print(f"  管理面 admin token（仅显示一次，请保存）：\n")
        print(f"    {secret}\n")
        print(f"  WebUI:  http://127.0.0.1:{cfg.port}/")
        print("════════════════════════════════════════════════════")
    subprocess.run(["systemctl", "--user", "status", "xusi.service",
                    "--no-pager", "-l"], check=False)
    return 0


def cmd_uninstall(_args) -> int:
    subprocess.run(["systemctl", "--user", "disable", "--now", "xusi.service"],
                   check=False)
    unit = Path.home() / ".config" / "systemd" / "user" / "xusi.service"
    if unit.exists():
        unit.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print("已停止并移除 xusi.service（agent 的实例目录与注册表保留，agent 单元不动）")
    return 0


# ── serve ────────────────────────────────────────────────────────────

def cmd_serve(args) -> int:
    import uvicorn
    cfg = get_config()
    host = args.host or cfg.host
    port = int(args.port or cfg.port)
    print(f"墟司 v{__version__} 管理面 → http://{host}:{port}  （WebUI: / ）")
    uvicorn.run("xusi.api:app", host=host, port=port, log_level=args.log_level)


# ── init（写 / 轮换 admin token）───────────────────────────────────────

def cmd_init(args) -> int:
    """首次安装 / 轮换 admin token（写 etc/xusi.toml 的 [admin].secret）。"""
    cfg = get_config()
    toml_path = cfg.root / "etc" / "xusi.toml"
    if cfg.admin_secret and not getattr(args, "rotate", False):
        print(f"[admin].secret 已存在：{cfg.admin_secret[:8]}..."
              "（如要轮换，加 --rotate）")
        return 0
    secret = getattr(args, "secret", None) or secrets.token_urlsafe(32)
    from . import authtok
    if not authtok.write_secret(toml_path, secret):
        print(f"error: 无法写 {toml_path}", file=sys.stderr)
        return 1
    print(f"[admin].secret 已写入 {toml_path}")
    print(f"admin token: {secret}")
    return 0


# ── status / doctor ──────────────────────────────────────────────────

def cmd_status(args) -> int:
    from . import agentops
    rows = agentops.list_status()
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(注册表中还没有 agent —— 在 WebUI 或 POST /api/agents 创建)")
        return 0
    for r in rows:
        proc = r.get("process", {})
        rt = "容器" if r.get("runtime") == "docker" else "系统"
        print(f"{r['id']:28} 端口{r['port']:5} {rt}:{proc.get('active', '?'):9} "
              f"期望:{r['desired_state']:8} {r['name']}")
    return 0


def cmd_doctor(args) -> int:
    from . import brains, ports, systemdctl, versions
    cfg = get_config()
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'OK' if good else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
        if not good:
            ok = False

    print(f"墟司 doctor（v{__version__}，root={ROOT}）")
    check("systemd 用户会话", subprocess.run(
        ["systemctl", "--user", "is-system-running"], capture_output=True).returncode in (0, 1))
    # xuseek-v2 源码唯一事实源 = versions/ 里的 zip（新建 agent 取最新版作实例
    # 私有副本）；仓库为空则无法创建 agent，算 FAIL。
    vs = versions.list_versions()
    check("版本仓库非空（xuseek-v2 源码事实源）", bool(vs),
          "" if vs else f"{cfg.versions_dir} 为空——请投放 xuseek-v2-<版本号>.zip（docs/versions.md）")
    if vs:
        print(f"  [INFO] 版本仓库 {cfg.versions_dir}：{len(vs)} 个版本包"
              f"（{'、'.join(v['version'] for v in vs)}）——新建 agent 取最新版")
    pool = brains.pool_summary()
    check("密钥池至少一家可用", any(b["has_key"] for b in pool),
          f"{len(pool)} 家：{', '.join(b['name'] + ('(有key)' if b['has_key'] else '(缺key)') for b in pool)}")
    # port_free(cfg.port) 恒 False（管理面端口在分配层被保留），旧写法因此
    # 恒等 manager_running()——install 前跑 doctor 必误报。改用 ports 的
    # 主机级三态检验：空闲、或在跑本服务都算过，其余按状态给可行动的提示。
    mgr_running = systemdctl.manager_running()
    state = ports.port_host_state(cfg.port)
    if state == "free":
        check("管理面端口空闲或已由本服务监听", True)
    elif state == "listening":
        # 内核有监听但 systemd 看不到单元：多半是本服务没带 XDG_RUNTIME_DIR
        # 启动（unit_state 全 not-found），也可能是别的进程抢了端口
        check("管理面端口空闲或已由本服务监听", mgr_running,
              "" if mgr_running else
              f"端口 {cfg.port} 已被监听但本服务单元不可见——xusi 若在运行，"
              f"确认以 XDG_RUNTIME_DIR 启动（否则 systemd --user 看不到单元）；"
              f"否则为其它进程占用")
    else:
        check("管理面端口空闲或已由本服务监听", False,
              f"端口 {cfg.port} 无监听但 bind 被拒——可能刚停止（TIME_WAIT 窗口），稍候重试")
    check("agent 端口段有富余", len(ports.available_ports(5)) >= 5,
          f"可用示例 {ports.available_ports(5)}")
    if getattr(args, "mode", "serve") == "cli":
        print("  [INFO] CLI 模式：跳过管理面 token 检查（CLI 直调不鉴权 HTTP）")
    else:
        check("管理面 token 已初始化", bool(get_config().admin_secret),
              "" if get_config().admin_secret else
              "[admin].secret 缺失——`xusi init` 生成，或在 etc/xusi.toml 手填")
    droots = get_config().default_roots
    if droots:
        print(f"  [INFO] 缺省根智能体：{len(droots)} 个（创建对话框预填："
              + "、".join(r["address"] for r in droots) + "）")
    units = subprocess.run(["systemctl", "--user", "list-units", "xusi-a-*",
                            "--no-legend", "--plain"], capture_output=True, text=True)
    n_units = len([l for l in units.stdout.splitlines() if l.strip()])
    print(f"  [INFO] 运行中的 agent 单元：{n_units} 个")
    # 双运行时：docker 是可选路径——缺省运行时是 docker 时才算 FAIL；
    # 否则只报 INFO（含 docker agent 计数与可行动提示），systemd agent 不受影响
    from . import dockerctl, registry
    d_ok, d_hint = dockerctl.docker_available()
    n_doc = len([a for a in registry.list_agents() if a.get("runtime") == "docker"])
    if cfg.default_runtime == "docker":
        check("Docker 运行时可用（daemon + compose 插件）", d_ok,
              "" if d_ok else d_hint or "docker 不可用")
    elif d_ok:
        print(f"  [INFO] Docker 运行时可用" + (f"（容器 agent {n_doc} 个）" if n_doc else ""))
    else:
        print(f"  [INFO] Docker 运行时不可用：{d_hint or '未知原因'}"
              + (f"（已有容器 agent {n_doc} 个——修复后它们才能拉起）" if n_doc else
                 "（不影响 systemd 运行时；要建容器 agent 先修复）"))
    print("结论：" + ("全部通过 ✓" if ok else "存在未通过项 ✗"))
    return 0 if ok else 1


# ── backup / backups / restore ────────────────────────────────────────

def cmd_backup(args) -> int:
    """备份一个或全部 agent（运行中 SIGSTOP 冻结窗快照）；产物落到 etc/backups/。"""
    from . import backup
    if args.all:
        from . import registry
        from . import agentops as _aops
        n_ok = n_skip = 0
        for a in registry.list_agents():
            try:
                info = backup.snapshot(a["id"], reason=args.reason)
                print(f"  ✓ {a['id']:28} {info['size_bytes']:>8} B  {info['key']}")
                n_ok += 1
            except backup.BackupError as e:
                print(f"  · {a['id']:28} 跳过：{e}", file=sys.stderr)
                n_skip += 1
        print(f"==> 完成 {n_ok} 个，{n_skip} 个跳过")
        return 0
    if not args.agent_id:
        print("error: 需要 agent-id 或 --all", file=sys.stderr)
        return 2
    info = backup.snapshot(args.agent_id, reason=args.reason)
    print(f"  agent  : {info['meta']['agent_id']}")
    print(f"  key    : {info['key']}")
    print(f"  size   : {info['size_bytes']} B")
    print(f"  reason : {info['meta']['snapshot_reason']}")
    print(f"  at     : {info['meta']['snapshot_at']}")
    return 0


def cmd_backups(args) -> int:
    """列出某 agent（缺省全部）的备份包。"""
    from . import backup
    rows = backup.list_backups(agent_id=args.agent_id)
    if not rows:
        print("(没有备份)" if args.agent_id else "(没有备份——先 `xusi backup <id>`)")
        return 0
    for r in rows:
        print(f"  {r['mtime']}  {r['size_bytes']:>10} B  {r['key']}")
    return 0


def cmd_restore(args) -> int:
    """从备份包恢复 agent 到新 home；可改名 / 换端口。"""
    from . import backup
    bp = Path(args.from_path).expanduser().resolve()
    if not bp.is_file():
        print(f"error: 备份文件不存在：{bp}", file=sys.stderr)
        return 2
    out = backup.restore(
        bp,
        new_id=args.new_id,
        port=args.port,
        overwrite=args.overwrite,
    )
    print(f"  restored id     : {out['id']}")
    print(f"  port            : {out['port']}")
    print(f"  home            : {out['home']}")
    print(f"  restored_from   : {out['restored_from']}")
    return 0


# ── agent CRUD（进程内直调 agentops——与 serve 同一条实现；跨进程并发由
#    registry.file_lock 互斥；CLI-only 机器没有 serve 也能全功能管理）────────


def _cli_agent_error(e: Exception) -> int:
    print(f"error: {e}", file=sys.stderr)
    return 2


def _arg_text(val: str) -> str:
    """@file → 读文件内容（mission/extra_config 这类长文本）；否则原样返回。"""
    if val.startswith("@"):
        return Path(val[1:]).expanduser().read_text(encoding="utf-8")
    return val


def _parse_roots(items: list[str]) -> list[dict]:
    roots = []
    for it in items:
        parts = it.split()
        if len(parts) != 2:
            raise ValueError(f"--roots 需成对 'ADDRESS TOKEN'：{it!r}")
        roots.append({"address": parts[0], "token": parts[1]})
    return roots


def cmd_create(args) -> int:
    from . import agentops
    if getattr(args, "spec", None):
        body = json.loads(Path(args.spec).expanduser().read_text(encoding="utf-8"))
    else:
        body = {
            "name": args.name,
            "mission": _arg_text(args.mission),
            "brain_list": [b for b in args.brains.split(",") if b.strip()],
            "expose": bool(args.expose),
            "port": args.port,
            "budgets": json.loads(args.budgets) if args.budgets else None,
            "note": args.note or "",
            "source_version": args.source_version or "",
            "roots": _parse_roots(args.roots) if args.roots else None,
            "extra_config": _arg_text(args.extra_config) if args.extra_config else "",
            "runtime": args.runtime or "",
        }
    try:
        r = agentops.create_agent(**body)
    except (agentops.AgentError, ValueError, TypeError, OSError) as e:
        return _cli_agent_error(e)
    print(f"  created       : {r['id']}")
    print(f"  name          : {r['name']}")
    print(f"  port          : {r['port']}")
    print(f"  runtime       : {r['runtime']}")
    print(f"  source        : {r.get('source_version', '')}")
    return 0


def cmd_agent_op(args) -> int:
    from . import agentops
    fn = {"start": agentops.start, "stop": agentops.stop, "pause": agentops.pause,
          "resume": agentops.resume, "restart": agentops.restart}[args.op]
    try:
        r = fn(args.agent_id)
    except agentops.AgentError as e:
        return _cli_agent_error(e)
    print(f"  {args.op} ok: {r['id']} 期望态={r.get('desired_state', '')}")
    return 0


def cmd_delete(args) -> int:
    from . import agentops
    try:
        r = agentops.delete(args.agent_id)
    except agentops.AgentError as e:
        return _cli_agent_error(e)
    print(f"  deleted: {r.get('id', args.agent_id)}（home 进 .trash）")
    return 0


def cmd_mail(args) -> int:
    from . import agentops
    try:
        r = agentops.mail(args.agent_id, args.text)
    except agentops.AgentError as e:
        return _cli_agent_error(e)
    print(f"  已投信 {r['id']}（{r['at']}）")
    return 0


def cmd_mailbox(args) -> int:
    from . import agentops
    try:
        r = agentops.mailbox(args.agent_id, limit=args.limit, box=args.box)
    except agentops.AgentError as e:
        return _cli_agent_error(e)
    msgs = r["messages"]
    if not msgs:
        print("(邮箱为空)")
        return 0
    for m in msgs:
        print(f"[{m.get('at', '?')}] {m.get('sender')}: {m.get('text')}")
    return 0


def cmd_observe_token(args) -> int:
    from . import agentops
    try:
        tok = agentops.observe_token(args.agent_id, force_new=bool(args.new))
    except agentops.AgentError as e:
        return _cli_agent_error(e)
    print(f"observe token: {tok}")
    print("（观察台 URL 带 ?mtoken=<此 token> 打开即认证）")
    return 0


# ── remote（控制端 fan-out：远端 xusi 批量管理，纯 ssh/scp）─────────────


def _remote_one(h: dict, fn) -> int:
    from . import remote
    try:
        cp = fn(h)
    except remote.RemoteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if cp.stdout:
        print(cp.stdout, end="" if cp.stdout.endswith("\n") else "\n")
    if cp.returncode != 0:
        if cp.stderr:
            print(cp.stderr, file=sys.stderr, end="")
        return 2
    return 0


def _remote_status(hosts: list[dict], json_out: bool) -> int:
    from . import remote
    results = remote.fan_out(remote.remote_status, hosts)
    rc = 0
    if json_out:
        merged: list[dict] = []
        for res in results:
            if "error" in res:
                merged.append({"host": res["host"], "error": res["error"]})
                rc = 2
            else:
                merged += [{"host": res["host"], **row} for row in res.get("rows", [])]
        print(json.dumps(merged, ensure_ascii=False, indent=2))
        return rc
    w = max((len(h.get("name", "")) for h in hosts), default=10) + 2
    for res in results:
        if "error" in res:
            print(f"  {res['host']:<{w}} ERROR {res['error']}")
            rc = 2
            continue
        rows = res.get("rows", [])
        if not rows:
            print(f"  {res['host']:<{w}} (没有 agent)")
            continue
        for r in rows:
            proc = r.get("process", {})
            rt = "容器" if r.get("runtime") == "docker" else "系统"
            print(f"  {res['host']:<{w}} {r['id']:22} 端口{r['port']:5} "
                  f"{rt}:{proc.get('active', '?'):9} 期望:{r['desired_state']:8} {r['name']}")
    return rc


def _remote_doctor(hosts: list[dict]) -> int:
    from . import remote

    def one(h: dict) -> dict:
        cp = remote.xusi_cmd(h, ["doctor", "--mode", "cli"], timeout=120)
        return {"host": h.get("name", ""), "rc": cp.returncode,
                "out": (cp.stdout or "") + (cp.stderr or "")}

    rc = 0
    for res in remote.fan_out(one, hosts):
        print(f"== {res['host']} ==")
        for line in res["out"].splitlines():
            print(f"  {line}")
        if res["rc"] != 0:
            rc = 2
    return rc


def _remote_install(h: dict) -> int:
    from . import remote
    try:
        for line in remote.install_host(h):
            print(f"  {line}")
    except remote.RemoteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"  完成：{h['name']} 已接入——零服务、零端口，~/xusi 自洽目录")
    return 0


def _remote_upgrade(h: dict) -> int:
    from . import remote
    try:
        remote.upgrade_host(h)
    except remote.RemoteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"  已重推代码：{h['name']}")
    return 0


def _remote_backup(h: dict, args) -> int:
    from . import remote
    agent_id = (list(args.rest) or [""])[0]
    if not agent_id:
        print("error: 需要 agent id：xusi remote backup --on H <agent-id>",
              file=sys.stderr)
        return 2
    out_dir = Path(args.out).expanduser() if getattr(args, "out", None) \
        else get_config().etc_dir / "remote-backups" / h.get("name", "host")
    try:
        local = remote.backup_host(h, agent_id, out_dir)
    except remote.RemoteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"  备份已拉回：{local}")
    return 0


def _remote_restore(h: dict, args) -> int:
    from . import remote
    try:
        cp = remote.restore_host(h, Path(args.from_path), list(args.rest))
    except remote.RemoteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if cp.stdout:
        print(cp.stdout, end="" if cp.stdout.endswith("\n") else "\n")
    if cp.returncode != 0:
        if cp.stderr:
            print(cp.stderr, file=sys.stderr, end="")
        return 2
    return 0


class _UsageError(Exception):
    pass


_REMOTE_HELP = """\
usage: xusi remote <cmd> [--on NAME] [参数...]

远端 xusi 批量管理（控制端 fan-out，纯 ssh/scp；清单 etc/hosts.toml）：
  status    [--on NAME] [--json]        全队 agent 一览
  doctor    [--on NAME]                 全队环境自检（CLI 模式）
  create    --on NAME <本地 create 参数>  在指定机器创建 agent（@file/--spec 自动上传）
  start|stop|pause|resume|restart|delete --on NAME <agent-id>
  mail      --on NAME <agent-id> <text>  投信（与 agent 的唯一写通道）
  mailbox   --on NAME <agent-id> [--limit N] [--box outbox|inbox]
  observe-token --on NAME <agent-id> [--new]
  install   --on NAME                   新机接入：python3.12 + linger + 推代码 + 播种 brains
  upgrade   --on NAME                   重推代码 tar（管理面升级 / 内核版本发布）
  backup    --on NAME <agent-id> [--out DIR]  远端备份 → 拉回控制端
  restore   --on NAME --from FILE [恢复参数]   备份包推上远端并恢复（跨主机迁移）\
"""


def _remote_main(argv: list[str]) -> int:
    """remote 子命令手工分发——argparse 的 REMAINDER 与嵌套 subparsers 是死结
    （透传的 --name 等选项会被顶回父解析器报 unrecognized），所以这里不用
    argparse：固定形状 `xusi remote <cmd> [--on NAME] [flag] [rest...]`，
    flag 从 rest 里手工提取，其余原样透传远端。"""
    from types import SimpleNamespace
    if not argv or argv[0] in ("-h", "--help"):
        print(_REMOTE_HELP, end="")
        return 0 if argv else 2
    rcmd = argv[0]
    rest = list(argv[1:])

    def take(flag: str, default: str | None = None, required: bool = False) -> str | None:
        nonlocal rest
        if flag not in rest:
            if required:
                print(f"error: remote {rcmd} 需要 {flag}\n", file=sys.stderr)
                print(_REMOTE_HELP, file=sys.stderr, end="")
                raise _UsageError
            return default
        i = rest.index(flag)
        rest.pop(i)
        if i >= len(rest):
            print(f"error: {flag} 缺值\n", file=sys.stderr)
            print(_REMOTE_HELP, file=sys.stderr, end="")
            raise _UsageError
        return rest.pop(i)

    try:
        need_on = rcmd not in ("status", "doctor")
        host = take("--on", required=need_on)
        json_out = False
        if rcmd == "status" and "--json" in rest:
            rest.remove("--json")
            json_out = True
        out = take("--out") if rcmd == "backup" else None
        from_path = take("--from", required=True) if rcmd == "restore" else None
    except _UsageError:
        return 2

    return cmd_remote(SimpleNamespace(rfn=rcmd, host=host, rest=rest,
                                      json=json_out, out=out, from_path=from_path))


def cmd_remote(args) -> int:
    from . import remote
    rfn = args.rfn
    try:
        if rfn in ("status", "doctor"):
            hosts = [remote.find_host(args.host)] if getattr(args, "host", None) \
                else remote.load_hosts()
        else:
            hosts = [remote.find_host(args.host)]
    except remote.RemoteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if rfn == "status":
        return _remote_status(hosts, json_out=bool(getattr(args, "json", False)))
    if rfn == "doctor":
        return _remote_doctor(hosts)
    if rfn == "install":
        return _remote_install(hosts[0])
    if rfn == "upgrade":
        return _remote_upgrade(hosts[0])
    if rfn == "backup":
        return _remote_backup(hosts[0], args)
    if rfn == "restore":
        return _remote_restore(hosts[0], args)
    if rfn == "create":
        return _remote_one(hosts[0], lambda h: remote.remote_create(h, list(args.rest),
                                                                    timeout=900))
    return _remote_one(hosts[0], lambda h: remote.remote_agent_op(h, rfn, list(args.rest)))


def main() -> int:
    # remote 走手工分发（REMAINDER 透传与 argparse 不兼容）；先于 parse_args 拦截
    if len(sys.argv) > 1 and sys.argv[1] == "remote":
        return _remote_main(sys.argv[2:])
    p = argparse.ArgumentParser(prog="xusi", description="墟司 —— xuseek 智能体管理面")
    p.add_argument("--version", action="version", version=f"xusi {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="前台运行管理面（常驻请用 install）")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--log-level", default="info")
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("install", help="安装 systemd 用户服务并启动").set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="停止并移除管理面服务").set_defaults(fn=cmd_uninstall)

    st_ = sub.add_parser("status", help="agent 一览")
    st_.add_argument("--json", action="store_true", help="JSON 输出（remote 汇总用）")
    st_.set_defaults(fn=cmd_status)

    d_ = sub.add_parser("doctor", help="环境自检")
    d_.add_argument("--mode", choices=("serve", "cli"), default="serve",
                    help="cli：跳过管理面 token 检查（远端零管理机器）")
    d_.set_defaults(fn=cmd_doctor)

    c_ = sub.add_parser("create", help="创建并启动 agent（进程内直调 agentops）")
    c_.add_argument("--spec", default=None,
                    help="整体 JSON 文件（与 POST /api/agents 的 body 同构）")
    c_.add_argument("--name", default="")
    c_.add_argument("--mission", default="", help="mission 文本；@file 读文件")
    c_.add_argument("--brains", default="", help="逗号分隔，首个为默认大脑")
    c_.add_argument("--runtime", default="", help="systemd / docker（缺省取配置默认）")
    c_.add_argument("--expose", action="store_true")
    c_.add_argument("--port", type=int, default=None)
    c_.add_argument("--budgets", default=None, help="JSON 字符串")
    c_.add_argument("--note", default=None)
    c_.add_argument("--source-version", default=None)
    c_.add_argument("--roots", action="append", default=None,
                    help="'ADDRESS TOKEN' 成对，可重复")
    c_.add_argument("--extra-config", default=None, help="自由 TOML；@file 读文件")
    c_.set_defaults(fn=cmd_create)

    for op in ("start", "stop", "pause", "resume", "restart"):
        op_ = sub.add_parser(op, help=f"{op} agent")
        op_.add_argument("agent_id")
        op_.set_defaults(fn=cmd_agent_op, op=op)

    dl_ = sub.add_parser("delete", help="删除 agent（须先停止；home 进 .trash）")
    dl_.add_argument("agent_id")
    dl_.set_defaults(fn=cmd_delete)

    ml_ = sub.add_parser("mail", help="给 agent 投信（与 agent 的唯一写通道）")
    ml_.add_argument("agent_id")
    ml_.add_argument("text")
    ml_.set_defaults(fn=cmd_mail)

    mb_ = sub.add_parser("mailbox", help="读 agent 邮箱")
    mb_.add_argument("agent_id")
    mb_.add_argument("--limit", type=int, default=50)
    mb_.add_argument("--box", choices=("outbox", "inbox"), default="outbox",
                     help="outbox=来信 / inbox=投信历史")
    mb_.set_defaults(fn=cmd_mailbox)

    ot_ = sub.add_parser("observe-token", help="签发/轮换观察台 token（CLI-only 机器）")
    ot_.add_argument("agent_id")
    ot_.add_argument("--new", action="store_true", help="强制轮换新 token")
    ot_.set_defaults(fn=cmd_observe_token)

    # remote 只在此登记 help 条目；实际解析在 main() 入口拦截 → _remote_main
    # 手工分发（argparse 的 REMAINDER 与嵌套 subparsers 是死结，见 _remote_main）
    sub.add_parser("remote", help="多机 fan-out（控制端：远端 xusi 批量管理，纯 ssh/scp）") \
       .set_defaults(fn=lambda _args: 2)

    init_ = sub.add_parser("init", help="生成 / 轮换 [admin].secret（管理面 admin token）")
    init_.add_argument("--secret", dest="secret", default=None,
                       help="指定 secret；缺省 = 本机生成新的")
    init_.add_argument("--rotate", action="store_true",
                       help="已有 secret 时强制轮换（重新生成）")
    init_.set_defaults(fn=cmd_init)

    bp_ = sub.add_parser("backup", help="备份 agent 的 data + workspace（冻结窗快照）")
    bp_.add_argument("agent_id", nargs="?", default="")
    bp_.add_argument("--all", action="store_true", help="备份所有 agent（运行中走 SIGSTOP 冻结窗快照）")
    bp_.add_argument("--reason", default="manual", help="备份原因（manual/pre-modify/...）")
    bp_.set_defaults(fn=cmd_backup)

    bl_ = sub.add_parser("backups", help="列出备份包")
    bl_.add_argument("agent_id", nargs="?", default="", help="可选 agent-id 过滤")
    bl_.set_defaults(fn=cmd_backups)

    rs_ = sub.add_parser("restore", help="从备份包恢复 agent（可跨主机）")
    rs_.add_argument("--from", dest="from_path", required=True, help="备份 tar.gz 路径")
    rs_.add_argument("--new-id", default=None, help="恢复后用新 id（避免冲突）")
    rs_.add_argument("--port", type=int, default=None, help="恢复后端口（默认自动分配；listen host 由注册表 expose 推导）")
    rs_.add_argument("--overwrite", action="store_true", help="覆盖同名已存在 agent")
    rs_.set_defaults(fn=cmd_restore)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
