# xusi 改进提案：docker agent 容器内可写 HOME 与 pip 落点（HOME=/data + PIP_TARGET）

> 状态：✅ 已实施（2026-09-10 同日）· compose env 三件套落 `dockerctl._render_compose`，
> `_EXCLUDE_DIRS` 补 `.cache` **和** `.local`（「保留与否实施时定」定为排除：可重装
> 且任意层级匹配连 09b6 自救布局 workspace/.local 一并排除）· 存量 docker agent
> 下次重启重渲染 compose 自然生效，无迁移 · 立案前全部行为已在 agent-09b6 容器
> 实测（v2.7.37 镜像，见 §实证）
> 源头：2026-09-10 09b6 升级后错误风暴的溯源结论——错误全是 9/9 systemd→docker
> 切换的旧伤（见 `proposal-venv-path-compat.md` 同日验收），其中一整类
> （pip 装包死路）根因在 xusi 渲染的 compose 缺 `HOME`。

## 问题：容器里大脑的 pip 是死路

compose 以裸 `user: "1000:1001"` 起容器（`dockerctl.py` `_render_compose`）。
镜像 `/etc/passwd` 无此 uid → Docker 把 `HOME` 落成 `/`。实测后果：

| 大脑的尝试 | 结果（实测） |
|---|---|
| `pip install --user <pkg>` | `OSError: [Errno 13] Permission denied: '/.local'` |
| `pip install <pkg>`（默认进 venv） | `Permission denied: '/app/.venv/...'`（root 只读镜像层） |
| `pip cache` / HF 缓存（`~/.cache`） | `~` 展开为 `/`，同样不可写 |

09b6 的大脑被迫自救：`pip install --prefix /data/workspace/.local` + 手工往
`amem/core.py` 里补 `sys.path.insert`（sandbox 子进程的 PYTHONPATH 不含它）。
能活，但每个新 exec 上下文都要重新对付一遍——这类报错占了升级后事件流的
一大截。

## 实证（2026-09-10，agent-09b6 容器内，/app/.venv python 3.13.15）

```
HOME=/data /app/.venv/bin/python -c "import site; …"
  → ENABLE_USER_SITE: False        # venv 禁 user-site —— 光改 HOME 救不了 --user
  → user site: /data/.local/lib/python3.13/site-packages

① HOME=/data pip install --user idna
  → ERROR: Can not perform a '--user' install. User site-packages are not
    visible in this virtualenv.          # --user 在 venv 里是死路，与 HOME 无关
② HOME=/data PIP_TARGET=/tmp/pt pip install --user idna
  → ERROR: Can not combine '--user' and '--target'.
③ HOME=/data PIP_TARGET=/tmp/pt pip install idna
  → Successfully installed idna-3.19    # 普通 install 落进 target ✓
④ HOME=/data PYTHONPATH=/tmp/pt python -c "import idna"
  → idna OK 3.19                        # target 里的包可导入 ✓（含 daemon 自身）
```

结论：**三件套缺一不可**——`HOME`（可写落点+缓存）、`PIP_TARGET`（让缺省
`pip install` 有地方落）、`PYTHONPATH`（让装进去的包处处可导）。

## 改动（实施时做，两处代码）

1. **`xusi/dockerctl.py` `_render_compose` 的 `environment:` 块**（现只渲染
   `TZ` + 镜像源三件）加三行：

   ```yaml
   environment:
     TZ: …
     HOME: "/data"
     PIP_TARGET: "/data/.local/site-packages"   # 版本无关路径，别带 python3.13
     PYTHONPATH: "/data/.local/site-packages"
   ```

   生效路径：compose env → daemon 进程 →（a）daemon 自身 sys.path 尾部
   （内核技能 amem 直接 `import numpy` 复活，不再依赖大脑往 core.py 补
   sys.path）；（b）`tools/shell.py` `_run` 保留既有 PYTHONPATH 并前插内核
   包根 → run_shell / run_python / 大脑的一切子进程全继承。

2. **`xusi/backup.py` `_EXCLUDE_DIRS`** 补 `.cache`（pip/HF 纯缓存，torch
   量级 500MB+，绝不该进备份）。`.local` 建议同补（可重装；保留与否实施时
   定——注意它已落在实例目录、随 bind mount 持久、镜像重建不丢）。

## 已知行为变化（实施时告知）

- 大脑若仍习惯性 `pip install --user`：与 PIP_TARGET 撞车报
  `Can not combine '--user' and '--target'`——报错信息本身可行动（去掉
  --user 即可），LLM 读得懂；比现在两条路全 Permission denied 好。
- 内核 Python 小版本升级后 `.local/site-packages` 里的二进制 wheel（cp313）
  会失配：pip 重装即愈；实施时可在大脑 BOOT.md 层面留一句，不归管理面管。
- systemd 模式不受影响（HOME=管理面用户真实 home，本就可写）；两运行时
  `~` 展开不同（docker=/data、systemd=/home/<user>）——本提案目标「可写」
  而非「同路径」，同路径话题归内核 venv 兼容软链（`proposal-venv-path-compat.md`）。

## 铺开与兼容

- compose 每次 spawn 重渲染：改动合入后，各 docker agent **下次重启自然
  生效**，无迁移、幂等；存量大脑的自救布局（`/data/workspace/.local` +
  amem sys.path 补丁）与新落点并存不冲突。
- 09b6 验收（实施后）：容器内 `echo $HOME`=`/data`；`pip install idna`
  默认落 `/data/.local/site-packages` 且 `python -c "import idna"` 直接过；
  `pip install --user` 报 combine 错（预期）；amem 技能（agentic_memory）
  在 daemon 内不再因 numpy 崩。

## 不做的

- 不改内核（xuseek）：shell.py 的 env 传递语义现成正确，改的是它的输入。
- 不给 `/.local`、`/root/.local` 建目录或调容器 passwd——治标且脏。
- 不动 systemd 模式渲染。
