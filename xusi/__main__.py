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
import re
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


def cmd_install(args) -> int:
    py = _ensure_venv()
    # xuseek-v2 源码：自管于本目录下，缺失时从 GitHub 拉取（etc/xusi.toml source_repo）
    from . import agentops
    src = agentops.ensure_source()
    print(f"==> xuseek-v2 源码就位：{src}")
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
    from . import brains, ports, systemdctl
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
    src_ok = (cfg.source_dir / "xuseek.sh").exists()
    check("xuseek-v2 源码（自管）", src_ok,
          f"{cfg.source_dir}" + ("" if src_ok else f"（缺失；创建 agent 时自动从 {cfg.source_repo} 拉取）"))
    check("xuseek 源码 venv 可用", (cfg.source_dir / ".venv" / "bin" / "python").exists()
          or not src_ok, "首次 spawn 时由 xuseek.sh 自动构建")
    from . import versions
    vs = versions.list_versions()
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

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
