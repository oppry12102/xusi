# 内核统一更新建议稿 v2.9.41 —— 最终版

**基线**：~/work/xuseek2/xuseek2 @ v2.9.40（正典仓）
**性质**：**最终修改建议**——主人令统一更新，20ca 组参考执行；后续内核研究亦请以「简单、解耦、本质」三原则审视方案
**来源**：opt.1-4 成果吸收 + 零信息防线 4 件 + 三方审核 + 对照实验

---

## 〇、设计总纲（后续研究请沿用）

> **简单**：代码最少、状态量最少、无模糊匹配
> **解耦**：各层互不需要对方存在——内核不依赖网关、网关不依赖身份
> **本质**：问「这个文件/机制该不该存在」，而不只问「怎么修好它」；安全/卫生策略优先下沉到运行环境层（Docker/entrypoint/环境变量），代码零改动
> **能让 LLM 做的让 LLM 做**：代码只陈述机械事实，判断归 LLM

---

## 一、基线变更（opt.1-4 的本质化重写）

### 1.1 替代 opt.1 + opt.2（权限/umask → 环境层）

**Dockerfile** 追加：
```dockerfile
ENV PYTHONDONTWRITEBYTECODE=1
```

**docker-entrypoint.sh** 首行（shebang 后）追加：
```bash
umask 077
```

**替代**：opt.1 的全树 chmod（部署时一次性操作，见 §1.4 存量处理）＋ opt.2 的 4 行 Python（cli.py × 3 + agent.py × 1）＋ **opt.3 的 200 行自定义 loader**。

**理由**：
- `umask 077` 在进程启动即生效——所有后续写文件（breath.json、session 存档、skills 写出）自动 600，无需逐文件 chmod
- `PYTHONDONTWRITEBYTECODE=1` 让 Python 不产 .pyc——「审 A 跑 B」的 PyCache Trap **从根上不存在**，不需要 _reject_bytecode / _xuseek_source_loader / sys.modules 窗口
- 两行改一处（环境层），代码零改动，内核源码保持 stdlib 纯净

### 1.2 替代 opt.4（breath.json → 删，读 facts.db）

**变更**：
- `daemon.py`：删除 `_write_breath`/`_clear_breath`（及其在 `_begin_session`/`_end_session` 的调用点）
- `api/app.py`：`_derived_status` 中 breath.json 读取段替换为：
```python
        # 呼吸中 = facts 最后一条 session_start 晚于最后一条 session_end
        # （原子性由 SQLite 事务保证——不再有独立 breath.json 半写问题）
        last_start = facts.tail(1, types=["session_start"])
        last_end = facts.tail(1, types=["session_end"])
        if last_start and (not last_end or last_start[0]["at"] > last_end[0]["at"]):
            out["state"] = "running_session"
            out["current_session"] = last_start[0].get("session_id")
            return out
```

**理由**：
- breath.json 存的信息（session_id, at）**在 facts.db 里本来就有**（session_start 行）——独立文件是冗余通道
- SQLite 事务保证原子性——**半写问题从文件层面消失**，不需要 tmp+os.replace、不需要读端 try/except
- 删代码（~30 行）而非加代码（~40 行修补）——**减少一个状态源**

**兼容**：观察台 `/v1/status` 返回结构不变（state/current_session 语义同）。

### 1.3 存量处理（一次性）

各实例首次以 v2.9.41 启动时，entrypoint 的 umask 自动生效于新写文件。存量文件权限不一致无功能影响（属主 uid 可读写），不做批量迁移（简单原则：不为卫生问题引入复杂迁移脚本）。opt.1 的 chmod 脚本保留在 releases/ 供手动使用，不入内核源码。

### 1.4 不采纳的 opt 组件

| 组件 | 理由 |
|---|---|
| opt.3 全部（~200 行自定义 loader） | 环境变量替代后问题不存在 |
| opt.2 的 4 行 Python | entrypoint umask 替代 |
| opt.4 的 tmp+replace+读端兜底 | 整个 breath.json 删除后问题不存在 |

---

## 二、零信息防线 4 件（原建议稿保留，细节见 `docs/kernel-zero-info-guard-proposal-v1.md`）

### 2.1 同命令三次拒绝
- **插桩**：`agent.py` 会话循环 `_exec(tc)` 顶部
- **状态**：`_last_cmd_fp: str`（上一条命令指纹）＋ `_cmd_repeat: int`——会话级局部变量
- **动作**：连续第 3 次同一命令 → 不执行，返回自解释 error（见原稿全文）→ `tool_repeat_reject` 事实
- **实验**：glm-5.3-flash N=20 锁定态，3/3 立即改道

