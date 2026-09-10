# 实例生命周期实操建议：创建 / 迁移 / 升级 / 复原 / 组网

> 状态：建议（待 xusi 评审采纳）· 2026-09-10 · 依据 = v2.7.38 内核工作树（入口 shim 方案）
> 上的三轮真机测试：① amem 破坏复原（e2e-amem）② 跨运行时迁移四阶段矩阵（e2e-migrate）
> ③ 双容器智能体模糊指令协作 + 门铃唤醒（e2e-p1/p2 + e2e-root-dir）。
> 设计底稿：内核仓 `docs/proposal-container-native-venv.md`（v2.7.38，含三次评审记录）。
> 本文取代 `proposal-container-native-venv.md`（xusi 侧）中「VENV 落点改写」方案——内核
> 评审已转向入口 shim，xusi 的配合事项以本文为准。

## 0. 前提：v2.7.38 改了什么（xusi 视角一句话版）

容器入口从「跑镜像里的 /app/xuseek.sh」改为 **shim 优先跑实例目录里自己的 xuseek.sh**
（`docker-entrypoint.sh`，探 `$XUSEEK_HOME/xuseek.sh` 与 `$XUSEEK_HOME/xuseek-v2/xuseek.sh`）。
于是：launcher、内核源码、`.venv`（首启自建、bind mount 持久）**全随实例目录**；镜像降级为
纯运行环境（python/uv/系统工具 + 兜底副本）。pip 回到完全缺省语义（装进 venv、pip list/
uninstall 正常），PIP_TARGET/PYTHONPATH 世界终结；内核升级**不再需要重建镜像**。

## 1. 创建

**compose 模板改动**（`dockerctl.py _render_compose`，一笔）：

| 项 | 现状 | 改为 | 依据 |
|---|---|---|---|
| `PIP_TARGET`/`PYTHONPATH` env | `/data/.local/site-packages` | **删** | venv 即世界；pip 缺省语义（测试①②全链验证） |
| `./xuseek:/app/xuseek` 挂载 | 有 | **删** | 运行的已是 /data 这份，挂载冗余 |
| `HOME: /data` | 有 | **保留** | uv/pip 缓存落 bind mount（`/data/.cache/uv`），独立价值 |
| `start_period` | 30s | **300s** | 首启自建 venv 现装依赖；`spawn_and_verify` 超时同步放宽。实测：镜像带 uv + 清华源时 6~51s，窗口给足 |
| `APT_MIRROR` 构建参数 | 可选 | **默认腾讯源** | 实测 apt 直连 deb.debian.org ~40KB/s（35MB 装了 16 分钟没完）；腾讯源后整镜像构建数分钟 |
| `UV_DEFAULT_INDEX`/`PIP_INDEX_URL` env | 无 | **加**（清华） | 实例首启自愈装依赖走镜像源 |
| `XUSEEK_EXTRAS` 烘培 | 渲染 `docker_extras` | 可留可删 | 运行时 extras 由实例按自己 config 现装，烘培只剩构建期验证+兜底价值 |

**镜像策略：fleet 共享一个 tag 足矣。** 镜像已与实例内容解耦（跑的是实例的 launcher），
同一内核版本 N 个实例引用同一 `xuseek:<ver>` 即可；按实例建镜像的旧习惯可以退役
（测试③双实例共享 `xuseek:e2e-pair`，构建一次）。重建镜像只发生在换基础镜像/系统依赖时。

**工件预置（amem 实例强烈建议）**：`workspace/models/minilm-onnx/`（465MB）与引擎版本无关、
与运行时无关（实测 3.12/3.13 双向迁移向量指纹守卫直接放行、零重嵌入）——从金种子
（如 agent-09b6 的副本）预拷进新实例，省一次 450MB 下载；venv 则**不要预建**（自愈自管）。

**uid 对齐是迁移无痛的前提**：容器运行用户 uid 必须与宿主管理用户一致（现 user 1000 即对）。
root 容器的属主不对称已实测踩坑（见 §2），对齐后双向迁移零处理。

## 2. 迁移

**同一台机换载体**（systemd ↔ docker）：直接切，不需要任何预处理——两个运行时用同一条
`.venv` 路径；解释器失配（宿主 /usr/bin/python3.12 ↔ 镜像 /usr/local/bin/python3.13）由
首启自愈识别并原地重建。实测：容器方向 6.1s、宿主方向 16.2s（含全量依赖重装）；指纹
确定性往返（依赖哈希逐字节相同，仅 python 前缀换）；amem 数据文件与向量零损伤。

**换机**：`rsync -a --exclude .venv 实例目录/ 新机:…/`，首启自愈重建环境（29s 级）。
`models/`、`data/`、`workspace/`、`config.toml`、内核副本全带走；`.venv` 是唯一可丢弃物
（它就是「环境的持久化缓存」，丢了只是重装一次）。

