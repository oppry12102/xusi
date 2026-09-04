# 存量 agent 内核升级实战（admin playbook）

> 沉淀自 2026-08-28 三连升级实验（v2.5.2 → v2.5.3 → v2.5.4 → v2.5.5：
> agent-0e50 两轮升级 + 4 个一次性验证 agent 建删全流程）。
> API 层「source_version 创建后不可改」约束的是**创建流程**；存量升级是目录级
> 操作，本文是标准做法。前置：管理面代码 ≥ `ca56645`（分档语义与内核 v2.5.5
> 对齐：未标注 tier 视同 power）。
> **当前目标版本：v2.7.12（2026-09-02 投放）**。v2.5.x → v2.7.x 是同一套目录级
> 流程；运行时依赖零变化（pyproject 只差版本号行），坑④的 .venv 平移结论不变。
> v2.7.5 清理了 `[agent]` 预算段（见 §5）——升级后存量 config.toml 里的该段是
> 死配置，投信让 agent 清掉即可。v2.7.12 起**互联由内核自己完成**（根智能体 +
> `[[roots]]` 出生交割，见 §5）；存量 agent 升级后 config 里没有 `[[roots]]`
> 也照常启动，只是暂不接入互联。

## 0. 前置条件（顺序重要）

1. **git pull 管理面**到 `ca56645` 及之后——老代码把未标注脑分到 "premium" 档，
   与内核 v2.5.5 的 "power" 档不一致（只影响混合标注池的预算推导）。
2. **versions/ 放入内核新包**：`xuseek-v2-<版本>.zip`（打包方法见
   [versions.md](versions.md)；包内根目录或唯一一级子目录两种布局都认）。
3. **etc/brains.toml 补齐每脑数据**：`tier`（power/economy）、`context_window`、
   economy 脑加 `note`（如"免费（自托管）"）。内核 v2.5.5 的**同档循环与按脑
   预检吃的就是这些数据**——不补则升级无收益（窗口未声明 = 不预检；tier 未
   标注全视同 power；行为与旧版相同，无害但白升）。

## 1. 升级操作（单 agent，停机约 1 分钟）

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)   # 坑②：systemctl --user 必需
cd <xusi 目录>
python3 - <<'EOF'
import sys, shutil
sys.path.insert(0, ".")
from xusi import registry, agentops, versions
from xusi.config import get_config

AID, NEW = "agent-XXXX", "v2.7.4"          # ← 只改这两处
agent = registry.get_agent(AID)
OLD = agent["source_version"]
home = get_config().instance_home(AID)

agentops.stop(AID)                          # 1) 优雅停（agentops.stop：冻结进程先
                                            #    SIGCONT 解救再停，不裸调 systemdctl；
                                            #    desired_state 落 stopped，中途死不
                                            #    谎报，reconcile 不从半迁移目录乱拉）
src, tmp = home / versions.SRC_DIR_NAME, home / "xuseek-v2.new"
bak = home / f"xuseek-v2.old-{OLD}"
for d in (tmp, bak):
    if d.exists(): shutil.rmtree(d)
versions.extract(NEW, tmp)                  # 2) 官方解压器（坑①：勿用裸 unzip）
shutil.move(str(src / ".venv"), str(tmp / ".venv"))   #   .venv 平移免重建（坑④）
src.rename(bak); tmp.rename(src)            #   旧树留作回滚
registry.update_agent(AID, {"source_version": NEW})   # 3) 坑③：API 改不了这字段
agentops.audit("upgrade_kernel", agent=AID, **{"from": OLD, "to": NEW})
agentops.spawn_and_verify(registry.get_agent(AID))    # 4) 拉起 + 健康验收
print("完成，旧树备份:", bak.name)
EOF
```

## 2. 升级后：config.toml 由 agent 自己改（v2 起 xusi 不再重渲染）

**内核升级不会重写 config.toml**；v2 起 xusi 也不再重渲染（config 只在创建时
渲染一次，此后归 agent 自治）。要把 tier / context_window / 预算 / note 写进去：

1. 管理员改 `etc/brains.toml`（tier/context_window/note 等新数据）；
2. **投信**给 agent：把新键值原文发给它，让它用 run_shell 自己编辑自己的
   `config.toml`（内核每口呼吸热重载，下一口生效）。

```python
from xusi import agentops
agentops.mail(AID, "请把你 config.toml 的 [brains.glm] 段更新为：tier = \"power\"、"
                   "context_window = 131072。另外 v2.7.5 起 [agent] 预算段已废除："
                   "max_context_tokens 由内核按大脑窗口自动派生、max_seconds 已删除，"
                   "请删掉 [agent] 段里的这两个键；如需轮数限额改用 [limits] max_rounds。"
                   "改前先自行备份 config.toml。")
