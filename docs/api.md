# xusi 管理面 API（v2）

> 与 agent 的唯一**写**通道是**管理邮箱**：投信（追加 `mailbox.jsonl`）与收信
> （读 `outbox.jsonl`）。只读观察收窄为两条（详情页用）：HTTP GET
> `/v1/events`、`/v1/status`（观察 token 缺失时 xusi 自动签发一枚写进
> `data/webui_tokens.json`），会话索引读磁盘 `sessions.jsonl`。
> 本 API 只做管理面自己的事：agent 簿记、进程生命周期、邮箱、
> 备份、只读观察。**彻底本地化管理**——互联由 xuseek 内核自己完成
> （根智能体 + `[[roots]]` 出生交割，见内核 docs/interconnect.md），xusi 不参与。
>
> agent 的对外呈现（观察台、自建服务）是 xuseek 自家业务——怎么访问由 agent
> 自己经邮箱告知，xusi 不内置相关知识。

- 在线版：`GET /api/docs.md`（本文档）
- Swagger：`/docs`
- 鉴权：`Authorization: Bearer <admin token>` 或 `?mtoken=<admin token>`
  （admin token = `etc/xusi.toml` 的 `[admin].secret`）
- 端口：默认 8601；agent 端口段默认 8602–8699

## 1. 元信息

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /api/health` | 无 | 管理面存活探针 `{ok, service, version, agents}` |
| `GET /api/node` | 无 | 本节点身份 `{id, name, version}`（无敏感字段） |
| `PATCH /api/node` | admin | 改显示名 `{"name": "..."}` |
| `GET /api/whoami` | admin | `{"role": "admin"}`（唯一的角色） |
| `GET /api/brains` | admin | 密钥池摘要（**不回 api_key**）：`[{name, base_url, model, has_key}]` |
| `GET /api/default-roots` | admin | 缺省根智能体（`etc/xusi.toml` 的 `[[default_roots]]`，创建对话框预填；**每次直读盘面，换根 token 改 toml 即生效、免重启**）：`{"roots":[{address, token}]}` |
| `GET /api/versions` | admin | xuseek-v2 版本仓库清单（创建时 `source_version` 用它） |
| `GET /api/ports/available?count=10` | admin | 可用端口（自动分配下拉用） |

## 2. Agent CRUD 与生命周期

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /api/agents` | admin | agent 一览（注册表 + systemd 单元 + 内核呼吸状态） |
| `POST /api/agents` | admin | 创建并启动（见下） |
| `GET /api/agents/{id}` | admin | 单个 agent 状态（进程/簿记；内核自报见 §4 status） |
| `PATCH /api/agents/{id}[?apply_restart=1]` | admin | 改簿记与进程层字段（见下；port 创建后固定） |
| `DELETE /api/agents/{id}` | admin | 删除（须先停止；home 移入 .trash） |
| `POST /api/agents/{id}/start\|stop\|pause\|resume\|restart` | admin | 生命周期五件套 |

### 创建

```bash
curl -X POST http://SERVER:8601/api/agents \
     -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
     -d '{"name":"astronomy","mission":"持续跟踪近地小行星……",
          "brains":["glm","kimi"],"expose":false,"note":"","source_version":"",
          "roots":[{"address":"https://root.example.com","token":"rt-…"}],
          "extra_config":""}'
```

- `name`：显示名（1–64 字符）。**不进 id**——id 一律 `agent-<4位随机hex>`，
  前缀统一；已有 agent 的 id 不变
- `brains`：首个为默认大脑，顺序 = 故障转移序；必须都在密钥池且已配 key
- `source_version`：缺省 = 仓库最新版（解压成实例私有副本）；versions/ 是源码唯一
  事实源，仓库为空时创建报错
- `budgets`：{max_rounds}——v2.7.5+ 内核只认 `[limits] max_rounds`（max_seconds
  已删除、max_context_tokens 由内核按大脑窗口自动派生）；更早内核认 `[agent]`
  三段。渲染格式随 `source_version` 自动分叉
