# xusi 配合事项：容器原生 venv（home 侧真 venv，docker 向 systemd 看齐）

> 状态：**已被取代**（2026-09-10）——内核评审将「VENV 落点改写」方案升级为**入口 shim
> 方案**（容器改跑实例 launcher，venv 自然落实例目录，launcher 零容器特判），并经三轮
> 真机测试（破坏复原 / 跨运行时迁移矩阵 / 双智能体协作门铃）验证。xusi 配合事项以
> [agent-lifecycle.md](agent-lifecycle.md) 为准（创建/迁移/升级/复原/组网全量建议）；
> 设计底稿归内核仓 `docs/proposal-container-native-venv.md`。本文其余部分留作历史。
> 原状态：提案（内核侧评审中）· 2026-09-10 立项 · 源头：用户指示「让以后的智能体
> 都更简单」——`proposal-docker-home-writable.md` 的三件套是过渡态，本提案是终点态。

## 问题：三件套治标，怪相留在 pip 语义里

docker-home-writable（已实施）让 `pip install` 能跑通了，但 PIP_TARGET 是
pip 的边角机制，代价长期存在：

- `pip list` / `pip uninstall` **看不见** target 里的包——大脑装的包成了
  「暗物质」，重复装 venv 已有的包（如 numpy）无报错无提示；
- 大脑仍需知道避坑（别 `--user`、target 与 venv 是两个世界）；
- docker（镜像 venv + PIP_TARGET）与 systemd（实例真 venv + 缺省语义）两套
  心智——同一个 agent 切换载体，pip 行为跟着变。

根子：容器模式把 venv 烘培在 `/app/.venv`（root 属主，user 1000 只读），
`xuseek.sh` 的 venv 自愈被短路——它本来就会自建可写 venv 并管到底。

## 方案：容器分支把 VENV 落点改到 home 侧（一条赋值）

`xuseek.sh` 现状 `VENV="$DIR/.venv"`（容器 = /app/.venv）。改为容器 +
home 布局时（`[ -e /.dockerenv ] && [ "$HOME_ARG" != "$DIR" ]`，与现有
软链分支同一判据）：

```
VENV="$HOME_ARG/xuseek-v2/.venv"     # xusi 管理（源码在 home/xuseek-v2）
VENV="$HOME_ARG/.venv"               # 裸克隆布局（home == 源码目录）
```

**xuseek.sh 现有机制零新逻辑、全复用**：

| 机制 | 现状 | 容器原生 venv 下 |
|---|---|---|
| venv 自愈（122 行） | 被 /app/.venv 烘培短路 | 首启 mkvenv 到 home（user 1000 属主，可写） |
| 依赖指纹（.deps_fingerprint） | 构建期已写 | 首启现装；镜像升级换 python 小版本 → 指纹变 → 自动重装 |
| 安装互斥（.install_lock） | 闲置 | 大脑自装与启动器自愈同一把锁（systemd 现状） |
| uv 探测 | 镜像 /bin/uv | 容器内照常命中（HOME=/data，缓存落 bind mount 持久） |

效果：**venv 落 bind mount，可写、持久、容器重建不丢；docker 与 systemd
看到同一条解释器路径 `<home>/xuseek-v2/.venv/bin/python3`**——v2.7.37 的
兼容软链铺的就是这条路径，本提案让软链变成真身，路径一字不差。

大脑装包回到**完全缺省语义**：`pip install` 落 venv、`pip list`/`pip
uninstall` 正常、无 target 怪相、无 PIP_TARGET/PYTHONPATH。

## 内核侧改动清单

1. **`xuseek.sh` VENV 赋值处加容器分支**（上文两条路径）。
2. **摘链自建**：v2.7.37 软链（→ /app/.venv）可执行，会**遮蔽**自愈判据
   （`[ -x "$VENV/bin/python" ]` 对活软链为真 → 永不触发重建）。容器分支需
   先 `rm` 软链（`rm -rf` 对软链只摘不跟随——123 行注释已写明语义）再走
   自愈。与 09b6 宿主 venv 遮蔽同款问题的新变种，自家链自家清。
3. **软链补链分支（139–150 行）退场**：home 侧 .venv 已是真身。裸克隆布局
   下 `$HOME_ARG == $DIR` 本就不进该分支——社区版行为一字不变。
4. **Dockerfile 烘培段（`RUN ./xuseek.sh version`）处置**：构建期
   WORKDIR /app、无 /data——自愈会建进 /app/.venv（维持现状）或跳过（容器
   分支不触发）。建议保留构建期自检、接受 /app/.venv 仅作构建期 selftest
   环境；`XUSEEK_EXTRAS` 烘培收益见「已知取舍」。
