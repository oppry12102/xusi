"""墟司 CLI：

    python -m xusi serve                 # 前台跑管理面（调试用；常驻走 install）
    python -m xusi install               # 建 venv → 装 systemd 用户服务 → 启动
    python -m xusi uninstall             # 停止并移除管理面服务（不动 agent 数据）
    python -m xusi token new/list/revoke # 管理面 token（admin / user）
    python -m xusi status                # 全部 agent 一览
    python -m xusi doctor                # 环境自检
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import zipfile
from pathlib import Path

from . import __version__, authtok
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


def _ensure_node_id() -> None:
    """安装时若 etc/xusi.toml 没设 [node].id，自动生成 6 字节 url-safe 写回。
    用文本保形（不重写整个文件、保留注释）：缺 [node] 段则追加；缺 id 行则插入。"""
    cfg = get_config()
    if cfg.node_id:
        return
    new_id = secrets.token_urlsafe(6)
    toml = cfg.root / "etc" / "xusi.toml"
    text = toml.read_text(encoding="utf-8") if toml.exists() else ""
    lines = text.splitlines(keepends=True)
    # 找 [node] 段
    node_idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == "[node]":
            node_idx = i
            break
    if node_idx is None:
        # 追加整段
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n[node]\n")
        lines.append(f'id = "{new_id}"\n')
    else:
        # 在 [node] 段内找 `id =` 行；没找到则在段末之后插入
        in_node = False
        insert_at = None
        for i, ln in enumerate(lines):
            s = ln.strip()
            if s == "[node]":
                in_node = True
                continue
            if in_node:
                if s.startswith("[") and s.endswith("]"):   # 进入下一段
                    insert_at = i
                    break
                if s.startswith("id"):
                    # 已经有 id 行（即使空也当作已设）——不动
                    return
        if insert_at is None:
            insert_at = len(lines)
        lines.insert(insert_at, f'id = "{new_id}"\n')
    toml.write_text("".join(lines), encoding="utf-8")
    # cfg 缓存要清掉——get_config 是 module global
    from . import config as _cfg_mod
    _cfg_mod._CONFIG = None
    _cfg_mod.load_config()
    print(f"==> 节点身份：自动生成 id = {new_id}（写入 etc/xusi.toml [node].id；想换名手改）")


def cmd_install(args) -> int:
    py = _ensure_venv()
    # xuseek-v2 源码事实源 = versions/ 里的 zip 包。新约定：缺省从 versions 取最新
    # 版本解压到实例私有副本，共享主源码 source_dir 已废弃；只有 versions 为空时
    # 才回落到 source_dir（缺失时自动从 GitHub 拉取）。
    from . import agentops, versions as _versions
    cfg = get_config()
    _ensure_node_id()
    cfg = get_config()  # reload after id 写入
    vs = _versions.list_versions()
    if vs:
        print(f"==> 版本仓库就位：{cfg.versions_dir}（{len(vs)} 个版本包："
              + "、".join(v['version'] for v in vs) + "）——新建 agent 将取最新版作实例私有副本")
    else:
        src = agentops.ensure_source()
        print(f"==> 版本仓库为空，xuseek-v2 共享主源码就位：{src}（过渡期兼容）")
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

    # 首个 admin token（仅此处打印一次；etc/tokens.json 可随时读回）
    if not authtok.list_tokens():
        rec = authtok.new_token("admin", role="admin")
        print("\n════════════════════════════════════════════════════")
        print(f"  管理面 admin token（仅显示一次，请保存）：\n")
        print(f"    {rec['token']}\n")
        print(f"  WebUI:  http://127.0.0.1:{get_config().port}/")
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


# ── token ────────────────────────────────────────────────────────────

def cmd_token(args) -> int:
    if args.cmd == "new":
        rec = authtok.new_token(args.label, role=args.role,
                                agents=[a.strip() for a in args.agents.split(",")] if args.agents else None)
        print(rec["token"])
        print(f"# label: {rec['label']}  role: {rec['role']}  agents: {rec['agents']}",
              file=sys.stderr)
    elif args.cmd == "list":
        rows = authtok.list_tokens()
        if not rows:
            print("(尚无管理面 token)")
        for t in rows:
            print(f"{t['created_at']}  {t['label']:20}  {t['role']:6}  "
                  f"agents={','.join(t['agents'])}  {t['token']}")
    elif args.cmd == "revoke":
        n = authtok.revoke_token(args.prefix)
        print(f"已撤销 {n} 个管理面 token")
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
        health = "健康" if r.get("health", {}).get("ok") else "无响应"
        daemon = (r.get("agent_status", {}) or {}).get("daemon", {}).get("state", "-")
        print(f"{r['id']:28} 端口{r['port']:5} 单元:{proc.get('active', '?'):9} "
              f"{health:4} daemon:{daemon:15} 期望:{r['desired_state']:8} {r['name']}")
    return 0


def _zip_pack_names(zp: Path) -> list[tuple[str, str]]:
    """zip 里有哪些能力包：走查 xuseek/capabilities/*/manifest.toml，读 [pack] 的
    name/version（名字与目录名不一致的坏包跳过——与内核 discover 同判）。"""
    import tomllib
    out: list[tuple[str, str]] = []
    try:
        with zipfile.ZipFile(zp) as zf:
            names = [n for n in zf.namelist()
                     if re.fullmatch(r"(?:[^/]+/)*xuseek/capabilities/([a-z0-9-]+)/manifest\.toml", n)]
            for n in sorted(names):
                try:
                    raw = tomllib.loads(zf.read(n).decode("utf-8"))
                    p = raw.get("pack") or {}
                    d = n.rsplit("/", 2)[-2]
                    if str(p.get("name", "")) == d:
                        out.append((d, str(p.get("version", ""))))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def cmd_doctor(_args) -> int:
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
    # 源码事实源 = versions/ 里的 zip（缺省建 agent 自动取最新版作实例私有副本）；
    # source_dir 是过渡期兼容字段（仅现存 agent / 显式 "main" 用），不在 = OK
    vs = versions.list_versions()
    src_ok = (cfg.source_dir / "xuseek.sh").exists()
    if vs:
        # 新约定：versions/ 是事实源，source_dir 缺失属正常（已废弃）
        print(f"  [INFO] 共享主源码 source_dir（过渡期字段）{cfg.source_dir}："
              + ("就位" if src_ok else "未就位（已废弃——新建 agent 走 versions/）"))
        check("xuseek 源码 venv 可用", True,
              "venv 在每个 agent 实例的 xuseek-v2/.venv 首次启动时由 xuseek.sh 自建")
    else:
        # 老约定：versions 空时 source_dir 仍是唯一来源
        check("xuseek-v2 源码（自管）", src_ok,
              f"{cfg.source_dir}" + ("" if src_ok else f"（缺失；创建 agent 时自动从 {cfg.source_repo} 拉取）"))
        check("xuseek 源码 venv 可用", (cfg.source_dir / ".venv" / "bin" / "python").exists()
              or not src_ok, "首次 spawn 时由 xuseek.sh 自动构建")
    print(f"  [INFO] 版本仓库 {cfg.versions_dir}：{len(vs)} 个版本包"
          + (f"（{'、'.join(v['version'] for v in vs)}）" if vs else "（空——新建 agent 走共享主源码）"))
    # 各版本 zip 的能力包资产校验（投放校验：manifest 存在才算数；只读 manifest 名，
    # 不解析 pack 内容——契约一只许读 manifest）
    import zipfile
    for v in vs:
        packs = _zip_pack_names(cfg.versions_dir / v["file"])
        print(f"  [INFO] {v['version']} 能力包资产："
              + ("、".join(f"{n}@{ver}" for n, ver in packs) if packs else "（无）"))
    # HF 镜像（能力包嵌入模型首次下载走它）：软检查——离线机可忽略，不算 FAIL
    try:
        import httpx
        httpx.get("https://hf-mirror.com", timeout=5, follow_redirects=True)
        print("  [INFO] HF 镜像 hf-mirror.com 可达（能力包嵌入模型下载走它）")
    except Exception as e:
        print(f"  [WARN] HF 镜像 hf-mirror.com 不可达（{type(e).__name__}）——"
              "开了带嵌入模型的能力包（如 amem）时首次下载会失败；离线部署可忽略")
    pool = brains.pool_summary()
    check("密钥池至少一家可用", any(b["has_key"] for b in pool),
          f"{len(pool)} 家：{', '.join(b['name'] + ('(有key)' if b['has_key'] else '(缺key)') for b in pool)}")
    check("管理面端口空闲或已由本服务监听",
          not ports.port_free(cfg.port) and systemdctl.manager_running()
          or ports.port_free(cfg.port))
    check("agent 端口段有富余", len(ports.available_ports(5)) >= 5,
          f"可用示例 {ports.available_ports(5)}")
    check("管理面 token 已初始化", bool(authtok.list_tokens()))
    # agent 自建服务清单：坏条目不致命（逐级降级），只报数
    from . import registry, services
    n_svc = n_err = 0
    for a in registry.list_agents():
        svcs, errs = services.merge_services(a)
        n_svc += len(svcs); n_err += len(errs)
        for s in svcs:
            if cfg.port_lo <= s["port"] <= cfg.port_hi:
                print(f"  [WARN] 服务 {a['id']}/{s['name']} 端口 {s['port']} 在 agent 分配池内"
                      f"（建议 8700-8799），可能与新 agent 冲突")
        for e in errs:
            print(f"  [WARN] {a['id']} services.json：{e}")
    print(f"  [INFO] agent 自建服务：{n_svc} 个（清单错误 {n_err} 处）")
    units = subprocess.run(["systemctl", "--user", "list-units", "xusi-a-*",
                            "--no-legend", "--plain"], capture_output=True, text=True)
    n_units = len([l for l in units.stdout.splitlines() if l.strip()])
    print(f"  [INFO] 运行中的 agent 单元：{n_units} 个")
    print("结论：" + ("全部通过 ✓" if ok else "存在未通过项 ✗"))
    return 0 if ok else 1


# ── backup / backups / restore ────────────────────────────────────────

def cmd_backup(args) -> int:
    """备份一个或全部 sleeping 的 agent；产物落到 etc/backups/。"""
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
        host=args.host,
        overwrite=args.overwrite,
    )
    print(f"  restored id     : {out['id']}")
    print(f"  port            : {out['port']}")
    print(f"  home            : {out['home']}")
    print(f"  restored_from   : {out['restored_from']}")
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

    t = sub.add_parser("token", help="管理面 token")
    ts = t.add_subparsers(dest="cmd", required=True)
    tn = ts.add_parser("new", help="签发（仅显示一次）")
    tn.add_argument("label", nargs="?", default="")
    tn.add_argument("--role", choices=["admin", "user"], default="user")
    tn.add_argument("--agents", default=None, help="user 范围：逗号分隔 agent-id（admin 无需）")
    tn.set_defaults(fn=cmd_token)
    tl = ts.add_parser("list", help="列出")
    tl.set_defaults(fn=cmd_token)
    tr = ts.add_parser("revoke", help="按前缀撤销")
    tr.add_argument("prefix")
    tr.set_defaults(fn=cmd_token)

    bp_ = sub.add_parser("backup", help="备份 agent 的 data + workspace（仅 sleeping 可）")
    bp_.add_argument("agent_id", nargs="?", default="")
    bp_.add_argument("--all", action="store_true", help="备份所有 sleeping 的 agent")
    bp_.add_argument("--reason", default="manual", help="备份原因（manual/pre-modify/...）")
    bp_.set_defaults(fn=cmd_backup)

    bl_ = sub.add_parser("backups", help="列出备份包")
    bl_.add_argument("agent_id", nargs="?", default="", help="可选 agent-id 过滤")
    bl_.set_defaults(fn=cmd_backups)

    rs_ = sub.add_parser("restore", help="从备份包恢复 agent（可跨主机）")
    rs_.add_argument("--from", dest="from_path", required=True, help="备份 tar.gz 路径")
    rs_.add_argument("--new-id", default=None, help="恢复后用新 id（避免冲突）")
    rs_.add_argument("--port", type=int, default=None, help="恢复后端口（默认自动分配）")
    rs_.add_argument("--host", default="127.0.0.1", help="监听 host")
    rs_.add_argument("--overwrite", action="store_true", help="覆盖同名已存在 agent")
    rs_.set_defaults(fn=cmd_restore)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
