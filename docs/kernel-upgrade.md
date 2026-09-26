# 存量 agent 内核升级实战（admin playbook）

> 沉淀自 2026-08-28 三连升级实验（v2.5.2 → v2.5.3 → v2.5.4 → v2.5.5：
> agent-0e50 两轮升级 + 4 个一次性验证 agent 建删全流程）。
> API 层「source_version 创建后不可改」约束的是**创建流程**；存量升级是目录级
> 操作，本文是标准做法。
> **当前目标版本：v2.7.98（2026-09-22 投放，xmem 挂载回 always 裁定版）**。
> 升级流程同 §1；语义变化见 §5 末节（v2.7.79 起）——**注意：管理面（xusi）与
> 内核须同批升级**（xusi v2.6.0 起投信/收信/会话索引全部走 facts.db，
> 旧 jsonl 通道已退役：旧内核 + 新 xusi 或新内核 + 旧 xusi 的邮箱会静默
> 断开）。升级顺序：先升管理面（本仓代码），再按 §7 批量升 agent。
> v2.7.80 = v2.7.79 的上游修复版（selftest 计数 off-by-one + entrypoint
> watch 双布局定位，见 docs/kernel-v279-upstream-feedback.md）；v2.7.79 的
> 临时修补包已从 versions/ 移除，本机存量若还在 v2.7.79 请按本文升到 v2.7.80。

## 0. 前置条件（顺序重要）

1. **git pull 管理面**到 `ca56645` 及之后——老代码把未标注脑分到 "premium" 档，
   与内核 v2.5.5 的 "power" 档不一致（只影响混合标注池的预算推导）。
2. **versions/ 放入内核新包**：`xuseek-v<版本>.zip`（统称 xuseek，前缀永久不变）（打包方法见
   [versions.md](versions.md)；包内根目录或唯一一级子目录两种布局都认）。
3. **etc/brains.toml 补齐每脑数据**：`tier`（power/economy）、`context_window`、
   economy 脑加 `note`（如"免费（自托管）"）。内核 v2.5.5 的**同档循环与按脑
   预检吃的就是这些数据**——不补则升级无收益（窗口未声明 = 不预检；tier 未
   标注全视同 power；行为与旧版相同，无害但白升）。

## 1. 升级操作（单 agent，停机约 1 分钟）

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)   # 坑②：systemctl --user 必需
cd <xusi 目录>
python3.12 - <<'EOF'        # 坑⑧：裸 python3 在 Ubuntu 22.04 远端是 3.10，
                            #   无 tomllib（xusi 需 ≥3.11）——用 deadsnakes 3.12
import sys, shutil
sys.path.insert(0, ".")
from xusi import registry, agentops, versions
from xusi.config import get_config

AID, NEW = "agent-XXXX", "v2.7.4"          # ← 只改这两处
agent = registry.get_agent(AID)
OLD = agent["source_version"]
home = get_config().instance_home(AID)

# 0) 载体操作前通知（管理面对 agent 的承诺——2026-09-11 d093 无声灭审计定案后补）：
#    升级/重启对大脑就是「无声灭」（服务全灭、无预警）——投信让它事后归因
#    「断窗是管理面操作，无需排查」，会话中收到还能在停的宽限期里优雅收尾。
agentops.mail(AID, f"【管理面】内核升级 {OLD} → {NEW} 即将开始：载体会停止重建，"
                   f"你的常驻服务将短暂断窗，重建后自行恢复——此断窗为管理面操作，无需排查。")
agentops.stop(AID)                          # 1) 优雅停（agentops.stop：冻结进程先
                                            #    SIGCONT 解救再停，不裸调 systemdctl；
                                            #    desired_state 落 stopped，中途死不
                                            #    谎报，reconcile 不从半迁移目录乱拉）
src, tmp = versions.kernel_dir(home), home / "xuseek.new"   # 坑⑨：src 必须走
                                            # kernel_dir（兼容旧名 xuseek-v2 的存量
                                            # 实例——东京实案：直接用 SRC_DIR_NAME 时
                                            # rename 报 FileNotFoundError，卡在半迁移）