**属主不对称（只在 root 容器场景，uid 对齐则无此问题）**：root 容器写出的文件
（`.venv/`、被容器改写过的数据文件如 amem notes.jsonl）宿主普通用户动不了。venv 侧
v2.7.38 启动器已带指引报错（`sudo rm -rf … 或在容器内删除后再迁回`）；数据文件侧症状是
各处 PermissionError，一次性 `sudo chown -R $USER 实例目录` 即齐。测试④实录：智能体自己
`ls` 诊断 + `sudo chown` 自救并往 playbook 写了四段式经验条目——但**不要依赖**智能体有
免密 sudo，uid 对齐才是正解。

## 3. 升级

**内核升级 = 更新实例的内核副本 + restart**，镜像不动（launcher/源码/venv 都随实例）。
「忘重建镜像」这一整类事故从结构上消失。venv 跨内核升级存活：依赖变了自愈补装（指纹
幂等）、python 没变则秒级。回滚 = 内核副本退版本 + restart。
镜像重建仅当：换基础镜像（python 版本）、系统依赖变化——此时实例 venv 因 python 小版本
变会自动重装（正确行为，无需干预）。

## 4. 破坏复原（运维速查，全部实测）

| 场景 | 行为 | 实测 |
|---|---|---|
| `rm -rf .venv`（全毁） | 下次启动自愈重建+现装，数据零损伤 | 17~51s 恢复，notes.jsonl md5 不变 |
| venv 半残（删 `bin/pip`） | 自愈判据**点名 `bin/pip`**（pip3 还在也骗不过）→ 重建 | ✓ |
| 实例 launcher 被删 | shim 回落镜像副本 + stderr 提示；智能体可从 `/app` 拷回自修 | 兜底通路在 |
| amem 引擎工件坏 | 带指引 RuntimeError（不裸炸不静默回落） | v2.7.37 ⑧ |
| 安装失败（断网） | exit 1 → 容器崩溃循环自愈重试（restart: unless-stopped） | 幂等，来网即愈 |

**监控证据面**：容器 healthcheck（/v1/health）判活性；`data/doorbell_log.jsonl`（全史）
与 `data/sessions.jsonl` 判行为。坑：**会话文件只在会话结束时落盘**——判断「醒没醒/在不在
干活」别只看 sessions 文件，配合 daemon 日志（`docker compose logs`）。

## 5. 组网（可选，实测全通）

config `[[roots]]`（address+token）是**唯一**需要管理面注入的东西——两个互不知晓的容器
智能体在纯模糊指令（「请联络另一个智能体协作研究，办法自己想」）下，约 3 分钟各自按
playbook 完成全链：建带门铃收信节点（25 行配方，收信即 `doorbell.ring`）→ whoami 定址 →
登记验证 → 查目录互认 → 真实协作（课题文档 v0.1→v0.2、token 轮换约定、联系人簿）。
门铃唤醒实测：**铃→会话启动 5 秒**（睡梦中），书信节奏最快 8 秒/来回；开场白逐字注入
「本机提示：门铃响了——…」。

- **根目录服务**：fleet 可以共一个（测试实现 60 行 stdlib 在内核侧
  `/home/ubuntu/work/e2e-root-dir/root_directory.py`：自述契约 + 验址登记 + token 查询 +
  whoami 回报 seen_from，可直接采纳加固）。根 token 走 config [[roots]] 下发。
- 端口：智能体自己 ss 探测选端口（实测自然落在 8401/8402）；host 网络下管理面只需规划
  观察台端口段与自起服务端口段不冲突。
- 成本参考：组网+初期协作 ≈ ¥5/对。

## 6. xusi 改动清单（一笔收尾，硬约束：内核 v2.7.38 先发版）

1. `dockerctl.py` compose 模板：§1 表格七项；
2. `patch_agent` 的「systemd→docker 删宿主 venv」逻辑（c65dd09）**退役**——新世界同一条
   venv 路径自愈重建，「删」从保护变成破坏；
3. `spawn_and_verify` 验收超时放宽（分钟级首启）；
4. docs：`container-runtime.md`（「实例目录没有 .venv」等段落按新世界重写）、
   `kernel-upgrade.md`（删镜像重建步骤、补 `.venv` 平移新语义）、`versions.md` 视情况；
   `proposal-container-native-venv.md` 标注被本文取代；
5. 存量实例按新世界**重建**（用户已拍板不做兼容迁移）：新实例目录干净无
   `.local` 暗物质残留；重建时预拷 onnx 工件（§1）。

反序风险照旧：先撤 env 而镜像未含 shim → launcher 跑 /app 副本、pip 落回只读
`/app/.venv`，错误风暴。内核先行是唯一安全序。

## 7. 不做的

- 不给 PIP_TARGET 过渡钩子（内核从未认识它；交接顺序保证不并存）；
- 不预建/预管实例 `.venv`（自愈的地盘，外部一动就遮蔽判据）;
- 不默认烘培 `XUSEEK_EXTRAS`（收益已消失，实例按 config 现装）；
- 不替 agent 清理旧 `workspace/.local`（投信引导；重建实例则自然消失）；
- 不给门铃/收信节点做管理面机制（playbook 配方已足够，测试③零机制验证）。
