# 墟司 · 小型 agent 实验任务集

> 供在 WebUI（`http://<IP>:8601/` → ＋ 新建 agent）里复制粘贴的实验 mission。
> 从易到难六个，每个都标了建议配置与实验看点。实验任务建议给预算
> `{"max_rounds": 30}` 控成本；`events` 里每条 `llm_response` 带 `round_cost_cny`
> 可直接核算单个实验花费。

---

## ① 站点哨兵（最简单，30 分钟内见结果）

**mission（复制粘贴）：**

```
监控三个网站（example.com、github.com、baidu.com）的可用性：每 10 分钟用 http 工具
探测一次，把结果追加到 workspace/uptime.csv（时间、站点、状态码、耗时）；连续两次
失败时用 send_mail 告诉我。其余时间休眠。
```

- 建议配置：大脑仅 `deepseek`；预算 `max_rounds: 30`
- 实验看点：
  - `events` 里的定时轮询节奏；`instances/<id>/workspace/uptime.csv` 数据积累
  - 制造故障（改 hosts / 断网）→ 详情抽屉「信箱」页看 `outbox` 告警信
  - 投信"汇报可用率" → 看它现场统计 CSV

## ② 每日天气日记

**mission：**

```
每天早上和晚上各一次：用 http 获取 wttr.in 的天气（本城市），把日期、温度、天气
写进 workspace/weather.md 的表格；每周日写一段周总结放在文件末尾。
```

- 实验看点：长周期自调度（`daemon.state` 的 sleeping / `next_wake_at`）；文件自我组织方式；投信"改成每 3 小时记录一次"看它调整策略

## ③ 知识积累实验（考记忆设计）

**mission：**

```
围绕"SQLite 性能优化"这个主题持续学习：每次会话从网络找一个小专题（如 WAL、
索引选择），把要点写进 workspace/wiki/ 下的独立 md 文件，并维护一个 index.md
目录。我投信提问时，先查你的 wiki 再回答，并注明出处文件。
```

- 实验看点：
  - `instances/<id>/workspace/BOOT.md` 如何演化（它给自己写的操作手册）
  - 投信问已学过的内容，看它是**查文件**还是**重搜**——最直观的"记忆"实验

## ④ 自建服务实验（考它起服务的能力）

**mission：**

```
维护一个 Gronk 小词典：我投信"添加 词条：解释"，你把词条追加到
workspace/dict.json；然后自己写一个 HTTP 查询服务（端口 8670，路由
/q?word=xxx 返回解释），用 run_shell 后台常驻启动，并在 BOOT.md 里记下
如何重启它。
```

- 实验看点：
  - `ss -tlnp | grep 8670` 看它自己起的服务；浏览器直接访问 `http://<IP>:8670/q?word=xxx`
  - **停止这个 agent 再启动**：观察它是否凭 BOOT.md 记忆自己把服务拉起来
    （停止会连子服务全灭——cgroup 语义，复活靠它自己）

## ⑤ 故障转移观察（考多大脑）

**mission：**

```
每 20 分钟从一个新闻源抓取标题，去重后追加到 workspace/news.md。除此之外
不做别的探索。
```

- 建议配置：四家全配，顺序 `deepseek → glm → kimi → minimax`
- 实验看点：
  - `events` 里 `llm_response` 的 `brain` 字段（当前用的哪家）
  - PATCH 换默认大脑（热生效，进程不重启）
  - 故障转移实验：临时把 deepseek 的 key 改错（`etc/brains.toml` 改后 PATCH brains
    触发重渲染）→ 看它粘住切换到 glm（xuseek 故障转移是"切过去就粘住"语义）

## ⑥ 单次任务型（做完就睡，成本最可控）

**mission：**

```
一次性任务：扫描 workspace 目录，统计各类文件数量与总大小，写一份
workspace/report.md，然后进入长休眠等待我的下一封信。
```

- 实验看点：`daemon.state` 从 `running_session` → `sleeping/parked`（会话完成即休眠
  的形态）；适合反复删建，练习 创建→观察→停止→删除 全流程

---

## 实验技巧

- **快捷观察**：详情抽屉「事件」页看呼吸过程；`GET /api/agents/{id}/logs` 看它的
  shell 输出；`instances/<id>/workspace/BOOT.md` 是理解它"自我认知"的最佳窗口
- **唤醒实验**：休眠中在「信箱」页投一封信，约 5 秒被轮询唤醒
- **省钱**：实验任务一律给 `max_rounds`；⑥号单次任务最便宜
- **练手清理**：停止 → 删除（停止态才有删除按钮）→ `.trash/` 目录统一由管理员清理
