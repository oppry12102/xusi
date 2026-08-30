"""墟司 CLI：

    python -m xusi serve                 # 前台跑管理面（调试用；常驻走 install）
    python -m xusi install               # 建 venv → 装 systemd 用户服务 → 启动
    python -m xusi init                  # 首次安装 / 轮换 admin token（写 [admin].secret）
    python -m xusi uninstall             # 停止并移除管理面服务（不动 agent 数据）
    python -m xusi status                # 全部 agent 一览（含互联标注）
    python -m xusi doctor                # 环境自检
    python -m xusi post-upgrade-note     # 升级后向 agent 投递迁移说明信（管理邮箱）
"""
from __future__ import annotations

import argparse
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

def cmd_status(_args) -> int:
    from . import agentops
    rows = agentops.list_status()
    if not rows:
        print("(注册表中还没有 agent —— 在 WebUI 或 POST /api/agents 创建)")
        return 0
    for r in rows:
        proc = r.get("process", {})
        ic = r.get("interconnect") or {}
        conn = f"互联:{ic.get('port', '-')}" if ic.get("token") else "互联:未发布"
        print(f"{r['id']:28} 端口{r['port']:5} 单元:{proc.get('active', '?'):9} "
              f"期望:{r['desired_state']:8} {conn:14} {r['name']}")
    return 0


def cmd_doctor(_args) -> int:
    from . import brains, mailroom, ports, systemdctl, versions
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
    check("管理面 token 已初始化", bool(get_config().admin_secret),
          "" if get_config().admin_secret else
          "[admin].secret 缺失——`xusi init` 生成，或在 etc/xusi.toml 手填")
    # 互联信箱（mailroom）：outbox 扫描状态 + 互联标注统计
    from . import registry
    snap = mailroom.state_snapshot()
    n_pub = sum(1 for a in registry.list_agents()
                if isinstance(a.get("interconnect"), dict) and a.get("interconnect", {}).get("token"))
    print(f"  [INFO] 互联信箱：{n_pub}/{len(registry.list_agents())} 个 agent 已发布互联；"
          f"outbox 扫描偏移 {len(snap)} 份")
    units = subprocess.run(["systemctl", "--user", "list-units", "xusi-a-*",
                            "--no-legend", "--plain"], capture_output=True, text=True)
    n_units = len([l for l in units.stdout.splitlines() if l.strip()])
    print(f"  [INFO] 运行中的 agent 单元：{n_units} 个")
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


# ── post-upgrade-note（升级迁移信：经管理邮箱投递，管理员手动触发）─────

_UPGRADE_NOTE_TMPL = """【墟司管理面升级通知】
墟司已重构：你我之间现在只有一条管理邮箱通道（其余通信方式全部取消）。
你的对外呈现（观察台、自建服务等）是你的自家业务——怎么让人找到你，由你自己
决定，并经 send_mail 告知管理员。
1. 与其它 agent 互联（可选）：自生成互联 token 与互联端口，回信发布登记：
   {{"xusi":"publish","port":<互联端口>,"token":"<互联token>","host":"<其它机器可达地址，同机可省略>"}}
   重复发布 = 更新，随时可换端口换 token。
2. 需要其它 agent 的互联地址与 token：回信 {{"xusi":"request_directory"}}，管理面自动回执目录。
3. mission / brains / budgets 今后由你自己改：管理员不再改写你的 config.toml
   （仅在创建时渲染一次）。需要新大脑的 api_key 时向管理员索取，用 run_shell 编辑
   config.toml，每口呼吸热重载（改动前建议先自行备份）。"""


def cmd_post_upgrade_note(args) -> int:
    """升级后向 agent 投迁移说明信。管理员手动触发（不做升级自动投——
    管理员先验收再通知）。"""
    from . import agentops, registry
    agents = registry.list_agents()
    if args.agent_id:
        agents = [a for a in agents if a["id"] == args.agent_id]
        if not agents:
            print(f"error: agent 不存在：{args.agent_id}", file=sys.stderr)
            return 2
    if not agents:
        print("(注册表中没有 agent)")
        return 0
    for a in agents:
        try:
            r = agentops.mail(a["id"], _UPGRADE_NOTE_TMPL)
            print(f"  ✓ 已投递 {a['id']:28}（{r['id']}）")
        except agentops.AgentError as e:
            print(f"  · {a['id']:28} 跳过：{e}", file=sys.stderr)
    agentops.audit("migration.note", agents=[a["id"] for a in agents])
    return 0


def main() -> int:
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
    sub.add_parser("status", help="agent 一览").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="环境自检").set_defaults(fn=cmd_doctor)

    init_ = sub.add_parser("init", help="生成 / 轮换 [admin].secret（管理面 admin token）")
    init_.add_argument("--secret", dest="secret", default=None,
                       help="指定 secret；缺省 = 本机生成新的")
    init_.add_argument("--rotate", action="store_true",
                       help="已有 secret 时强制轮换（重新生成）")
    init_.set_defaults(fn=cmd_init)

    upn_ = sub.add_parser("post-upgrade-note", help="升级后向 agent 投递迁移说明信（管理邮箱）")
    upn_.add_argument("--agent", dest="agent_id", default="",
                      help="只投给指定 agent；缺省 = 全部")
    upn_.set_defaults(fn=cmd_post_upgrade_note)

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