bak = home / f"{versions.SRC_DIR_NAME}.old-{OLD}"
for d in (tmp, bak):
    if d.exists(): shutil.rmtree(d)
versions.extract(NEW, tmp)                  # 2) 官方解压器（坑①：勿用裸 unzip）
_venv = src / ".venv"
if _venv.is_dir() and not _venv.is_symlink():
    shutil.move(str(_venv), str(tmp / ".venv"))   #   .venv 平移免重建（坑④）——v2.7.38 起两种
                                                  #   运行时都是实例目录里的真目录，平移通用；
                                                  #   软链 = v2.7.37 及更早的兼容层遗留，跳过
                                                  #   （新树首启自建，属预期的「重装一次环境」）
src.rename(bak); tmp.rename(src)            #   旧树留作回滚
registry.update_agent(AID, {"source_version": NEW})   # 3) 坑③：API 改不了这字段
agentops.audit("upgrade_kernel", agent=AID, **{"from": OLD, "to": NEW})
agentops.spawn_and_verify(registry.get_agent(AID))    # 4) 拉起 + 健康验收
agentops.start(AID)                       # 5) 坑⑥：agentops.stop 把 desired_state 落
                                          #    成了 stopped，spawn_and_verify 不回写——
                                          #    active 时 start() 只做 finalize（不重拉），
                                          #    恢复期望态 running；漏掉这步，下次
                                          #    manager 重启时 reconcile 会把已升级的
                                          #    agent 停掉
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
| ④ | 担心 `.venv` 要重建 | 平移即可（`mv` 进新树，路径不变）——v2.7.38 起两种运行时都是实例目录里的真目录，平移通用；依赖没变指纹不漂移不重装，变了 xuseek.sh 按指纹自愈补装（有 uv 用 uv，失败回落 pip）；干脆不平移也行：新树首启自建，只是多装一次（实测 6~51s） |
| ⑤ | 担心停单元时 manager 抢拉 | 不会——reconcile 只在 manager 重启时跑，手动操作窗口安全 |
| ⑥ | 升级收尾后 desired_state 停在 stopped（agentops.stop 落盘、spawn_and_verify 不回写）→ 下次 manager 重启 reconcile 按期望态把已升级的 agent 停掉 | §1 脚本收尾补 `agentops.start(AID)`（active 时只 finalize、不重拉） |
| ⑧ | 内核目录改名（2026-09-26 xuseek-v2→xuseek）后：**实例自有脚本的硬编码旧路径**（ctl.sh 锚点/自愈脚本/BOOT 指令里的 `/data/xuseek-v2`）与**常驻服务未恢复**（收信口 8409 类断连，邻居报 >9 分钟拒连）——72ad 实案：ctl 锚点 rc=127、收信服务靠管理员投信才恢复；47db/8a43 同款残留已投信自修 | 升级/改名后**逐实例检查**：grep 实例 home 的 ctl.sh/workspace 里 `xuseek-v2` 残留；pidfile vs 进程对账（`kill -0`）；有发现投信让 agent 自修（72ad 已把锚点改成 VIRTUAL_ENV→python3→/data/xuseek→/app 探测式，可作范本） |
| ⑩ | bare 运行时升级（AutoDL 等无 systemd）：agent 用自己 ctl 起的大服务（vllm 类）挂在 setsid 监督器进程组里，`agentops.stop` 收组时**连带杀死**——若是舰队脑端点（audl-4080 实案：6018 断供 12 分钟），其他智能体的大脑调用当场缺供 | 升级后**立即探活对外口**（curl 该 agent 声明的服务端口）；死了立刻投信 agent（它按自己 playbook 的 ctl 配方恢复，冷加载 vllm 要 ~10 分钟——先投信再兜底）；其恢复命令在 `workspace/playbook/llm-服务运维.md` 类文件里，兜底代跑也要走**它自己的 ctl**（保看门狗/pidfile 模式） |
| ⑨ | §1 脚本的 src 直接取 `versions.SRC_DIR_NAME` → 存量实例还是旧目录名时 rename 报 FileNotFoundError、**卡在半迁移**（服务已停、新树已解包、venv 没平移）——tx-tokyo-1 两实例实案 | src 走 `versions.kernel_dir(home)`（新名优先、旧名兼容）；已在半迁移态时手工收尾：wrapper → 新树改名 → venv 平移 → 旧树备份 → spawn |
| ⑦ | docker compose build 永久挂死、零输出零事件（tx-bj-3 实案）：buildx bake 冷路径解析基础镜像 manifest/attestation 时**不走 daemon.json 的 registry-mirrors**，直连 registry-1.docker.io 在墙内静默挂死且 bake 无超时；杀客户端还会楔死 dockerd 内置 buildkit 控制器（后续构建排队不启动，重启 dockerd 才解） | 已内建修复（dockerctl 构建前解析 Dockerfile FROM 行逐一 `docker pull` 走镜像站预热，失败静默）。手工应急：先 `docker pull <基础镜像>` 把元数据拉齐，构建即秒级；已楔死则 `sudo systemctl restart docker` |

