# 特例归一、保护归位——v2.9.43 审查 15 发现的收尾方案（内核修改建议）

> 状态：**建议（待评审）** · 2026-11-22 · 源头：提交 `bb8769e`（v2.9.43）代码审查 15 条确认发现
> 本文回答三件事：①为什么 15 条无一需要新机制；②每条归到什么位置、并进哪条路径；③哪些取舍是明知的。
> 三轮轨迹：v2.9.41（机制交互伤）→ v2.9.42（3 个真回归）→ v2.9.43（核心干净，余为历史遗留+测试质量）——这是收尾轮，不是再设计轮。

## 1. 诊断：15 条全是两类旧伤——「特例」与「错位」

| 类 | 发现 | 一句话 |
|---|---|---|
| 特例：同一失败两条处理路径 | #1 #6 | 序列化 except 自开日志特例，原始 result 漏给下游 `.get()`——非 dict 崩会话、dict 双记账 |
| 错位：保护放错地方 | #2 #3 #5 #10 | 凭证保险库/登记队列裸写；存量钥匙不补 mode；token 钉测的是环境不是代码 |
| 错位：一致性分叉 | #7 #8 #11 #12 | 批注四处与兄弟机制各差一个词 |
| 错位：测试钉错对象 | #5 #13 #14 #15 | 钉了环境 umask、账本顺序、注释散文——没钉被测机制本身 |
| 账目更正 | #4 | 「死 chmod」不死：selftest 直调 `_preflight()`，它是 config.toml 唯一保障——**保留，零改动** |
| Deferred | #9 | e2e 解析陷阱今日无触发场景，按「有实证案例再补」 |

核心机制（4 行批注、两处 umask、GatewayCtxReject 删除）本轮审查**零伤**——
元组 `==` 成本可忽略、dict 比较行为优于提案预期（Python dict == 本就不看
键序）、9 枚新钉无假绿。收尾轮只动特例与错位。

## 2. A：崩会话——convert early, log once（agent.py:437-444）

except 分支自开一条日志路径、却只换 content_str 不换 result，是崩与双记的
共同根子。删掉特例，归一化进正常流：

```python
except (TypeError, ValueError) as _ser:
    result = {"error": f"结果不可 JSON 序列化（{type(_ser).__name__}: {_ser}）"
                       "——返回值须为纯 JSON 形（dict/list/str/数字/bool/None）"}
    content_str = json.dumps(result, ensure_ascii=False)
    status = "error"  # 不在此 log——下方统一的 elif 落一次账
```

**净 −1 行**（删 except 里的 log 调用），#1（非 dict 崩会话）与 #6（同一
失败双记 tool_error）同消。原则：错误只有一种形状，日志只有一条路径。
下游 `end_session`/`rewrite` 的 isinstance 检查不受影响（归一后的 dict 无
那些键）。既有钉（selftest.py:3096 只测 dict 载荷）扩一个裸 set 载荷用例，
钉住这条兄弟路径。

## 3. B：凭证——普通数据靠进程基线，秘密靠写点自持

两轮摇摆（全压 umask → 入口数不全；全压写点 chmod → 写点数不全）的根源是
**两层东西被当成一层**。归位后两层不重复、不竞争：

| 层 | 机制 | 本轮动作 |
|---|---|---|
| 普通数据（账本/归档/workspace） | 进程入口 umask | 已就位（cli.main 顶部 + stall_watch 首行），不动 |
| **seed 模板脚本 = 独立进程入口**（清单补全） | 模板顶部各一行 `os.umask(0o077)` | inbox_server 与 directory_server 两模板各加一行（`import json, os, time` 之后）。一处覆盖 directory.json（凭据保险库，seed.py:574 裸 `json.dump(open(DB,"w"))`）、queue.jsonl（:506，含同伴 token 的登记正文）、inbox.key/token.txt 的创建竞态窗（#10）、以及**未来一切新写点**（#2 消解） |
| 凭证文件（秘密性内禀） | 写点 chmod 自持 | ① `tokens._save`（api/tokens.py:46-48）tmp 写后、`os.replace` 前加一行 `os.chmod(tmp, 0o600)`——模块与「谁 import 它」解耦（#5）；② 两模板钥匙早返回（`if k: return k`、`if t: return t`）各补一行 `os.chmod(..., 0o600)`——v2.9.42 时代落下的 0644 存量文件收编（#3） |
| `_preflight` 的 chmod（cli.py:67） | **保留** | #4 账目更正：提案 `trivially-true` §2.2 判它「死重」错误——selftest 直接调用 `_preflight()`（不经 main 的 umask），此行是含 key 的 config.toml 唯一保障。「死代码」断言需要调用方枚举证明，缺证明不删 |