- `roots`（可选，≤8 条）：根智能体 `[{address, token}]`——渲染进出生 config 的
  `[[roots]]` 段（每个条目一个数组表），内核首次启动一次性交割到
  `workspace/playbook/根智能体.json`（此后死键）。address/token 须齐备；
  token 可写 `env:变量名`。**仅 v2.7.12+ 内核支持**——选了旧版内核时创建报错
  （400）；创建后接入走投信（见内核 docs/interconnect.md）。WebUI 创建对话框
  默认预填 `etc/xusi.toml` 的 `[[default_roots]]`（`GET /api/default-roots`，
  可删改）
- `extra_config`（可选，≤8000 字符）：管理员自由 TOML **原样追加**到出生 config
  末尾（`[capabilities]` 等内核可选段或未来新段）。落盘前整体 tomllib 校验，
  写坏直接拒绝创建（400）——xusi 不必追踪内核每个新配置段
- 创建时 xusi 渲染一次 `config.toml`（出生配置：mission/brains/api_key/budgets/
  instance_id/roots/extra_config，chmod 600），**此后 xusi 不再改写该文件**
  （唯一例外：改参按密钥池手术式重渲染 [brain] + [brains.*] 段，见下）。
  `instance_id` = 终身 id（世界唯一、迁移随行）——身份的事实源在实例自己
  身上，注册表只是「本机住着谁」的缓存；克隆（restore new_id）时随新 id
  手术改写
- 启动验收 = systemd 单元 active + 端口进入监听（不再探 agent 的 HTTP）

### 改参（PATCH）

只接受：`name` / `note` / `expose` / `brains`。

- `name`/`note`：写注册表即生效
- `expose`：进程监听参数，返回 `restart_required: true`；
  `?apply_restart=1` 保存并立即重启
- `port`：**创建后固定，PATCH 返回 400**——agent 对外联络 = ip+port，
  改端口等于换地址（断已建立的互联与观测台入口）；要换端口只能删了重建
  （或克隆到新端口）
- `brains`：大脑列表（首个为默认，顺序 = 故障转移序；非空、无重复、都在
  密钥池且已配 key）。手术式重渲染 config.toml 的 `[brain]` + `[brains.*]`
  段（按密钥池模板，其余段逐字节保留）→ 原子落盘（先 tomllib 校验，任何
  失败**原文件不动**）→ 注册表快照同步。**下次呼吸生效，不重启**（内核
  每口呼吸热重载；会话中的呼吸不受影响）；返回 `brains_effective:
  "next_breath"`。与当前快照相同也重渲染（幂等 resync——轮换 brains.toml
  的 key 后对 agent 做任意 PATCH 即触发）
- **mission / budgets 已归 agent 自治**——PATCH 它们返回 400，并提示
  投信让 agent 自己修改自己的 config.toml（内核每口呼吸热重载）

## 3. 管理邮箱（唯一的写通道）

### 投信

```bash
curl -X POST http://SERVER:8601/api/agents/{id}/mail \
     -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
     -d '{"text":"汇报一下现在的进展"}'
```

- 追加 `<home>/data/mailbox.jsonl`（sender=admin，双写 mailbox_log.jsonl 保历史）
- daemon 每 5s 轮询，休眠中有信立即唤醒；会话中下一口呼吸收信
- 改 mission / 调预算 / 让它接入互联（给根地址与 token）——都走这里（换大脑用改参 PATCH，直路且立即反馈）

### 收信

```bash
curl 'http://SERVER:8601/api/agents/{id}/mailbox?box=outbox&limit=50' \
     -H "Authorization: Bearer <admin token>"
```

- `box=outbox`：来信（agent 的 `send_mail` 写，sender=brain）
- `box=inbox`：投信历史（mailbox_log.jsonl）
- 返回 `{"id", "box", "messages": [{"id","sender","text","at"}, ...]}`