## 4. 验证清单（升级后 5 分钟）

- [ ] journal 启动横幅：`大脑：X（故障转移兜底: …）`——兜底名单应是**同档**脑，不是全池
- [ ] `config.toml`：`[brains.X]` 带 `tier` / `context_window`；
      旧 `[agent]` 预算段已清掉（v2.7.5 不再认——max_context_tokens 改自动派生、
      max_seconds 删除；轮数限额只剩 `[limits] max_rounds`）
- [ ] 事件流 `llm_response.brain` 正常粘滞、无 `llm_error` / `llm_retry` 风暴（观察 1~2 个会话）
- [ ] **实例自有脚本无旧路径残留 + 常驻服务已恢复**：grep 实例 home（ctl.sh/自愈脚本/
      BOOT 指令）无 `/data/xuseek-v2` 硬编码；`data/*.pid` 与进程 `kill -0` 对账全活；
      对外口（收信/数据服务）实测可达——死口邻居视角 9 分钟起步才报（见坑⑧）

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

### 内核 v2.7.79（2026-09-20）：facts.db 事实账时代（管理面 v2.6.0 同步收敛）

- **事实账取代 jsonl**：mail/outbox/会话索引全部落 `data/facts.db`（SQLite
  WAL，行号=rowid，只增不减）——`mailbox.jsonl`/`outbox.jsonl`/
  `sessions.jsonl`/`offsets.json` 全部退役。管理面投信/收信/会话列表走同一
  账本；升级后旧 jsonl 文件留在 data/ 里是死数据，可让 agent 清掉。
- **呼吸面看门狗**：`[limits] session_stall_s` 键退役——判活的眼睛长在壳里
  （docker：入口 shim 的 stall_watch，`XUSEEK_STALL_S` env；systemd：管理面
  每 5 分钟瞬态 timer 跑内核 stall_check.py，停滞即重启单元）。升级后 config
  残留该键会 stderr 大声提醒，投信让 agent 删掉。
- **`[capabilities]` 段退役**：amem 资产（packs/amem）无条件播种、重依赖
  归大脑/管理员自装（配方 playbook/依赖安装.md）；`[retention]` 段退役
  （会话存档不代删，超软阈值只记 sessions_over_cap 事实）。
- **升级后投信模板**（让 agent 自己清理死配置）：

```python
from xusi import agentops
agentops.mail(AID, "内核已升级至 v2.7.79：① 请删除 config.toml 里的 "
                   "[limits] session_stall_s、[retention]、[capabilities] 段"
                   "（均已退役，留着只会收到启动提醒）；② data/ 下旧的 "
                   "mailbox.jsonl / mailbox_log.jsonl / outbox.jsonl / "
                   "sessions.jsonl 是退役通道的死数据，信箱与会话索引现在都在 "
                   "data/facts.db 里，确认无误后可自行清理。")
```

### 内核 v2.7.98（2026-09-22）：[xmem] mount 选项裁撤——enabled=true 恒起手全挂

- **`[xmem] mount`（demand|always）整体移除**：v2.7.96/97 的按需挂载状态机
  （起手只挂一行声明、首调才挂完整 schema、闲置 8 轮摘回）裁撤，回 v2.7.95
  行为——`enabled=true` 会话起手五件工具全挂。config 残留 `mount` 键被内核
  静默忽略（死配置），建议让 agent 删掉；管理面 `xusi patch --xmem on|off`
  整块重渲染时会顺带清掉旧块里的 mount 行。