### 2.2 同输出递进防线
- **插桩**：`agent.py` 纯文本轮判定处（`text_round_noticed` 旁）
- **状态**：`_last_out_sha: str` ＋ `_out_repeat: int`
- **动作**：第 2 次相同 → 注入【本机提示】（用户角色打断，实验 C 3/3 有效）；第 3 次 → `end_session(stop_reason="repeat_output_guard")`

### 2.3 网关错误码映射
- **插桩**：`llm.py` `FailoverBrain.chat` 重试分类处
- **动作**：HTTP 4xx 且响应体含 `ctx_loop_reject` 或 `ctx_fuse_reject` → 不重试 → 抛 `GatewayCtxReject` → `agent.py` catch → `end_session(stop_reason="gateway_ctx_reject")`
- **理由**：11-22 实弹——网关拒 400+ 发，调用方继续硬撞（n=51+）；无映射则网关断成本、内核死循环不死

### 2.4 X-Ctx-Warn 响应头消费
- **插桩**：`llm.py` 响应处理处
- **动作**：`X-Ctx-Warn` 头存在 → `log({"event": "ctx_warn", "warn": 值})`——纯观测不判定

---

## 三、总变更量

| 类别 | 文件 | 增/删 | 行数估计 |
|---|---|---|---|
| 环境层 | Dockerfile | +1 行 | 1 |
| 环境层 | docker-entrypoint.sh | +1 行 | 1 |
| 删 breath | daemon.py | −~15 行 | 15 |
| 删 breath | api/app.py | −~15 / +8 行 | 23 |
| 零信息 | agent.py | +~35 行 | 35 |
| 零信息 | llm.py | +~25 行 | 25 |
| 自检 | selftest.py | +~20 行 | 20 |
| **合计** | | **净增约 +80 行** | |

（不含 opt.3 的 ~200 行、opt.2 的 4 行、opt.4 的 ~40 行——全部被替代/删除）

---

## 四、测试验收

| # | 断言 | 预期 |
|---|---|---|
| 1 | 同命令第 3 次被拒（前 2 放行） | error ＋ tool_repeat_reject 事实 |
| 2 | 被拒后模型改道（真脑） | 换方法或 end_session（实验 3/3） |
| 3 | est 不再爬升 | 桩循环下平顶 |
| 4 | 连续性重置（check→他事→check） | 零拒绝 |
| 5 | 跨会话清零 | 零拒绝 |
| 6 | 同输出第 2 次提示→第 3 次终止 | 【本机提示】→ end_session |
| 7 | 网关 4xx ctx_* → 不重试＋终止 | GatewayCtxReject → end_session |
| 8 | X-Ctx-Warn 头→ctx_warn 事实 | 观测到位 |
| 9 | /v1/status 语义不变 | state=running_session 时 current_session 正确 |
| 10 | PYTHONDONTWRITEBYTECODE 生效 | 无 .pyc 产生 |
| 11 | umask 077 生效 | 新写文件 600 |
| 12 | 全量 selftest | 全绿 |

---

## 五、发布路径

1. **在 ~/work/xuseek2/xuseek2 正典仓上直接修改**（主人令）
2. 20ca 组验证（测试台三断言 + 12 项验收 + e2e 全链）
3. 打 **v2.9.41**，git archive 出包（注释=发版 commit，惯例）
4. 主人定档后管理面推送（范围含不含股票舰队由主人明示）
5. fad0/c150 在 v2.9.41 普及后撤网关侧过渡件（阶段二）

---

## 六、对内核组后续研究的三原则提示

本稿在审 opt.1-4 时发现：**四个补丁都在修补症状，都存在更本质的做法**（权限→Docker 层 umask、字节码→环境变量、breath.json→删冗余通道读 facts.db）。建议后续内核研究先问三层：

1. **这个文件/机制该不该存在？**（如 breath.json 是 facts.db 的冗余）
2. **这个策略能不能放到环境层？**（如 umask/PYTHONDONTWRITEBYTECODE 在 Docker 一行替代代码 200 行）
3. **代码只做代码擅长的事（陈述事实、执行操作），判断交给 LLM**

—— 管理员 2026-11-22 · 最终建议 · 请 20ca 组长参考执行