## 4. 只读观察与会话（详情页事件流 / 工具统计 / 会话）

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /api/agents/{id}/events?limit=80` | admin | 只读转发内核 `/v1/events`：`{"id","events":[...]}`。事件仅存于 agent 进程内存（环形缓冲，进程重启即清零）；limit 钳 1..500 |
| `GET /api/agents/{id}/status` | admin | 只读转发内核 `/v1/status`（原样透传：daemon 状态 / 下次呼吸 / 工具统计） |
| `GET /api/agents/{id}/sessions?limit=30` | admin | 会话索引：读磁盘 `data/sessions.jsonl` 尾部（最新在前）：`{"id","sessions":[...]}`。limit 钳 1..200 |
| `GET /api/agents/{id}/boot` | admin | Boot 自述：读磁盘 `workspace/BOOT.md` 全文（超 64000 字符截断打 `truncated`；缺失 → `exists:false`）。agent 停机也能看 |
| `GET /api/agents/{id}/ui-url` | admin | 观测台直连入口 `{port, token, expose, active}`——浏览器直连 agent 端口 `/ui/?token=`（不走管理面反代）；token 缺失自动签发进 `data/webui_tokens.json` |

- 观察台 token：`data/webui_tokens.json` 缺失/为空时，xusi **自动签发一枚**写进
  该文件（`secrets.token_urlsafe(32)`、label=`xusi-observe`、merge 不覆盖）；
  内核每请求重读该文件，免重启生效。401 时补签一枚重试一次
- events / status 要求 agent 正在运行（systemd 单元 active），否则
  400「agent 未在运行」；sessions 走磁盘，agent 停机也能看历史呼吸
- 工具统计 = 前端聚合事件流（tool_exec / tool_error / tool_timeout），
  无独立端点；同样是进程内存计数，重启清零

## 5. 日志（进程宿主职责）

```bash
curl 'http://SERVER:8601/api/agents/{id}/logs?limit=300' \
     -H "Authorization: Bearer <admin token>"
```

journalctl 该 agent 单元的最近 N 行（stdout/stderr 捕获，非 agent 接口）。

## 6. 备份 / 恢复

| 端点 | 说明 |
|---|---|
| `POST /api/agents/{id}/backup` | 备份 data/ + workspace/ + config.toml 到 etc/backups/（运行中 = SIGSTOP 冻结窗快照） |
| `GET /api/agents/{id}/backups[?with_meta=1]` | 该 agent 的备份清单 |
| `GET /api/backups[?with_meta=1]` | 全量备份清单（从备份克隆用） |
| `GET /api/backups/{key}` | 备份元数据 + 包内 meta |
| `DELETE /api/backups/{key}` | 删除备份包 |
| `POST /api/restore` | 恢复：`{key}`（WebUI 回滚）或 `{key, new_id, port, brains, note}`（克隆；WebUI 的 new_id 自动生成 `agent-xxxx`，前缀统一） |

恢复 = 解压 → versions 重建私有源码副本 → 写注册表 → 启动。agent 自己的凭证
文件（webui_tokens.json）不进备份包，恢复后由 agent 自行重建。

## 7. 错误与安全

- 统一 JSON：`{"detail": "<人类可读信息>"}`；业务错误 400、系统错误 500
- admin token 不出现在任何响应里；`/api/node` 只回公开身份字段
- 根智能体 token 在创建时渲染进出生 `config.toml`（600，此后归 agent 自治），
  并在注册表 `etc/agents.json`（600）留展示快照——`/api/agents` 是 admin 鉴权
  端点；互联本身（目录、token 轮换、断线恢复）由 xuseek 内核自管（其
  docs/interconnect.md），xusi 不参与
- `expose=true` 意味着 agent 端口 LAN 直通，访问凭证由 agent 自己管理，谨慎开启