cli.main() 的入口清单注释补全为四类写者进程（cli.main / stall_watch /
inbox_server 模板 / directory_server 模板），stall_check 标注只读。

## 4. C：批注——四个词的一致性（agent.py）

| 发现 | 修法 | 理由 |
|---|---|---|
| #7 `str(fn)` 与四个兄弟事件不一致 | 删 `str()`（None 本就 JSON 安全） | 一个 schema 一种写法；跨事件按名关联不再断 |
| #8 rewrite 后 `_cmd_repeat` 不重置 | rewrite 分支加 `_cmd_repeat = 0` | 与 `text_round_noticed`/`last_context_tokens` 同例——重写即新对话，计数从零起；少一个「为什么这个不重置」的概念 |
| #12 fn="?" 坍缩成「同一命令」 | 批注条件加 `fn != "?"` | 机器对叫不出名字的命令不能说「同一」；畸形调用的错误槽已自明，不需要批注 |
| #11 错误路径 repeat 事件无 args | 事件带 `_snip(str(tc.get("arguments")), 80)` | 有界预览（不回到 v2.9.42 #14 的无界灌入）；复用现成 `_snip`；tool_exec 只在 ok 时落 args，错误路径不再丢「哪条命令在循环」 |

## 5. D：测试——钉行为，不钉环境、不钉散文

1. **token 0600 钉升级**：`_save` 自持 mode 后，钉改为在 **umask 022 下断言
   0600**——钉代码不钉环境，强度反而升级（#5 收尾）；
2. **TOCTOU 钉改全负断言**：`state in ("parked","sleeping")` 半句受共享账本
   daemon_error 顺序影响（推导 override 可出 "error"）——改为只断言
   `state != "running_session"` 且 `current_session is None` 且无
   `breath_unreadable` 键（#14；后两半钉已存在，只改前半）；
3. **删 3 枚死 import**（selftest.py:3263-3268：`as AG` 重复、`llm`、`ast`）；
4. **入口清单 pin 只钉行为**：断言两处 umask 调用存在（grep 级），删除对
   注释散文内容的断言（#15）——注释可以改，机制不能丢。

## 6. E：e2e 潜伏陷阱——留指针，不建机制（#9）

批注前缀使 `tool_results()` 的 `json.loads` 落空走 raw 兜底——**今日零触发**
（现有场景同命令 ≤2 次/会话）。output 的消费者是大脑不是机器，Responses
协议没有侧信道。在 `run_e2e.py:198` 附近加一行注释（「≥3 次同命令调用
的输出带[机器批注]前缀，json.loads 走兜底——需要解析的场景先剥前缀」），
机制一个不建（项目惯例：有实证案例再补）。

## 7. 取舍（明知的）

1. **rewrite 重置计数**：大脑重写上下文后重新计数——理论上给「重写躲批注」
   留缝，但批注本是劝说不设防（判断归大脑），缝隙无害；一致性收益大于
   理论损失。
2. **凭证 chmod 与 umask 两层并存**：不是重复——umask 管无自持力的普通
   数据（进程族），chmod 管秘密性内禀的凭证文件（写点）。枚举各三处，
   有界且稳定。
3. **模板 umask 是进程级**：模板脚本是独立进程（与 stall_watch 同族），
   进程级一处生效正是该模式的正形；脚本内零包依赖的原则不破。
4. **#9 deferred**：留注释指针不留机制，触发那天再议剥前缀还是改 harness。

## 8. 范围与验收

- 行数预算：A −1 行；B 模板 2 行 + chmod 3 行 + 注释补全；C 四个词；
  D ~6 行；E 1 行注释——**净增约 10 行，新增概念 0**。
- selftest：序列化钉扩裸 set 载荷（钉住 #1 族）；token 钉升级 umask 022
  环境；TOCTOU 钉改负断言；入口 pin 改行为断言；删死 import。总量维持
  670 量级。
- 三条单调箭头：
  1. **机器语义职责不增**（零新机制，全是归位与归一）✓
  2. **大脑能力面不减**（事件信息更全：错误路径也有 args 预览）✓
  3. **验证加深**（#1 族兄弟路径入钉；token 钉从环境依赖升级为代码断言）✓
- 关联：`proposal-trivially-true.md`（§2.2 的「死 chmod」误判由本文 §3
  更正；「入口恰好两个」的清单由本文补全为四类写者）；
  `proposal-defense-subtraction.md`（历史）；`constitution.md` §一（机器
  只做递/守门/记账/死与复活——本方案无一出新）、§四（复用现成机制：
  `_snip`/umask/统一错误形状）。

**收敛声明**：批注 4 行无分支、权限两层各归位、测试钉行为——三轮迭代后
机制已减到存在性边缘，再减即伤存在。本轮之后本族议题关闭，除非出现新
实证案例。