```

## 3. 踩过的坑（每条都真实发生过）

| # | 坑 | 正解 |
|---|---|---|
| ① | 用 `zipfile.extractall` / 裸 unzip 解内核包 → `xuseek.sh` 644 → systemd 报 `Permission denied` 拉不起 | 一律走 `versions.extract`：还原权限位、无条件保证 `xuseek.sh` 可执行、防 zip-slip |
| ② | 后台脚本调 `systemctl --user` 全部 `not-found` | 先 `export XDG_RUNTIME_DIR=/run/user/$(id -u)` |
| ③ | PATCH 改 `source_version` 被拒（`_PATCHABLE` 白名单不含它） | `registry.update_agent()` 直改；不改则 webui/备份恢复显示错版本 |
| ④ | 担心 `.venv` 要重建 | 平移即可（`mv` 进新树，路径不变）；v2.7.4 依赖与 v2.5.x 完全一致（pyproject 只差版本号行），指纹不漂移不重装；今后依赖变了 xuseek.sh 也会按指纹自愈补装（有 uv 用 uv，失败回落 pip） |
| ⑤ | 担心停单元时 manager 抢拉 | 不会——reconcile 只在 manager 重启时跑，手动操作窗口安全 |

## 4. 验证清单（升级后 5 分钟）

- [ ] journal 启动横幅：`大脑：X（故障转移兜底: …）`——兜底名单应是**同档**脑，不是全池
- [ ] `config.toml`：`[brains.X]` 带 `tier` / `context_window`；
      旧 `[agent]` 预算段已清掉（v2.7.5 不再认——max_context_tokens 改自动派生、
      max_seconds 删除；轮数限额只剩 `[limits] max_rounds`）
- [ ] 事件流 `llm_response.brain` 正常粘滞、无 `llm_error` / `llm_retry` 风暴（观察 1~2 个会话）

## 5. 语义变化提醒（内核 v2.5.5）

- **故障转移同档循环**：跨档不再自动兜底；档内全灭的错误会提示其它档（跨档走
  投信让 agent 自己改 default）。
- **未标注 tier 视同 power**：全未标注的存量池行为一字不差；「已标注 + 未标注」
  混合池里，未标注脑会加入 power 轮转。
- **发送前按 context_window 预检**（≥2 脑的池跳过装不下的脑）；单脑池无预检，
  400 自然报错。
- **playbook 种子只补缺**：存量 agent 的 `llm-调用.md` 不会自动更新（归大脑
  所有）；要告知智能体新档位语义走 send_mail。
- 会话内预算恒定的不变量成立：预算 = 同档最小 − 8k，任何中途换脑都不会把
  可用窗口换小。

### 内核 v2.7.x 新增（2026-08-29，v2.7.4）

- **撤 init**：升级流程不受影响（playbook 从不调 init）。serve/run 预检就是唯一
  引导点：config 缺失才写模板（xusi 创建时已渲染，不会触发）；经验库/能力包
  种子无条件幂等补播。
- **能力包 `[capabilities]`**：开关写 config.toml（机器不代写，可投信让 agent
  自改）；开启包的重依赖由 xuseek.sh 启动时按指纹自愈安装，装不上软失败只警告、
  不阻呼吸。
- **xuseek.sh 自愈增强**：venv 失效自动重建；依赖指纹（python 版本 + 主依赖 +
  已开 extras）不一致才重装。
- 升级后顺带投信告知：`./xuseek.sh capabilities list` 看本版本能力包；种子已在
  workspace 播好，用不用归大脑。

### 内核 v2.7.5（2026-08-30）：清理 [agent] 预算段

- **max_seconds 删除**；**max_context_tokens 改为自动派生**（default 同档**可用**脑
  已声明窗口的最小值 − 8192，内核现场活算、不可配置——管理面/管理员都不再手算）。
- **可配置限额只剩 `[limits] max_rounds`**（0 = 不限；到顶优雅结束）。
- 升级后存量 config.toml 的 `[agent]` 段是死配置（内核不认、静默忽略），
  投信让 agent 清掉（见 §2 模板）；不清也不报错，只是留着误导人。
- 管理面已同步（xusi ≥ 本提交）：创建渲染按所选内核版本分叉——≥2.7.5 写
  `[limits] max_rounds`（budgets 里的 max_seconds/max_context_tokens 渲染时
  忽略并在配置里留注释），更早版本仍写 `[agent]` 三段。

### 内核 v2.7.12（2026-09-02）：互联由内核自完成 + [[roots]] 出生交割

- **xusi 的互联公告板已删除**（管理面 v2.2.0）：publish/request_directory 信封、
  注册表 interconnect 字段、WebUI 互联标注全部移除——互联不再经过 xusi，
  xusi 彻底本地化管理。
- **根智能体（目录服务）**是互联发现的唯一方案（内核 docs/interconnect.md）：
  实例间两两直连，根只解决「互相知道」这一件事。token 由根签发，管理员只是
  把它抄进出生 config 的信使。
- **`[[roots]]` 出生交割键**：address + token 齐备的条目在启动预检时一次性
  交割到 `workspace/playbook/根智能体.json`（与 mission → 初心.md 同构），
  交割后 config 里的该段即死键；重交割 = 删该文件 + 改该段 + 重启。
- **存量 agent 接入互联**：升级后 config 无 `[[roots]]` → 照常启动、暂不接入。
  要接入时**投信**把根地址与 token 发给 agent，让它自己加 `[[roots]]` 段
  （v2.7.12 内核认识；config 每口呼吸热重载，但交割发生在启动预检——加段后
  需一次重启生效，用卡片上的「⟳ 重启」即可）。
- **新建 agent**：xusi ≥ v2.2.0 的创建对话框 / `POST /api/agents` 有 `roots`
  字段（仅 v2.7.12+ 内核有效，选了旧版内核时创建报错 400）。

## 6. 回滚

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user stop <unit>
mv instances/<id>/xuseek-v2/.venv instances/<id>/xuseek-v2.old-<旧版>/.venv
mv instances/<id>/xuseek-v2 instances/<id>/xuseek-v2.failed
mv instances/<id>/xuseek-v2.old-<旧版> instances/<id>/xuseek-v2
# 再 registry.update_agent 改回旧版本号 + spawn_and_verify（同 §1 脚本尾段）
```

## 7. 批量升级建议

- 先升 1 个 agent 观察半天，再批量（§1 脚本循环 AID 列表，各自停机 ~1 分钟）。
- 一次性 / 实验 agent 直接**删除重建**更省事：新建缺省即取 versions/ 最新版。
- 稳定后删掉 `xuseek-v2.old-*` 备份树省磁盘（实例目录可单独迁移，别把 GB 级
  备份带着走）。

## 8. 容器运行时（docker）的 agent

**本 playbook 对 docker agent 原样可用**：停机 → 换目录 → 改注册表
`source_version` → spawn_and_verify。区别只在最后一步——镜像 tag 含
source_version（`xuseek-agent-<id>:<version>`），tag 变化自动触发镜像重建
（含内核 selftest 门禁，比裸机多几分钟构建）。docker agent 跳过 §1 的
`.venv 平移`步骤也安全（venv 烘培在镜像里，实例目录没有 .venv）；
回滚同样只是改回旧版本号 + spawn（旧镜像还在，秒级起）。旧镜像清理交
`docker image prune`。详见 `docs/container-runtime.md`。
