"""本地共享：同一服务器、同一用户名下的所有容器智能体共用一个目录。

同机同伴之间传大文件不走邮件、不拷贝——写一次，对方直接读。

- 宿主原位置：管理面目录下的 share/（cfg.share_dir，[manager] share_dir 可改）。
- 容器里：/data/share——与世界根 /data/workspace **并排、在它外面**（workspace 是
  智能体的私人领域，公共目录不放进去：它遍历、打包、索引自己的世界时不会
  误带别人的东西）。内核的文件工具与 run_shell 对绝对路径无沙箱——内核零改动。
- 只挂容器形态（runtime=docker）：挂载由 dockerctl 渲染 compose 时带上；本模块
  在每次 spawn 之前把目录就位，并给 playbook 种一份用法指南（缺则种、有不
  覆写——playbook 归大脑所有）。谁挂载谁说话：没挂载的实例不会有这份指南。
- 系统形态（systemd/bare）不挂载——进程跑在宿主上、文件系统直通，共享目录
  就是一个走得到的普通路径：给 playbook 种**相对路径版**指南（ensure_system），
  说明从实例目录出发怎么走、容器同伴看到的是同一个目录。
- 管理面文件页：挂了的实例，开放根多一个 share/，映射到共享目录本身（files.py，
  判据 = mounted_dir）——文件页看到的 = agent 看到的。
"""
import os
import re
from pathlib import Path

CONTAINER_MOUNT = "/data/share"             # 容器内：与世界根 /data/workspace 并排（HOME=/data）
MOUNT_REL = Path("share")                   # 实例侧的挂载点（宿主视角是空目录）
GUIDE_NAME = "本地共享.md"

_GUIDE_TAIL = """## 它的性质（事实，不是规矩）

- **人人可读可写**：同机任何智能体都能看、能改、能删这里的任何文件。别放凭据和
  私钥；别动不是你放的文件；清自己的子目录前先跟用的人打个招呼。
- **不在管理面备份里**：备份只备你自己的实例目录，这里的内容不随备份走——要长期
  留底的，另存进你自己的世界。
- **可能很大**：所有人放的东西都在这同一个目录里。找什么直接去对方的子目录，
  别对它做全量遍历（`find`、`grep -r` 这类）。

只有这台服务器、同一个用户名下的智能体看得到它；别的机器上看不到你放的文件。
"""

GUIDE = """# 本地共享（""" + CONTAINER_MOUNT + """）

`""" + CONTAINER_MOUNT + """` 不是你的私人世界：它是**这台服务器、同一个用户名下所有容器
智能体共用的目录**，和你的世界根 `/data/workspace` 并排、在它外面（宿主上的原位置是
管理面目录下的 `share/`，你的容器启动时把它挂进来）。
同机同伴之间传大文件走这里——写一次，对方直接读，不用邮件、不用拷贝。

## 怎么用

- **放文件**：写进 `""" + CONTAINER_MOUNT + """/<你的 id>/…`（各用自己的子目录，互不踩踏；
  你的 id 见 `playbook/我的身份.md`）。直接写到这里，别先写进自己世界再拷一份。
- **告诉对方**：用你平时联络同伴的办法（信、门铃、你自建的收信口）说清路径，
  例如「文件在 """ + CONTAINER_MOUNT + """/ab12/data-2026-11.parquet」。
- **取文件**：这里的文件你都能直接读，就地看，不必拷回自己世界。
- 它在你的世界根外面：文件工具和 run_shell 都写绝对路径（`~/share/…` 也行）。

- **它本身是挂载点**：删不掉、改不了名。清理只清自己的子目录。
""" + _GUIDE_TAIL


def system_guide(home: Path) -> str:
    """系统形态（systemd/bare）的指南：进程跑在宿主上、文件系统直通，不需要
    挂载——共享目录就是一个走得到的普通路径。说明里给**相对目录地址**（从
    实例目录出发）与绝对路径，及容器同伴视角的同一个目录。"""
    share = _share_dir()
    rel = os.path.relpath(share, Path(home))
    return f"""# 本地共享（{rel}）

`{rel}` 不是你的私人世界：它是**这台服务器、同一个用户名下所有智能体共用的目录**
（宿主上的原位置：`{share}`；同机的容器智能体在容器里看到的是 `{CONTAINER_MOUNT}`，
同一个目录——它们挂进去，你直接走到）。
你是系统形态，进程就跑在宿主上，文件系统直通——不需要挂载，从你的实例目录出发
写相对路径 `{rel}/…`，或写绝对路径，都行。
同机同伴之间传大文件走这里——写一次，对方直接读，不用邮件、不用拷贝。

## 怎么用

- **放文件**：写进 `{rel}/<你的 id>/…`（各用自己的子目录，互不踩踏；
  你的 id 见 `playbook/我的身份.md`）。直接写到这里，别先写进自己世界再拷一份。
- **告诉对方**：用你平时联络同伴的办法（信、门铃、你自建的收信口）说清路径。
  对容器同伴说 `{CONTAINER_MOUNT}/…`，对系统同伴说 `{rel}/…`——同一个地方。
- **取文件**：这里的文件你都能直接读，就地看，不必拷回自己世界。
""" + _GUIDE_TAIL


