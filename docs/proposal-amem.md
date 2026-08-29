# 建议引入「A-MEM 长期记忆能力」（给墟司的分工建议 · 第二版）

> 状态：**历史提案（本文已不再指导当前代码）**。2026-08-22 已实施并经裁决收缩；
> **v2 减法重构（2026-08-29）后相关代码路径全部移除**——xusi 与 agent 只剩管理
> 邮箱通道：不再渲染/保真 config.toml（仅创建时渲染一次）、不再只读观察
> `[capabilities]` 段、不再调 `xuseek.sh capabilities list` CLI、doctor 不再检查
> 能力包资产。能力包的种子、启用、依赖安装一律归 agent 自治（内核首启预检
> 无条件播种——v2.7.4 撤 init，serve/run 预检就是唯一引导点；启用与否、装不装
> 依赖是 agent 自己的事）。
> 本文保留仅作思路记录；契约文本见版本仓库 zip 内 `docs/capabilities.md`。

## 1. 背景：A-MEM 是什么，为什么值得引入

A-MEM（arXiv:2502.12110, NeurIPS 2025）是 Zettelkasten 式智能体长期记忆：写入时
LLM 生成结构化笔记（keywords/context/tags）并自动建链、演化旧记忆；检索时语义
top-k + 链接展开。`~/work/research/amem` 是可独立部署的复现，已内置 xuseek 适配
（LLM 优先走 `xuseek.llm.call_brain`，享受故障转移）。

实测（eval/locomo_conv26，57 问）：LLM 表示增强使 Recall@5 0.605 → 1.035（**+71%**）。

对墟寻的定位——补记忆分层的第三层，不与现有机制重叠：

| 层 | 机制 | 注入方式 |
|---|---|---|
| 工作记忆 | `workspace/BOOT.md` | 每次会话全文注入（机器保证） |
| 程序性记忆 | `workspace/playbook/` | 按需 read/grep |
| **情景/语义记忆** | **A-MEM（本提案）** | **大脑显式 search；机器永不自动注入** |

引入方式已裁决为**能力包播种**：内核随版本 zip 自带种子资产（库+技能脚本+指南），
无条件幂等播进 workspace；依赖（numpy/torch/sentence-transformers，~2GB）按实例
开关预装；**注册与使用归大脑**。检索不自动进系统提示——这条红线不碰，
"记忆=文件，全权归大脑"的内核原则原样。

## 2. 跨仓库分工（对齐 capabilities 契约）

一句话：**内核定义并自愈能力；管理面只做选择与观察；大脑决定用不用。**

| 仓库 | 职责 | 明确不做 |
|---|---|---|
| `research/amem` | ① tier 透传（记忆分析走 economy 档，不烧主池）；② 库级钉死嵌入模型（防中途换模型致余弦失真）；③ 打快照 tag 供内核取用 | 不碰内核/管理面仓库 |
| `research/xuseek-v2` | 能力包格式与资产、无条件幂等播种、`pyproject` extras、`xuseek.sh` 按指纹自愈安装 extras、CLI（`capabilities list`；`init --capability` 随 v2.7.4 撤 init 作废）、selftest、发版 | 不替大脑注册技能；不自动注入 pack 效果 |
| **`xusi`（本文）** | 见 §3：渲染保真、表单与开关、doctor、成本展示 | **不碰 pip**；不解析 pack 内容；不硬编码 pack 名 |

两仓之间的全部界面 = 三份冻结契约（详见 `capabilities.md` §2）：
manifest 格式、`config.toml [capabilities]` 段、两个公开 CLI。契约之外各自内部自由。

## 3. 墟司侧的具体改动

### 3.1 config 渲染保真（唯一修改义务，可先行，独立有益）

`brains.write_agent_config` 目前整文件重渲染 `config.toml`。能力开关的事实源是内核
所有的 `[capabilities]` 段（内核所有；v2.7.4 已撤 init，机器不代写 config），
**渲染器必须原样保真回传它不认识的段**——
否则 step 2 渲染即清掉 step 1 写入的开关。

这不只是为本提案服务：它保护的是未来一切内核段。建议无论本提案何时落地，
此修改先行合入：读旧 config → 保真未识别段 → 写回。

```toml
[capabilities]        # 内核所有；墟司只写它认识的段，其余原样回传
amem = true
```

