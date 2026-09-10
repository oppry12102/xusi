# xusi 配合事项：容器解释器路径兼容软链（内核 v2.7.37 起；提案原写 v2.7.38，实发版即含）

> 状态：四项全部完成（2026-09-10）· 第 1–2 项文档落地（kernel-upgrade.md §8
> 同句一并改）· 第 3 项切换代码落地（patch 移除宿主真 venv）· 第 4 项 09b6
> 升级 v2.7.37 实测通过（提案原写 v2.7.38 起，实发 v2.7.37 即含软链）·
> 内核侧已实施
> 设计底稿与评审纪要见内核仓 `docs/proposal-venv-path-compat.md`（本文件只列
> xusi 需要动的部分）。源头：2026-09-09 agent-09b6「8404 服务无法重启」事故。

## 内核侧已落地（行为契约）

- `xuseek.sh` 启动时（每次调用，幂等）：容器内（`/.dockerenv` 且 home≠内核目录）
  给 home 视角的源码目录补 `.venv → /app/.venv` 兼容软链——xusi 布局落在
  `<实例>/xuseek-v2/.venv`，裸克隆布局落在 `<实例>/.venv`。已存在（真目录或链）
  一律不动；失败留一行 stderr，不算致命。
- 效果：**`<实例>/xuseek-v2/.venv/bin/python3` 在 systemd 与 docker 两个运行时下
  是同一条有效路径**——大脑自起脚本（watchdog/采集器）硬编码它不再因运行时
  切换失效。09b6 那类事故的结构性修复。
- 配套：venv 自愈认死链（`-e` 改 `-e||-L`，docker→systemd 反向切换遗留的死链
  会被识别并重建为真 venv）；`.dockerignore` 改 `**/.venv`（软链不进 build
  context）；`.gitignore` 改 `.venv`（软链不脏 git status）。

## xusi 需要修改（4 项：3 项文档/文案 + 1 项切换代码）

1. **`docs/container-runtime.md` §内核升级（容器版）改写**——现文
   「docker agent 跳过 `.venv 平移`步骤也安全（venv 烘培在镜像里，实例目录里
   没有 .venv）」在软链落地后不再为真。建议改为：
   > docker agent 跳过 `.venv 平移`步骤也安全（venv 烘培在镜像里；实例目录的
   > `.venv` 只是内核首启自建的兼容软链——升级换源码目录时旧链随旧树走，
   > 新树首启自动补链；删除只摘链不跟随）。
2. **`docs/container-runtime.md` §语义对齐表补一行**：

   | 事项 | systemd | docker |
   |---|---|---|
   | `.venv` 解释器路径 | `<实例>/xuseek-v2/.venv`（真 venv，xuseek.sh 首启自建） | **同路径**（兼容软链 → 镜像 `/app/.venv`；内核首启自建，死链自愈） |

3. **§创建/切换（systemd→docker）确认遮蔽处置**——实例目录若曾有宿主真
   `.venv`，它会**遮蔽**兼容软链（`ln -s` 跳过已存在项），而宿主 venv 的解释器
   路径在容器里多半失效 = 09b6 同款复现。✅ 已落地：`patch_agent` 的 runtime
   切换在 systemd→docker 时自动移除真目录 `.venv`（软链不动，留给内核自愈；
   audit 记 `venv_removed`）。内核 `docs/container.md` §迁移 已加同款指引
   （社区版无管理面兜底的情形）。
4. **09b6 实例验收**（2026-09-10 已过，v2.7.34 → v2.7.37 升级 + 重建二连实测）——内核版本滚到该实例后首启：
   - [x] 容器内 `ls -l /data/xuseek-v2/.venv` 为指向 `/app/.venv` 的软链，
         `/data/xuseek-v2/.venv/bin/python3 -c ''` 可执行（python 3.13.15）
   - [x] 其 watchdog/自起脚本（硬编码该路径）原样复活，8404 服务拉起
         （首启 ~60s 后 inboxd 监听 0.0.0.0:8404；watchdog + roll_prediction_mkt
         全家在跑；现存脚本用的是大脑事故后自改的 `/app/.venv` 兜底路径——
         规范软链路径已验证可执行，两条路都通）
   - [x] 镜像重建后链仍在（软链未进 build context）——删镜像强制重建实测：
         构建正常（`.dockerignore **/.venv` 挡住 context 里的软链），链原样保留

## 不需要做的

- 兼容层不覆盖实例目录里真实存在的 `.venv`（破坏性动作不归内核兼容层；遮蔽
  情形由第 3 项的切换步骤处置——管理面切换时移除真目录，软链留给内核）。
- rootless podman 不在面内（`/.dockerenv` 检测是 docker 主路径判据；podman 用
  `/run/.containerenv`，真出现时再议）。