def mountable(home: Path, share_dir: Path) -> bool:
    """这次 spawn 挂不挂：共享目录在，且实例侧挂载点处没有实例自己的内容。

    实例目录里若已有非空的 share/（如裸机形态时自建的私有目录），挂载会把它
    整个盖住——里面的文件凭空消失。这种情况不挂，原样留着。"""
    if not share_dir.is_dir():
        return False
    mp = Path(home) / MOUNT_REL
    if mp.is_symlink():
        return False                        # 挂载点处是符号链接：不碰
    return not mp.exists() or (mp.is_dir() and not any(mp.iterdir()))


_MOUNT_LINE = re.compile(          # compose 里那一行卷：      - "<宿主共享目录>:/data/share"
    r'^\s*- "((?:[^"\\]|\\.)+):' + re.escape(CONTAINER_MOUNT) + r'"\s*$', re.M)


def mounted_dir(agent_id: str) -> Path | None:
    """这个实例的容器里挂的共享目录（宿主上的真实路径）；没挂 = None。

    依据 = 它上次 spawn 渲染的 compose（每次 spawn 重渲染，即当前容器定义）。
    管理面文件页据此把开放根 share/ 映射到同一处——文件页看到的 =
    agent 看到的；没重启过的存量实例、systemd/bare 形态都是 None，不会误映射。"""
    import json
    from . import dockerctl, registry
    from .config import get_config
    a = registry.get_agent(agent_id)
    if not a or a.get("runtime") != "docker":
        return None
    try:
        text = dockerctl.compose_file_for(get_config().unit_name(agent_id)).read_text(encoding="utf-8")
    except OSError:
        return None
    m = _MOUNT_LINE.search(text)
    return Path(json.loads(f'"{m.group(1)}"')) if m else None


def _share_dir() -> Path:
    from .config import get_config
    return get_config().share_dir


def ensure(home: Path) -> list[str]:
    """spawn 前调用（仅 docker 形态）：共享目录与挂载点就位 + 指南缺则种。

    返回需要人看的行（种了 / 没挂及原因）；一切照旧时返回空表——每次 restart
    都会经过这里，不该刷屏。共享目录建不出来（配置指到不可写处）只告警不拦：
    可选功能坏了不该挡住 agent 启动。"""
    home = Path(home)
    share_dir = _share_dir()
    try:
        share_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return [f"本地共享未就位：共享目录建不出来（{share_dir}：{e}）——本次不挂载"]
    if not mountable(home, share_dir):
        return [f"本地共享未挂载：实例目录里已有非空的 {MOUNT_REL}/（实例自己的内容，"
                f"挂载会盖住它）——原样留着；要启用请先把那里的东西改名或搬走"]
    (home / MOUNT_REL).mkdir(parents=True, exist_ok=True)   # 先于 dockerd 建好：它代建的属主是 root
    pb = home / "workspace" / "playbook"
    pb.mkdir(parents=True, exist_ok=True)
    p = pb / GUIDE_NAME
    if p.exists():
        return []
    p.write_text(GUIDE, encoding="utf-8")
    return [f"{GUIDE_NAME} ✓ 种入 playbook（share/ 用法；之后归大脑所有，不覆写）"]


def ensure_system(home: Path) -> list[str]:
    """spawn 前调用（systemd/bare 形态）：不挂载（宿主进程文件系统直通），
    只把**相对路径版**指南缺则种入 playbook。谁挂载谁说话的反面：没挂载的
    实例也有知情权——它是这台机上唯一不看指南就找不到共享目录的形态。"""
    try:
        _share_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        return []                       # 共享目录不存在时不种系统版指南（没东西可共享）
    pb = Path(home) / "workspace" / "playbook"
    pb.mkdir(parents=True, exist_ok=True)
    p = pb / GUIDE_NAME
    if p.exists():
        return []
    p.write_text(system_guide(home), encoding="utf-8")
    return [f"{GUIDE_NAME} ✓ 种入 playbook（相对路径版；之后归大脑所有，不覆写）"]
