# 存量 agent 内核升级实战（admin playbook）

> 沉淀自 2026-08-28 三连升级实验（v2.5.2 → v2.5.3 → v2.5.4 → v2.5.5：
> agent-0e50 两轮升级 + 4 个一次性验证 agent 建删全流程）。
> API 层「source_version 创建后不可改」约束的是**创建流程**；存量升级是目录级
> 操作，本文是标准做法。前置：管理面代码 ≥ `ca56645`（分档语义与内核 v2.5.5
> 对齐：未标注 tier 视同 power）。

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
from xusi import systemdctl, registry, agentops, versions
from xusi.config import get_config

AID, NEW = "agent-XXXX", "v2.5.5"          # ← 只改这两处
agent = registry.get_agent(AID)
OLD = agent["source_version"]
home, unit = get_config().instance_home(AID), get_config().unit_name(AID)

systemdctl.stop(unit)                       # 1) 停（优雅停窗 20s，轮边界落盘）
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

## 2. 升级后必做：PATCH 触发重渲染

**内核升级不会重写 config.toml**。存量 agent 需要一次任意 PATCH 把
tier / context_window / 预算 / note 渲染进去（热重载，下一口呼吸生效）：

```python
from xusi import agentops, registry
a = registry.get_agent(AID)
agentops.patch_agent(AID, {"brains": a["brains"]})   # 原样写回即触发重渲染
```

## 3. 踩过的坑（每条都真实发生过）

| # | 坑 | 正解 |
|---|---|---|
| ① | 用 `zipfile.extractall` / 裸 unzip 解内核包 → `xuseek.sh` 644 → systemd 报 `Permission denied` 拉不起 | 一律走 `versions.extract`：还原权限位、无条件保证 `xuseek.sh` 可执行、防 zip-slip |
| ② | 后台脚本调 `systemctl --user` 全部 `not-found` | 先 `export XDG_RUNTIME_DIR=/run/user/$(id -u)` |
| ③ | PATCH 改 `source_version` 被拒（`_PATCHABLE` 白名单不含它） | `registry.update_agent()` 直改；不改则 webui/备份恢复显示错版本 |
| ④ | 担心 `.venv` 要重建 | 平移即可（`mv` 进新树，路径不变）；v2.5.x 无新增依赖，未来加了 xuseek.sh 也会自愈补装 |
| ⑤ | 担心停单元时 manager 抢拉 | 不会——reconcile 只在 manager 重启时跑，手动操作窗口安全 |

## 4. 验证清单（升级后 5 分钟）

- [ ] `/v1/status` 的 `version` = 新版本号
- [ ] journal 启动横幅：`大脑：X（故障转移兜底: …）`——兜底名单应是**同档**脑，不是全池
- [ ] `config.toml`：`[brains.X]` 带 `tier` / `context_window`；
      `[agent] max_context_tokens` = default 同档已声明窗口最小值 − 8192
- [ ] 事件流 `llm_response.brain` 正常粘滞、无 `llm_error` / `llm_retry` 风暴（观察 1~2 个会话）

## 5. 语义变化提醒（内核 v2.5.5）

- **故障转移同档循环**：跨档不再自动兜底；档内全灭的错误会提示其它档（跨档走
  管理员 PATCH 换 default）。
- **未标注 tier 视同 power**：全未标注的存量池行为一字不差；「已标注 + 未标注」
  混合池里，未标注脑会加入 power 轮转。
- **发送前按 context_window 预检**（≥2 脑的池跳过装不下的脑）；单脑池无预检，
  400 自然报错。
- **playbook 种子只补缺**：存量 agent 的 `llm-调用.md` 不会自动更新（归大脑
  所有）；要告知智能体新档位语义走 send_mail。
- 会话内预算恒定的不变量成立：预算 = 同档最小 − 8k，任何中途换脑都不会把
  可用窗口换小。

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
- 一次性 / 实验 agent 直接**删除重建**更省事：新建缺省即取 versions/ 最新版，
- 稳定后删掉 `xuseek-v2.old-*` 备份树省磁盘（实例目录可单独迁移，别把 GB 级
  备份带着走）。
