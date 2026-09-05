"""Docker 运行时操控：agent 容器生命周期（compose + host 网络）。

函数形状与 systemdctl.py 对齐（Runtime 协议）：
spawn_agent / unit_state / unit_brief / unit_load_state / stop / restart /
main_stopped / kill_signal / reset_failed / journal_tail。

容器名 = systemd 单元名（xusi-a-<id>，复用 cfg.unit_name）；compose.yaml 由
管理面渲染在 instances/.compose/<unit>/compose.yaml —— 实例根（/data 挂载）
之外的兄弟目录，容器内大脑看不到也改不到；spawn 每次重渲染，路径/端口/
镜像 tag 恒与注册表一致。镜像 tag 含内核版本（xuseek-agent-<id>:<version>）：
升级 source_version 后 tag 变化 → 镜像缺失 → 自动重建（构建含内核 selftest
门禁）。容器是可弃的一次性运行时，实例状态全在 bind mount（见内核 DOCKER.md）。

信号路径（main_stopped/kill_signal）走 docker exec 在容器内完成——管理面是
普通用户（systemd --user），对宿主 root 的容器进程发不了信号；docker exec
以容器内 root 身份行动，与 systemd 模式同样只作用于 daemon 主进程
（不用 docker pause——那会连大脑自起的服务一起冻，语义不一致）。

daemon 主进程定位：xuseek.sh 以 `exec python -m xuseek …` 启动 daemon，
容器内 pgrep -f 匹配该 cmdline（镜像自带 procps）。误匹配概率极低，最坏
后果只是冻错进程，与 systemd 侧「杀错 PID」同类风险。

manager_running 不在此协议内：xusi 管理面自身恒为 systemd 用户服务。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# 默认 PyPI 镜像（与 systemdctl.DEFAULT_UV_INDEX_URL 同一默认，cfg.docker_pip_index
# 未配置时用它；显式空串 = 关闭镜像）。本机直连 pypi.org 不可达的历史教训。
DEFAULT_UV_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"


class DockerError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: float = 30) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise DockerError(f"命令超时：{' '.join(cmd[:3])} …（{e.timeout}s）") from e
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        raise DockerError(err[-1] if err else f"exit {r.returncode}: {' '.join(cmd[:4])}")
    return r.stdout


def docker_available() -> tuple[bool, str]:
    """探测 docker daemon 与 compose 插件，返回 (可用, 提示)。

    三层探测：socket 可读性预判 → daemon 在线 → compose 插件可调。
    权限不足（当前用户不在 docker 组）与 daemon 挂掉都要识别为不可用——
    spawn 前置与 doctor 共用；提示必须可行动（usermod -aG docker）。
    socket 预判先行：管理面是 systemd --user 普通用户场景，无写权限时
    docker CLI 也会说 permission denied，先做本地检查少走一条子进程；
    CLI 报错的字符串匹配只作兜底（i18n/新版本措辞变化不可依赖）。
    """
    # ① socket 可读性预判
    sock = Path("/var/run/docker.sock")
    if sock.exists() and not os.access(sock, os.R_OK | os.W_OK):
        return False, (
            f"当前用户无 docker.sock 访问权限（{sock} 不可读写）——"
            f"sudo usermod -aG docker $USER 后重新登录"
        )
    # ② daemon 在线
    try:
        _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
    except DockerError as e:
        msg = str(e)
        low = msg.lower()
        if "permission denied" in low or ("dial unix" in low and "permission" in low):
            return False, (
                f"当前用户无 docker.sock 访问权限——sudo usermod -aG docker $USER 后重新登录"
                f"（原始：{msg}）"
            )
        return False, f"docker daemon 不可用：{msg}"
    # ③ compose 插件
    try:
        _run(["docker", "compose", "version"], timeout=15)
    except DockerError as e:
        return False, f"docker compose 插件不可用：{e}"
    return True, ""


# ── compose 渲染 ─────────────────────────────────────────────────────────

def compose_file_for(unit: str) -> Path:
    """渲染目录按 unit 命名（不按 agent id——避免从 unit 反解 id 的 hack）。"""
    from .config import get_config
    return get_config().compose_dir / unit / "compose.yaml"


def _compose_args(unit: str) -> list[str]:
    """compose 命令公共前缀。project name 显式钉为 unit——不依赖 compose
    对 compose.yaml 所在目录名的推导（渲染目录名 = unit，推导结果其实一致，
    显式钉死是为了杜绝改名/移动目录引起的静默行为变化）。"""
    return ["docker", "compose", "-f", str(compose_file_for(unit)), "-p", unit]


def _image_tag(unit: str, version: str) -> str:
    """镜像 tag 按 agent + 内核版本：升级 source_version → tag 变 → 自动重建。"""
    agent_id = unit[len("xusi-a-"):] if unit.startswith("xusi-a-") else unit
    ver = (version or "latest").strip()
    # tag 合法字符集 [A-Za-z0-9._-]；非法字符替换为 -（版本号经 versions._VER_RE
    # 校验过，这里只是兜底）
    ver = "".join(c if c.isalnum() or c in "._-" else "-" for c in ver) or "latest"
    return f"xuseek-agent-{agent_id}:{ver}"


def _render_compose(unit: str, source_dir: Path, home: Path, host: str,
                    port: int, version: str) -> str:
    """渲染 compose.yaml（全绝对路径字面量、无 ${} 插值——文件自足可审计）。

    与内核 compose.example.yaml 的差异：build context 指实例私有副本
    xuseek-v2、镜像 tag 含版本、logging 补 json-file 轮转（docker 默认无上限，
    长跑 agent 会写穿磁盘）、**user 钉为管理面用户**（cfg.docker_user）——
    内核模板默认 root，但 root 写进 /data 的文件宿主属主是 root，管理面
    （普通用户）就写不了 mailbox.jsonl / webui_tokens.json（投信与观察台
    token 签发会 PermissionError）。钉成管理面用户后容器内大脑的能力与
    systemd 模式完全对齐（同 uid），落盘文件属主一致，管理面读写照常。
    另补 **cap_add: NET_BIND_SERVICE**——普通用户 bind 1024 以下端口会被
    内核拒绝（agent-8e09 实测 Permission denied），给这个窄能力后大脑可
    直接监听 80/443（不限制 agent 能力；该 cap 仅放开特权端口绑定）。"""
    from .config import get_config
    cfg = get_config()
    pip_index = cfg.docker_pip_index
    if pip_index is None:
        pip_index = DEFAULT_UV_INDEX_URL
    index_env = ""
    pip_arg = ""
    if pip_index:
        index_env = (
            "      UV_INDEX_URL: {j}\n"
            "      PIP_INDEX_URL: {j}\n"
            "      UV_DEFAULT_INDEX: {j}\n"
        ).format(j=_yq(pip_index))
        pip_arg = f"        PIP_INDEX: {_yq(pip_index)}\n"
    health = f"curl -fsS http://127.0.0.1:{port}/v1/health || exit 1"
    return f"""# 由 xusi 管理面渲染（runtime=docker）——不要手改：spawn 每次都会重渲染，