- **管理面同步（xusi 本提交）**：create/patch/API/WebUI 的 `--xmem-mount`
  选项全部移除（2.7.96/97 内核无 mount 键 = 缺省 demand，与旧缺省渲染同义，
  向后兼容；仅 v2.7.96/97 的 always 档不可再选——全队升 2.7.98 后 moot）。

### 内核 v2.7.39–v2.7.41（2026-09-12）

- **`[[services]]` 段退役（v2.7.40）**：常驻服务的判活从 daemon 移到**启动壳看门狗**
  （壳 `wait` 服务进程、死即按铃——壳不睡觉，见种子 `常驻服务.md`）；daemon 不再
  读此段。升级后 config 残留 `[[services]]` 会 stderr **大声提醒**（不静默：静默
  会让大脑误以为还有人看着）但**不再有人守护**——须投信让 agent 把常驻服务改经
  ctl 壳启动并删除该段（2026-09-12 agent-f3c2 实测：config 无此段，零影响）。
- **冻结/长假可见化**：休眠期 SIGSTOP 冻结或唤醒点已过 ≥60s 才醒，status 记
  `last_freeze_gap_s` + stderr 一行——缺口可见，不是假安全感（parked 驻留无唤醒
  点、冻结不可见，接受）。
- **软链式 venv 修复**：环境 bin 探测锚 `sys.prefix` 而非 `resolve(sys.executable)`
  ——`python3 -m venv` 软链式 venv 下 resolve 展开软链到 /usr/bin，venv 探测落空、
  PATH 前置失效（VIRTUAL_ENV 恒空血训）；selftest 有回归线。
- **种子第八主题**：`常驻服务.md`（ctl 壳启动协议 / 看门狗 / 静音停机）。

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
systemctl --user stop <unit>          # docker 实例改用 `python -m xusi stop <id>`
mv instances/<id>/xuseek-v2/.venv instances/<id>/xuseek-v2.old-<旧版>/.venv
mv instances/<id>/xuseek-v2 instances/<id>/xuseek-v2.failed
mv instances/<id>/xuseek-v2.old-<旧版> instances/<id>/xuseek-v2
# 再 registry.update_agent 改回旧版本号 + spawn_and_verify（同 §1 脚本尾段）
```

回滚对两种运行时同样直接：`.venv` 是真目录（v2.7.38 起），`mv` 回旧树即用；
docker 实例**不涉及镜像**——镜像 fleet 共享（`xuseek:<version>`）且与实例
内容解耦，回滚只是换内核副本目录。

## 7. 批量升级建议

- 先升 1 个 agent 观察半天，再批量（§1 脚本循环 AID 列表，各自停机 ~1 分钟）。
- 一次性 / 实验 agent 直接**删除重建**更省事：新建缺省即取 versions/ 最新版。
- 稳定后删掉 `xuseek-v2.old-*` 备份树省磁盘（实例目录可单独迁移，别把 GB 级
  备份带着走）。

## 8. 容器运行时（docker）的 agent

**本 playbook 对 docker agent 原样可用**：停机 → 换目录 → 改注册表
`source_version` → spawn_and_verify。**镜像基本不动**——v2.7.38 入口 shim 起，
跑的是实例目录自己的 launcher/源码/`.venv`；镜像 fleet 共享
`xuseek:<version>`（只是运行环境 + 兜底副本），升到新版本时全队只构建一次
（第一个升的实例触发，含内核 selftest 门禁，其余复用），「忘重建镜像」
事故类从结构上消失。§1 的 `.venv 平移`对 docker 实例同样适用（v2.7.38 起
也是真目录；旧版遗留的兼容软链会被守卫跳过、新树首启自建）。首启如遇
解释器/依赖差异，venv 自愈原地重建或补装（分钟级，一次性）。回滚同 §6：
换回旧树 + spawn，镜像无关、秒级起。镜像重建仅当换基础镜像（python 版本）
或系统依赖变化；旧镜像清理交 `docker image prune`。详见
`docs/container-runtime.md`。