### 3.2 创建表单：动态渲染能力开关

- 新建 agent 表单增加"能力"区：对**将要使用的那份源码副本**跑
  `xuseek.sh capabilities list --json`，按返回动态渲染复选框（名称/摘要/成本）。
  多版本 agent 并存时各自问各自的源码——**墟司零硬编码、无版本知识**。
- 创建流程只在既有 spawn 序列上透传一个参数：

```
1) 渲染 config.toml（创建时唯一一次）——原提案的 `init --capability` 已随
   v2.7.4 撤 init 作废；[capabilities] 开关由管理员自写或投信让 agent 自改
2)~5) 注册 / systemd 拉起 / 验收 / 签 token   # 其余不变
```

**依赖安装不需要墟司做任何事**：首次 serve 时 `xuseek.sh` 按双指纹自检补装
（主依赖硬失败、extras 软失败——extras 装不上只警告照常运行，自动重试/大脑自装）；
异机迁移重建 venv 同一自愈路径覆盖。

### 3.3 存量 agent：能力开关动作

WebUI agent 卡片（或 API）一个动作：

```
改 <home>/config.toml 的 [capabilities] 段（文件通道）
+ xuseek.sh --home <home> seed（公开 CLI：补播缺失种子，永不覆盖）
+ 重启（systemd 通道）——重启即触发依赖自愈安装
```

开启后大脑下个会话读 playbook 指南，自行决定是否 `register_skill` 启用——
**墟司不替大脑注册**（`data/skills.json` 是大脑的领地）。

粒度限制（明示，不为过渡形态做机制）：开关粒度 = venv 粒度 = 源码副本粒度。
"共享主源码"（source_version="main"）的旧 agent 共享 venv，开关不成立——
仅对版本化私有副本 agent 生效（新建缺省即是）。

### 3.4 doctor 检查 + 成本展示

- `xusi doctor` 增加：HF 镜像可达（`https://hf-mirror.com`，嵌入模型 470MB 首次
  下载走它）；指定版本 zip 是否含 capabilities 资产（版本仓库投放校验）。
- 成本表不自己维护——manifest 的 `[costs]` 原样透传展示（表单悬停/文档页）。

## 4. 成本模型（管理员开关 off→on 的决策依据）

| 项 | 量级 | 说明 |
|---|---|---|
| 磁盘 / agent | ~2 GB | venv 里 torch+sentence-transformers+numpy（CPU 版 torch 可减半；嵌入模型 470MB 走 HF 缓存，**同机多 agent 共享一份**） |
| 内存 / agent | ~0.5 GB 常驻 | 技能首次调用后模型留驻进程（内核已有进程级缓存）；不开则零开销 |
| LLM 调用 | 每条记忆 ≈2 次 | 写入时分析+演化判定（amem 前置项解决走 economy 档）；检索零 LLM 调用 |
| 依赖网络 | 首次 serve 时 | pip + HF 镜像；离线机建议预置 CPU torch |

注意：开关是**经济治理不是权限边界**——大脑有 run_shell 本就能自装依赖
（指南会教）；开关决定的是"预装与否"的成本缺省。

## 5. 实施顺序（每阶段独立可用）

1. **算法前置**（amem）：tier 透传 + 嵌入模型钉死 + tag 快照。
2. **内核落地**（xuseek-v2）：能力包框架 + amem pack 资产 + extras 自愈 + CLI
   + selftest，发版投放版本仓库。
3. **管理面**（xusi）：§3.1 渲染保真（可与 1、2 并行先行）→ 表单与开关 → doctor。
   先拿 1~2 个实验 agent 开启，观察记忆库生长、token 成本（call_brain 用量已在
   事件流，观察台可查）与检索命中，再议是否推广默认开启。

## 6. 开放问题

- **嵌入 API 化**（备选）：若 2GB/agent 普遍不可接受，amem 可改走 OpenAI 兼容
  `/embeddings`（密钥池多家支持），依赖缩到仅 numpy；代价是写入多一档 API 成本、
  嵌入质量受制供应商。建议先按本地模型试点，拿实跑数据再定。
- **默认开启时机**：等实验 agent 数据（条数/命中/成本）后再议。
- **多实例记忆共享**：不做（归属与写冲突问题，收益不明）；真需要时管理员给两个
  实例配同一远端，它们自己会想办法。