# 路径/端口/镜像 tag 恒与注册表一致。镜像 tag 含内核版本：升级 source_version
# 后 tag 变化 → 自动重建（构建含内核 selftest 门禁）。排障见 docs/container-runtime.md。
services:
  xuseek:
    build:
      context: {_yq(str(source_dir))}
      args:
        XUSEEK_EXTRAS: {_yq(cfg.docker_extras)}
        APT_MIRROR: {_yq(cfg.docker_apt_mirror)}
{pip_arg}    image: {_yq(_image_tag(unit, version))}
    container_name: {_yq(unit)}
    network_mode: host
    user: {_yq(cfg.docker_user)}
    cap_add:
      - NET_BIND_SERVICE
    volumes:
      - {_yq(f"{home}:/data")}
      - {_yq(f"{source_dir}/xuseek:/app/xuseek")}
    environment:
      TZ: {_yq(cfg.display_timezone)}
{index_env}    restart: unless-stopped
    stop_grace_period: 30s
    command: ["serve", "--host", {_yq(host)}, "--port", {_yq(str(port))}]
    healthcheck:
      test: ["CMD-SHELL", {_yq(health)}]
      interval: 15s
      timeout: 3s
      start_period: 30s
      retries: 5
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
"""


def _yq(s: str) -> str:
    """JSON 双引号字符串——JSON 是 YAML 子集，标量一律走它防注入/转义问题。"""
    import json
    return json.dumps(s, ensure_ascii=False)


# ── 协议函数 ─────────────────────────────────────────────────────────────

def _inspect(unit: str, fmt: str, timeout: float = 15) -> str:
    """docker inspect 单属性取值；容器不存在/daemon 不可用归一为 DockerError
    （调用方按错误文案区分 not-found 与 unknown）。"""
    return _run(["docker", "inspect", unit, "--format", fmt], timeout=timeout).strip()


def _err_kind(e: Exception) -> str:
    """DockerError → not-found（容器不存在） / unknown（daemon 挂、权限等）。"""
    msg = str(e).lower()
    if "no such" in msg:
        return "not-found"
    return "unknown"


def spawn_agent(unit: str, source_dir: str, home: str, host: str, port: int, *,
                version: str = "") -> None:
    """渲染 compose → （镜像缺失才）构建 → 拉起容器（host 网络）。

    构建参数（PIP_INDEX/APT_MIRROR/XUSEEK_EXTRAS）直接渲染进 compose 文件，
    不走 CLI 传参——文件自足可审计。构建在 compose up 之前同步完成，不挤占
    wait_health 的 90s 验收窗（验收只量「容器 active + 端口监听」这段秒级过程）。
    """
    ok, hint = docker_available()
    if not ok:
        raise DockerError(f"docker 不可用：{hint}")
    src = Path(source_dir)
    if not (src / "Dockerfile").is_file():
        raise DockerError(
            f"该内核版本不含 Dockerfile（{src}）：容器运行时需 xuseek-v2 ≥ v2.7.19，"
            f"升级内核走 docs/kernel-upgrade.md")
    state = unit_state(unit)
    if state in ("active", "activating"):
        raise DockerError(f"容器 {unit} 已在运行（{state}）")
    # exited / failed / created 等残留：up 前先 rm 一次（compose rm -f 幂等）——
    # 否则 container_name 撞名报 Conflict（down 没回收干净的旧容器、
    # daemon 挂掉后手动启的遗留）。rm 失败不阻塞：up 仍会给出真实报错
    if state != "not-found":
        try:
            subprocess.run(_compose_args(unit) + ["rm", "-f"],
                           capture_output=True, text=True, timeout=30, check=False)
        except subprocess.TimeoutExpired:
            pass

    # 每次重渲染（原子：tmp + os.replace + 600，与 registry._save 同手法）——
    # expose 切换后 --host 变化、端口/镜像 tag 恒与注册表一致
    cf = compose_file_for(unit)
    cf.parent.mkdir(parents=True, exist_ok=True)
    tmp = cf.with_suffix(".yaml.tmp")
    tmp.write_text(_render_compose(unit, src, Path(home), host, port, version),
                   encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, cf)

    tag = _image_tag(unit, version)
    has_image = subprocess.run(["docker", "image", "inspect", tag],
                               capture_output=True, text=True,
                               timeout=30).returncode == 0
    if not has_image:
        try:
            subprocess.run(_compose_args(unit) + ["build"], capture_output=True,
                           text=True, timeout=1800, check=True)
        except subprocess.TimeoutExpired as e:
            raise DockerError(f"镜像构建超时（30 分钟）：{tag}") from e
        except subprocess.CalledProcessError as e:
            out = ((e.stdout or "") + (e.stderr or "")).strip().splitlines()
            raise DockerError(
                f"镜像构建失败：{tag}（输出尾部：\n" + "\n".join(out[-30:]))
    _run(_compose_args(unit) + ["up", "-d"], timeout=120)


def unit_state(unit: str) -> str:
    """active / activating / inactive / failed / not-found / unknown。

    daemon 挂掉/权限不足 → unknown（**不是** not-found）——delete/reconcile
    据此区分「容器不存在」与「查不到」，前者才能放心清理。"""
    try:
        status, exit_code = _inspect(unit, "{{.State.Status}}|{{.State.ExitCode}}").split("|", 1)
    except DockerError as e:
        return _err_kind(e)
    if status == "running":
        return "active"
    if status == "restarting":
        return "activating"
    if status == "paused":          # docker pause 未用，预留
        return "active"
    if status == "exited":
        return "failed" if (exit_code or "0") != "0" else "inactive"
    return "inactive"               # created / dead / removed 残余


def unit_brief(unit: str) -> dict:
    """容器摘要，形状与 systemdctl.unit_brief 逐键对齐（UI 零差异消费）。"""
    try:
        out = _inspect(unit, "{{.State.Status}}|{{.State.Pid}}|{{.RestartCount}}"
                            "|{{.State.StartedAt}}|{{.State.ExitCode}}")
        status, pid, nres, started, exit_code = out.split("|", 4)
    except DockerError as e:
        kind = _err_kind(e)
        return {"active": kind, "sub": "", "main_pid": None, "auto_restarts": 0,
                "last_exit_status": None, "active_since": None, "error": str(e)}
    if status == "running":
        active, sub = "active", "running"
    elif status == "restarting":
        active, sub = "activating", "restarting"
    elif status == "paused":
        active, sub = "active", "paused"
    elif status == "exited":
        active, sub = ("failed", "exited") if (exit_code or "0") != "0" else ("inactive", "exited")
    else:
        active, sub = "inactive", status or "created"
    return {
        "active": active,
        "sub": sub,
        # State.Pid = 容器 PID 1（tini）的宿主 PID，仅展示语义——
        # main_stopped/kill_signal 不用它（走容器内 daemon 进程）
        "main_pid": int(pid) if pid.lstrip("-").isdigit() else None,
        "auto_restarts": int(nres) if nres.lstrip("-").isdigit() else 0,
        "last_exit_status": int(exit_code) if exit_code.lstrip("-").isdigit() else None,
        "active_since": started or None,
    }


def unit_load_state(unit: str) -> str:
    """loaded / not-found / unknown。"""
    try:
        _inspect(unit, "{{.Id}}")
        return "loaded"
    except DockerError as e:
        return _err_kind(e)


def stop(unit: str) -> None:
    """compose down：停 + 回收容器（对标 systemd 瞬态单元停止后回收）。
    幂等：容器不存在视为成功。down 尊重 compose 的 stop_grace_period 30s
    （> 内核 10s 优雅停窗，SIGTERM 落盘后回收）。"""
    if unit_load_state(unit) == "not-found":
        return
    try:
        _run(_compose_args(unit) + ["down"], timeout=120)
    except DockerError as e:
        # 竞态/渲染文件缺失：容器已不在即视为已停止（与 systemdctl.stop 的
        # "not loaded" 容错同构）
        if unit_load_state(unit) == "not-found":
            return
        raise


def restart(unit: str) -> None:
    """复用现有容器重启（agentops 只在 unit active 时调它——与 systemdctl
    同语义）。"""
    _run(_compose_args(unit) + ["restart"], timeout=120)


def _daemon_pid(unit: str) -> int | None:
    """容器内 xuseek daemon 进程的容器 PID（pgrep 匹配 cmdline；镜像带 procps）。

    xuseek.sh 以 `exec "$VENV/bin/python" -m xuseek "$@"` 启动 daemon——
    匹配 "-m xuseek serve"。容器未运行/daemon 未起 → None（吞 DockerError）。
    """
    try:
        r = subprocess.run(
            # 模式不能以 "-" 开头（pgrep 会当选项解析）——用 python.* 前缀锚住
            # daemon 的 cmdline（/app/.venv/bin/python -m xuseek serve …）
            ["docker", "exec", unit, "pgrep", "-f", "python.*-m xuseek serve"],
            capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.strip().splitlines():
        if line.strip().isdigit():
            return int(line.strip())
    return None


def main_stopped(unit: str) -> bool:
    """daemon 主进程是否处于 SIGSTOP 冻结态（容器内 /proc State: T）。

    与 systemdctl.main_stopped 同一判据（只读，不改状态）；systemd 层（这里是
    docker 层）看不出冻结——容器状态仍 running。"""
    pid = _daemon_pid(unit)
    if not pid:
        return False
    try:
        out = _run(["docker", "exec", unit, "ps", "-o", "stat=", "-p", str(pid)],
                   timeout=15)
    except DockerError:
        return False
    return out.strip().startswith("T")


def kill_signal(unit: str, sig: str) -> None:
    """给容器内 daemon 主进程发信号（SIGSTOP/SIGCONT：暂停/续跑/备份冻结窗）。

    走 docker exec 在容器内发——管理面是普通用户，对宿主 root 的容器进程
    发不了信号；exec 以容器内 root 行动。只冻 daemon，不碰 tini 与大脑
    自起的服务（与 systemd 模式「只冻主进程」语义一致）。"""
    pid = _daemon_pid(unit)
    if not pid:
        raise DockerError(f"找不到 agent daemon 进程（容器 {unit} 未运行或 daemon 未起）")
    _run(["docker", "exec", unit, "kill", f"-{sig}", str(pid)], timeout=15)


def reset_failed(unit: str) -> None:
    """noop：容器没有 systemd 的 failed 阻塞态，down 已回收。协议对称占位。"""
    return None


def journal_tail(unit: str, n: int = 200) -> str:
    """容器最近 n 行日志（json-file，compose 渲染时配了 10m×3 轮转）。"""
    try:
        return _run(["docker", "logs", "--tail", str(n), "--timestamps", unit],
                    timeout=15)
    except DockerError as e:
        return f"（日志读取失败：{e}）"


def cleanup(unit: str) -> None:
    """删 compose 渲染目录（delete/回滚/runtime 切换用）。镜像保留——
    `docker image prune` 交给管理员（docs/container-runtime.md）。

    与同 unit 的 spawn 存在理论竞争窗口（rmtree vs mkdir）；spawn 端每次
    mkdir 都带 exist_ok=True，自愈。cleanup 是清理动作，不抛错——抛错会让
    上层 patch_agent 把「改参切运行时」回退到旧载体，体验上「改了等于没改」。"""
    d = compose_file_for(unit).parent
    try:
        shutil.rmtree(d)
    except FileNotFoundError:
        pass  # 并发已被另一侧 rmtree 先一步，OK
    except OSError as e:
        print(f"[xusi] cleanup({unit}) 失败：{e}")