5. `docs/container.md` / DOCKER.md 更新（运行时 venv 在 home 的说明 +
   首启装依赖时长预期）。

## xusi 侧改动清单（内核版本铺开后一笔收尾）

1. **compose env 撤两行**：删 `PIP_TARGET`/`PYTHONPATH`（`dockerctl.py`
   `_render_compose`）。**`HOME: /data` 保留**——uv/pip 缓存落点，与 venv
   无关的独立价值（docker-home-writable 的残存部分）。
2. **healthcheck `start_period` 30s → 300s**：首启装依赖分钟级（指纹命中
   后重启仍秒级，只有首次/升级慢）。`spawn_and_verify` 验收超时同步放宽。
3. **`patch_agent` 的 systemd→docker 删宿主 venv 逻辑退役**（c65dd09 引入）
   ：新世界 docker 也要 home 真 venv，「删」变成错——退回不动。
4. docs：kernel-upgrade §1 .venv 平移从此对 docker 同样适用（真 venv 平移
   免重装，两运行时行为统一）；§8 重写；container-runtime.md 更新。
5. 大脑侧（投信，不归管理面）：09b6 撕 amem `sys.path` 补丁、撤
   `/data/workspace/.local` 自救布局——正道已通，自救是负债。

## 交接顺序（硬约束：内核先行）

1. 内核新版本发布（含容器分支 + 摘链自建 + selftest 门禁过）。
2. xusi 同一笔：撤 env 两行 + start_period 拉长。
3. 存量 agent 升级内核 + 重启 → 摘链 → 自建 → 指纹装依赖。
4. patch_agent 删 venv 逻辑退役 + docs 收尾 + 本提案标完成。

反序后果（为何内核必须先行）：先撤 env 而内核未升 → `pip install` 落回
只读 `/app/.venv`，错误风暴回潮；内核升了 env 未撤 → PIP_TARGET 还在，
pip 仍落 target 而非新 venv（怪相延续但不坏）。前者伤、后者不伤——安全序
唯一。

## 已知取舍

- **首启从秒级变分钟级**（venv + 主依赖现装，uv + 清华源；指纹命中后秒级）。
  systemd 模式的既有成本平移，非新增总量。
- **`XUSEEK_EXTRAS` 烘培（amem ~2GB）收益消失**：镜像层 /app/.venv 不再是
  运行 venv，首启需现装。本队现状 `docker_extras` 未配置（compose 渲染
  `XUSEEK_EXTRAS: ""`），零影响；重度用户可另立「cp -al 引导」优化（镜像
  venv 硬链接铺设到 home，秒级起步）——另行立项，不入本提案。
- 首启装依赖依赖网络：失败 → xuseek.sh exit 1 → 容器崩溃循环自愈重试
  （与 systemd 模式同款行为）。
- 镜像内 /root/.cache 的 uv 缓存对 user 1000 本就不可用（属主 root）——
  「热缓存起步」收益在 xusi 场景从未存在过，不属于本提案损失。

## 不做的

- 不做容器层持久化（named volume）——bind mount 已是答案。
- 不动裸 compose 社区路径（root + home==DIR）：VENV 落点不变、烘培照旧、
  行为一字不差。
- 不给 PIP_TARGET 过渡期兼容钩子——交接顺序保证不并存（见上）。
- 大脑自救布局的清理走投信引导，管理面不替 agent 动 workspace。

## 验收（选 09b6 或新 agent，内核新版本 + xusi 收尾后）

- [ ] 容器内 `ls -ld /data/xuseek-v2/.venv` 为真目录（非软链），
      `bin/python3 -c ''` 可执行，属主 user 1000。
- [ ] `pip install idna` 缺省落 venv site-packages；`pip list` 可见、
      `pip uninstall` 正常——零 env 前提下成立。
- [ ] daemon 进程的解释器 = `/data/xuseek-v2/.venv/bin/python`。
- [ ] 镜像重建（tag 变化）后实例 .venv 存活、指纹命中、重启秒级。
- [ ] systemd ↔ docker 切换：`.venv` 同一路径双向可用（切换只换载体）。
- [ ] amem extras 首启现装成功（本队现状路径，~2GB）。
- [ ] 09b6：amem `sys.path` 补丁已撕、`workspace/.local` 自救布局已撤，
      事件流不再出现 pip 路径类报错。
