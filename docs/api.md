# 墟司（xusi）· 外部访问 API 文档

> 墟司是多个墟寻（xuseek-v2）自主体的管理面：**一个端口（默认 8601）承载全部访问**——
> 管理 API、各 agent 观察台的反代、本地 WebUI。外部安卓 App（观墟台 voidhub）与
> WebUI 走同一套 token 鉴权接口。
>
> - **Base URL**：`http://<服务器IP>:8601`（管理面监听 0.0.0.0:8601）
> - **协议**：HTTP + JSON（UTF-8）；交互式文档 `GET /docs`（Swagger）
> - 所有时间戳为 UTC ISO8601（`Z` 结尾）

---

## 1. 鉴权：四档 token

| 层 | 用途 | 形态 | 获取 |
|---|---|---|---|
| **管理面 token**（admin） | 任何 `/api/*` + 反代入口 | `Authorization: Bearer <token>` 或 `?mtoken=<token>`（浏览器用，会进访问日志，勿外发） | 管理员在服务器签发（见 §8） |
| **api token** | **只**进 `/px /svc /v1 /ui`（反代入口），**任何 `/api/*` 都拒** | 同上（Bearer / `?mtoken=` / `?token=`） | `POST /api/tokens`（admin 签发，明文只返一次） |
| **互联 token** | **只**进 `/svc`（同集群 agent 互调） | `Authorization: Bearer <token>` 或 `?mtoken=<token>` | `POST /api/inter-agent-tokens`（admin 签发，明文只返一次） |
| **agent 观察台 token** | 仅该 agent 的 `/v1 /ui /px` | `Authorization: Bearer <token>` 或 `?token=<token>` | `GET /api/agents/{id}/tokens`（经管理面认证后获取） |

四档凭证**互不相通**：

- admin token 走任何端点（管理面 + 反代）——唯一能调 `/api/*` 写端点
- api token 只能进反代入口——**不能**调 `GET /api/tokens` 自己，更不能调 `DELETE`
- 互联 token 只能进 `/svc`——**不能**调任何 `/api/*` 写端点；与 api token 完全隔离，
  revoke 互不影响（一个影响外部服务、一个仅影响集群内 agent 互通信）
- agent webui token 仅对所属 agent 的 `/v1 /ui /px` 有效

**为什么分出 api / 互联 两档**：
- **admin token** 太重要（管理员私用），不暴露给任何外部/集群场景
- **api token** 给外部反代服务（手机 App / 第三方客户端）——revoke 影响外部所有调用方
- **互联 token** 给本集群 agent 互调用——revoke 只影响集群内 agent ↔ agent 通信，
  不影响外部服务、不影响 admin、不影响各 agent 自己的观察台。blast radius 最小。

管理面 token **统一为 admin**：全权（创建/删除/改参/启停/签发 token）。

历史背景：本系统曾区分 admin / user 两种 role（user 限制 agents 范围）。系统只供管理员使用后已统一签 admin；存量的 `role="user"` token 启动时静默升 admin，行为不变。

未带 token / token 无效：

```http
HTTP/1.1 401 Unauthorized
{"detail": "missing or invalid token..."}
```

管理面 token 通过后一律放行——不再有 403 Forbidden（用户场景已删除）。
api / 互联 token 通过也放行（仅限反代入口）；不带 / 无效在反代入口 → 401，
在 `/api/*` → 一律 401（连 401 都跟 admin token 走同一条，不会暴露路由存在性）。

---

## 2. 总路由图

| 路径 | 鉴权 | 说明 |
|---|---|---|
| `/api/health` | 无 | 管理面探活 |
| `/api/*` | **仅**管理面 token | 管理 API（§3–§6） |
| `/px/{agent-id}/*` | 管理面 token / api token / agent 观察台 token | 前缀反代到该 agent（§7.1） |
| `/svc` | 管理面 token / api token / agent 观察台 token | agent 自建服务**发现**（§7.3.1） |
| `/svc/{agent-id}/{服务名}/*` | 管理面 token / api token / agent 观察台 token | agent 自建服务**全功能反代**（§7.3） |
| `/v1/*`、`/ui/*` | 管理面 token / api token / agent 观察台 token | token 路由反代（§7.2，App 直连形态） |
| `/` | 可 `?mtoken=<管理面token>` 直达 | WebUI 管理页（URL 带 token 打开即认证并存本浏览器，地址栏参数自动清除） |
| `/docs`、`/api/openapi.json` | 无 | Swagger / OpenAPI |
| `/api/docs.md` | 无 | 本文档 |

---

## 3. 元信息

```bash
curl -s http://SERVER:8601/api/health
# {"ok":true,"service":"xusi","version":"1.1.0","agents":1,"at":"..."}

curl -s -H "Authorization: Bearer $T" http://SERVER:8601/api/whoami
# {"label":"admin","role":"admin","agents":["*"]}

curl -s -H "Authorization: Bearer $T" http://SERVER:8601/api/brains
# [{"name":"deepseek","base_url":"https://api.deepseek.com","model":"deepseek-v4-flash","has_key":true}, ...]
# api_key 永不回显

curl -s -H "Authorization: Bearer $T" http://SERVER:8601/api/versions
# {"repo_dir":".../versions","default_ready":true,
#  "versions":[{"version":"v2.3.0","file":"xuseek-v2-v2.3.0.zip",
#   "size_bytes":123456,"mtime":"..."}]}   ← 管理员投放的 xuseek-v2 版本包（docs/versions.md）；
#   清单首项 = 最新版 = 创建 agent 的缺省选择；default_ready = 共享主源码是否本地就绪

curl -s -H "Authorization: Bearer $T" "http://SERVER:8601/api/ports/available?count=5"
# {"range":[8602,8699],"ports":[8602,8603,8604,8605,8606]}
```

---

## 3b. api token 管理（admin-only）

反代入口凭证：admin 签发、admin 吊销，存 sha256（明文只在签发响应里出现一次）。
**api token 只能进 `/px /svc /v1 /ui`，调任何 `/api/*` 一律 401**——保护 admin
token 不外泄给外部反代服务。详见 §1（鉴权）和 §10（安全说明）。

```bash
# 签发：admin 鉴权，label 可选（不传 = 空字符串）
curl -s -X POST -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
     -d '{"label":"voidhub-bridge"}' \
     http://SERVER:8601/api/tokens
# → 201 {"id":"tk_xxxxxx","hash":"sha256:...","label":"voidhub-bridge",
#       "created_at":"2026-08-25T07:47:56Z","token":"<明文，只此一次>"}

# 列出：脱敏（id/label/created_at），不含 hash / 明文
curl -s -H "Authorization: Bearer $ADMIN" http://SERVER:8601/api/tokens
# → [{"id":"tk_xxxxxx","label":"voidhub-bridge","created_at":"..."}]

# 吊销
curl -s -X DELETE -H "Authorization: Bearer $ADMIN" http://SERVER:8601/api/tokens/tk_xxxxxx
# → {"revoked":"tk_xxxxxx"}
```

拿到 api token 后用它调任一反代入口（与 admin token 同形）：

```bash
curl -s -H "Authorization: Bearer $API" http://SERVER:8601/svc
curl -s -H "Authorization: Bearer $API" http://SERVER:8601/px/<agent-id>/v1/health
curl -s "http://SERVER:8601/v1/health?token=$API"
```

**集群语义**：api token 不跨节点同步（每节点一份 `etc/tokens.json`，外部服务
绑定到具体入口）。跨节点时在每台各签发一次。审计落 `etc/audit.jsonl` 的
`token.new` / `token.revoke` 事件。

---

## 4. agent 生命周期

### 4.1 创建并启动（admin）

```bash
curl -s -X POST http://SERVER:8601/api/agents \
  -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{
    "name": "astronomy",
    "mission": "每天跟踪 arXiv 天体物理新论文并入库。",
    "brains": ["deepseek", "glm", "kimi", "minimax"],
    "expose": false,
    "port": 8605
  }'
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 显示名；agent-id 自动生成 `name-slug-4位随机` |
| `mission` | str | 长期使命（必填，随时可改、热生效） |
| `brains` | [str] | 大脑列表（`GET /api/brains` 的 name；**首个为默认，顺序=故障转移序**） |
| `expose` | bool | `true`=agent 监听 `0.0.0.0:port` 直接对外；默认 `false` 仅 `127.0.0.1`，一切外部访问走管理面反代 |
| `port` | int? | 指定端口（8602–8699，已做占用检验）；缺省自动分配 |
| `budgets` | object? | 探索回路安全网 `{max_rounds, max_seconds, max_context_tokens}`。**缺省不写任何预算**（全不限，LLM 完全自主）；给了才写、只写给出的键，0 = 不限 |
| `source_version` | str? | xuseek-v2 版本号（`GET /api/versions`）。该版本源码解压为**实例私有副本**（`instances/<id>/xuseek-v2/`，实例自洽可单独迁移）；缺省 = **仓库最新版**；`"main"` = 共享主源码（保留值，将逐步废弃）。创建后不可改，实际版本见返回的 `source_version` |
| `note` | str? | 备注 |

创建过程（同步，约数秒）：〔选了版本：解压私有源码副本〕→ 初始化实例目录（**无条件播入全部能力包种子**——几 KB 文本，归大脑的世界）→ 渲染 config.toml（含所选大脑 key，600 权限；内核/大脑写入的段如 `[capabilities]` 保真回传）→ systemd 拉起（**Restart=always 掉线保护**）→ 健康验收 → 签发首个观察台 token（label `xusi-proxy`）。失败自动回滚。成功返回 `201` + agent 档案（含 `source_version`）。

### 4.1b 能力包（只读观察）

```bash
# 该 agent 的能力包清单：种子已在它的 workspace 里；enabled 反映 config [capabilities]
# 实况（通常全 false——管理面不写该段；若大脑自行写入亦如实显示）
curl -s -H "Authorization: Bearer $T" http://SERVER:8601/api/agents/{id}/capabilities
# → {"capabilities":[{"name":"amem","version":"1.0.0","summary":"…","extras":"amem",
#     "enabled":false,"costs":{"disk":"~2GB…","ram":"…","llm":"…"}}]}
```

> **分工裁决**：管理面**只负责种子，剩下的事情交给 agent 自己做**——不写
> `[capabilities]`、不装依赖、不为此重启 agent。启用与否（register_skill）、
> 依赖安装（run_shell 后台 pip，pack 的 playbook 指南有说明）全归大脑；
> 管理面只观察（清单、成本、开关实况）。

### 4.2 启停 / 暂停 / 续跑 / 重启（admin）

```bash
curl -s -X POST -H "Authorization: Bearer $T" http://SERVER:8601/api/agents/{id}/start
curl -s -X POST -H "Authorization: Bearer $T" http://SERVER:8601/api/agents/{id}/stop     # 优雅停：轮边界落盘
curl -s -X POST -H "Authorization: Bearer $T" http://SERVER:8601/api/agents/{id}/pause    # SIGSTOP 冻结
curl -s -X POST -H "Authorization: Bearer $T" http://SERVER:8601/api/agents/{id}/resume   # SIGCONT 续跑
curl -s -X POST -H "Authorization: Bearer $T" http://SERVER:8601/api/agents/{id}/restart
# → {"id":"...","desired_state":"running"}   （desired_state: running|stopped|paused）
```

> **暂停语义**：进程驻留但冻结（观察台暂停响应）。若恰逢会话中在途 LLM 调用，恢复后该
> 调用可能超时——xuseek 的大脑池会自动故障转移，属可接受损耗。停止/重启一律优雅停
> （SIGTERM，等轮边界把会话落盘后再退）。

### 4.3 改参（admin）

```bash
# 热改参（mission/brains/budgets/name/note）：写 config.toml，agent 下一个大循环自动重读，
# 不打断运行中的进程；brains 变更会从密钥池取最新 key 重新渲染。
curl -s -X PATCH -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"mission":"新使命…","brains":["glm","deepseek"]}' \
  http://SERVER:8601/api/agents/{id}
# → {...档案..., "restart_required": false, "restarted": false}

# 换端口 / 改暴露开关：必须重启进程。加 ?apply_restart=true 立即执行（优雅停→新参数拉起→健康验收）
curl -s -X PATCH -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"port":8606,"expose":true}' "http://SERVER:8601/api/agents/{id}?apply_restart=true"
# → {..., "restart_required": true, "restarted": true}
```

### 4.4 删除（admin；**必须先停止**）

```bash
curl -s -X DELETE -H "Authorization: Bearer $T" http://SERVER:8601/api/agents/{id}
# → {"id":"...","deleted":true,"moved_to":"/home/htao/work/xusi/instances/.trash/<id>-xxxxxx"}
```

**安全闸：agent 处于运行态（含暂停态）时删除被拒绝**（HTTP 400，提示先停止）——
必须先显式 `stop` 再删除，两步操作防误删。已停止的 agent 删除时：注册表除名、
实例目录**移入 `instances/.trash/`**（不物理删除——遗留数据由管理员自行清理）。

---

## 5. 观察（只读，不打断 agent 运行）

全部为对 agent 观察台 `/v1/*` 只读 GET 的聚合/转发，绝不写、绝不干预：

```bash
B="-H Authorization:Bearer\ $T"
curl -s $B http://SERVER:8601/api/agents                  # 全部 agent 状态聚合（含离线者）
curl -s $B http://SERVER:8601/api/agents/{id}             # 单个：进程/健康/daemon 状态
curl -s $B http://SERVER:8601/api/agents/{id}/status      # 原样 /v1/status（daemon、大脑、工具统计）
curl -s $B "http://SERVER:8601/api/agents/{id}/events?limit=50"    # 事件流
curl -s $B "http://SERVER:8601/api/agents/{id}/sessions?limit=20"  # 会话索引
curl -s $B "http://SERVER:8601/api/agents/{id}/messages?limit=30"  # 来信历史
curl -s $B "http://SERVER:8601/api/agents/{id}/outbox?limit=30"    # 大脑外发
curl -s $B "http://SERVER:8601/api/agents/{id}/logs?limit=200"     # 进程日志（journald）
```

状态聚合里的关键位：`process.active/sub`（systemd）、`health.ok`、
`agent_status.daemon.state`（`running_session` 呼吸中 / `sleeping` 休眠 / `parked` 驻留 / `stopped`）、
`agent_status.daemon.mailbox_pending`（待收信）、`process.auto_restarts`（掉线自动拉起次数）。

### 对端发现（agent 互通信入口）

agent 想找其他 agent 时调一次——懒查询，**不推送**、不写 agent home：

```bash
# 用 agent 自己的观察台 token 调（最常见）
curl -s -H "Authorization: Bearer $AGENT_TOKEN" \
     http://SERVER:8601/api/agent-peers
# → {"self":{"id":"agent-X"},
#    "access_pattern":"/svc/{peer_id}/{service_name}/*",
#    "peers":[
#      {"id":"agent-A","name":"Astronomy","node_id":"node-1",
#       "inter_agent_token":"<本 xusi 那把互联 token>"},
#      {"id":"agent-B","name":"Bio","node_id":"node-2",
#       "inter_agent_token":"<node-2 那把互联 token>"}
#    ]}
```

admin / api / 互联 token 调用也可以（这时不返回 `self`，且能看到自己所在的 peer 行）。
cluster 模式自动跨节点 fan-in；远端 peer 行带的是该远端 xusi 自己的互联 token
（若远端尚未签发则该字段缺省）。

拿到 peer 行后直接用它带的 `inter_agent_token` 调 `/svc/<peer_id>/<service_name>/...`：

```bash
curl -s -H "Authorization: Bearer $PEER_INTER_TOKEN" \
     http://SERVER:8601/svc/agent-A/inbox/...
# 走 node-1 xusi 的 /svc，node-1 验互联 token 合法后透传给 agent-A 的 inbox 服务
```

**为什么这样做**：
- xusi **不下发 peers.json 到 agent home**——写 agent 工作目录会越界，且几百 agent 时维护成本高
- xusi **不开新鉴权**——已有四档 token（admin / api / 互联 / agent webui）都接受
- 实际通信走 `/svc/<peer_id>/<service_name>/*`——见 §7.3 自建服务反代，由对端在
  `workspace/data/services.json` 里声明 inbox 服务（建议命名 `inbox` / `contact`），
  xusi 不替 agent 决定通信格式、鉴权、是否广播入口
- 互联 token 是同集群 agent 互调用的专用凭证，跟 api token 隔离——revoke 一个不影响另一个
- agent 之间怎么协商（要不要广播 token / 要不要互信）由 agent 自己决定

agent 创建时已自动种一份"对端发现与联系" playbook 条目进 `workspace/playbook/对端发现与联系.md`。

### 投信（影响大脑的唯一通道，admin 调用）

```bash
curl -s -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"text":"汇报一下现在的进展"}' http://SERVER:8601/api/agents/{id}/mail
# → {"posted":true,"id":"...","at":"..."}   休眠中约 5 秒内被轮询唤醒
```

---

## 6. agent 观察台 token（admin 签发）

```bash
# 列出（含完整 token——可直接交给"经管理面认证的用户"）
curl -s $B http://SERVER:8601/api/agents/{id}/tokens
# [{"token":"AbC…xyz","label":"xusi-proxy","created_at":"...","recorded_by_xusi":true}]

# 签发
curl -s -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"label":"phone"}' http://SERVER:8601/api/agents/{id}/tokens

# 撤销（前缀 ≥8 位，立即生效）
curl -s -X DELETE -H "Authorization: Bearer $T" http://SERVER:8601/api/agents/{id}/tokens/AbCdEf12
```

---

## 6b. 互联 token（admin 签发，每 xusi 一把）

互联 token 是同集群 agent ↔ agent 互调 `/svc` 时用的"入场券"。每 xusi
**只持 0 或 1 把**——本 xusi 上所有 agent 公用；不绑 agent 身份，跟 api token
同构但作用域更窄（只 `/svc`）。

```bash
# 签发：若已存在则返现有那条（不重发——避免覆盖正在用的）
curl -s -X POST -H "Authorization: Bearer $T" \
     http://SERVER:8601/api/inter-agent-tokens
# → {"id":"iat_abc123","token":"...","label":"inter-agent","created_at":"..."}

# 列出（admin 视角，含明文）
curl -s -H "Authorization: Bearer $T" http://SERVER:8601/api/inter-agent-tokens
# [{"id":"iat_abc123","token":"...","label":"inter-agent","created_at":"..."}]

# 撤销（按 id，agent 之间立即失去 /svc 互调能力）
curl -s -X DELETE -H "Authorization: Bearer $T" \
     http://SERVER:8601/api/inter-agent-tokens/iat_abc123
# → {"revoked":"iat_abc123"}
```

轮换：先 DELETE 旧的那把（agent 之间互调立即中断），再 POST 拿新 token，
最后用某种方式把新 token 通告给所有 agent（通常是 LLM 重新调用一次
`/api/agent-peers` 拿最新值）。

为什么需要这一档：

- **api token 给外部服务**（手机 App 等）——revoke 影响所有外部调用方，blast radius 大
- **互联 token 只给本集群 agent**——revoke 只影响集群内互通信，不影响外部、不影响 admin
- 两档完全隔离：互联 token 泄了，吊销它外部服务不受影响；api token 泄了，吊销它 agent
  互通信不受影响

agent 拿到互联 token 的途径：调 `/api/agent-peers`，peer 行里就有（每 peer 标注
它所在 xusi 那把）。

---

## 7. 反代：外部访问 agent 的三种方式（同一端口 8601）

### 7.1 前缀路由 `/px/{agent-id}/*`（管理面 token）—— 浏览器 / 通用客户端

```bash
curl -s -H "Authorization: Bearer $T" http://SERVER:8601/px/{id}/v1/status   # ← 等价 agent 的 /v1/status
curl -s -H "Authorization: Bearer $T" http://SERVER:8601/px/{id}/ui/         # ← agent 自带观测台页面（自动带 token）
```

- 管理面 token 鉴权通过后，转发时**自动注入该 agent 的观察台 token**——客户端无需持有第二层 token；
- agent 自带的 `/ui/` 页面做了路径重写，经代理直接可用；
- 转发始终走服务器内部 `127.0.0.1:<port>`，无论 agent 是否对外暴露。

### 7.2 token 路由 `/v1/*`、`/ui/*`（agent 观察台 token）—— **voidhub App 零改动接入**

App 里"添加智能体"（IP + 端口 + Token）填：

```
IP:     <服务器IP>
端口:   8601                ← 管理面端口（所有 agent 共用）
Token:  <该 agent 的观察台 token>
```

管理面凭 token 实时识别该请求属于哪个 agent 并定向转发（原样透传，含 `POST /ui/mail`）。
**一台服务器、一个对外端口、任意多个 agent**——不同 agent 只是不同 token。

```bash
# App 实际发出的等价请求：
curl -s -H "Authorization: Bearer $AGENT_TOKEN" http://SERVER:8601/v1/status
curl -s http://SERVER:8601/v1/health                                  # 无 token 探活 → 管理面应答
curl -s -X POST -H "Authorization: Bearer $AGENT_TOKEN" \
     -H "Content-Type: application/json" -d '{"text":"你好"}' \
     http://SERVER:8601/ui/mail
```

> 直接暴露（`expose=true`）的 agent 也可不经反代、直连 `http://SERVER:<agent端口>` + token 访问；
> 默认不暴露，最小化外网面。

### 7.3 服务路由 `/svc/{agent-id}/{service}/*` —— agent 自建的对外 API（**全功能反代**）

agent（大脑）可能在 workspace 里自建对外服务（如 FastAPI 行情/交易 API），监听独立端口
（仅 `127.0.0.1`）。这些服务通过 services.json 清单声明（见 §11），管理面统一收编反代——
**全功能透明转发**：任意 HTTP 方法（GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS）、请求体、
查询参数原样过，响应**流式回传**（SSE / 分块 / 大响应不被掐断）。某方法是否允许由
服务自己决定（上游的 405 等状态码原样透传），管理面不替 agent 决策。

```bash
curl -s  -H "Authorization: Bearer $T" http://SERVER:8601/svc/{id}/{svc}/openapi.json
curl -s  -H "Authorization: Bearer $T" http://SERVER:8601/svc/{id}/{svc}/api/v1/status
curl -s -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
      -d '{"id":"example-1"}' http://SERVER:8601/svc/{id}/{svc}/api/v1/items  # 写方法照常透传
curl -s -N -H "Authorization: Bearer $T" http://SERVER:8601/svc/{id}/{svc}/api/v1/stream   # SSE/流式
curl -s  -H "Authorization: Bearer $T" http://SERVER:8601/svc/{id}/{svc}/docs  # 原生 Swagger 页（已做路径重写）
```

- **鉴权同 `/px`（二选一）**：管理面 token，或该 agent 的观察台 token——
  voidhub App 等只持观察台 token 的客户端直接可用；
- **token 注入不变式**：客户端的 `Authorization` 一律不透传——清单声明了 `token_file`
  则管理面服务端读取并替换注入（每次请求实时读，agent 轮换 token 自动跟随），
  没声明则删除。**客户端的 token 绝不会到达 agent 自建服务**；
- **写审计**：非 GET/HEAD/OPTIONS 的调用记录进 `etc/audit.jsonl`（`svc.write`：谁、何时、
  哪个服务、方法、路径、上游状态码）——只被动记录，不干预；
- **响应头透传**：除逐跳头与 content-length/date/server 外全部透传（上游的 CORS 头可达
  浏览器端）；浏览器 **CORS 预检**（OPTIONS + `Access-Control-Request-Method`）由管理面
  本地应答 204——预检发不出 Authorization，真实请求照常鉴权，安全性不变；
- 转发始终走 `127.0.0.1:{port}`；读超时放宽到 600s（长任务 POST / SSE）；
- 错误：404（agent 或服务名不存在，附可用服务名清单）、403（越权）、
  400（路径含 `..`）、502（服务不可达）；上游业务状态码原样透传。

#### 7.3.1 外部程序三步接入

**凭 token 能做什么**（先回答三个常见问题）：

| 问题 | 答案 |
|---|---|
| 凭 token 能查有哪些 agent 吗？ | **agent 观察台 token → `GET /svc`** 返回该 token 所属 agent 的档案与服务清单（App 用它确认 token 归属）。要**枚举全部 agent** 需管理面 token（`GET /api/agents` 或 `GET /svc`）——观察台 token 只属于一个 agent，这是刻意的权限边界 |
| 能查管理面的接口文档吗？ | 能，且无需 token：`GET /api/docs.md`（本文档）；Swagger `GET /docs` |
| 能拿到 agent 服务的 API 文档吗？ | 能：`GET /svc` 返回每个服务的 `openapi`（管理面动态解析出的**实际可用路径**，null=无自描述）；再按该路径取 spec。无自描述的服务（如自写 HTTP 服务）路径需问 agent 或看其 workspace |

**第一步 · 发现**——只持 token 的客户端（App 形态：IP + 端口 + token）用 `GET /svc`：

```bash
# 凭 agent 观察台 token → 仅该 agent 的服务清单
curl -s -H "Authorization: Bearer $AGENT_TOKEN" http://SERVER:8601/svc
# → {"agents":[{"agent":"<agent-id>","name":"<agent 显示名>","base":"/svc/<agent-id>/",
#     "services":[{"name":"my-api","title":"我的服务","port":8710,
#       "base_path":"","openapi":"/openapi.json","auth":true,"auto":false,
#       "token_source":"manifest","openapi_source":"manifest"}]}]}

# 凭管理面 token → 返回全部 agent
curl -s -H "Authorization: Bearer $T" http://SERVER:8601/svc
```

字段说明：`auto: true` = agent 未写清单、管理面自动发现的服务（命名 `auto-{port}`）；
`auth: true` = 管理面已定位到服务 token、转发时自动注入（`token_source` 为 `manifest`
=agent 声明 / `auto`=管理面按候选搜索）；`openapi` = 实际可用的自描述路径
（`openapi_source` 同理），`null` = 无自描述。**服务名完全由 agent 自定**，与协议无关。

**第二步 · 读接口定义**——`openapi` 非空时取 spec：

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" \
     http://SERVER:8601/svc/<agent-id>/my-api/openapi.json
```

> openapi.json 里的 `paths` 是**服务本地路径**（如 `/api/v1/items`），经反代调用时
> 要前拼 `/svc/{agent-id}/{服务名}`——管理面不改写 agent 的自描述，保持代理透明。
> 无自描述的服务（`openapi: null`）只能直接调路径（向 agent 问或读其 workspace 文档），
> WebUI 探索器对此类服务自动转手填模式。

**第三步 · 调用**——拼上 `base`，任意方法：

```bash
BASE=http://SERVER:8601/svc/<agent-id>/my-api
curl -s -H "Authorization: Bearer $AGENT_TOKEN" $BASE/api/v1/items
curl -s -X POST -H "Authorization: Bearer $AGENT_TOKEN" -H "Content-Type: application/json" \
     -d '{"id":"example-1"}' $BASE/api/v1/items
```

（路径为示意，实际以该服务 openapi.json / agent 说明为准。）

**管理 API 形态的服务发现**（WebUI「服务」tab、API 探索器的数据源，含探活与清单错误）：

```bash
curl -s -H "Authorization: Bearer $T" http://SERVER:8601/api/agents/{id}/services
# → {"id":"...","services":[{"name":"my-api","port":8710,"title":"…","openapi":"/openapi.json",
#     "auth":true,"health":{"ok":true,"status":200,"ms":35},
#     "warn":"（仅端口落在分配池内时出现）"}],"errors":[]}
# 清单完全由 agent 的 services.json 自声明；管理面不代登记、不代改（不干预 agent）。
```

> 线上实际入口以 `GET /svc` 实时返回为准（服务名各 agent 自定，与协议无关）。

---

## 7b. 集群互联：节点身份 · 跨节点 SSO

xusi 实例之间的互联：**每节点仍然是完整自治的 xusi**，互联只是再加几条对等 URL。
没有 master、没有中心 DB；每个 worker / backup / portal 节点跑同一份代码、持有自己的
注册表，差别只在 `[node].role` 与 `etc/peers.toml` 里的对等名册。

### 节点身份

每个 xusi 在 etc/xusi.toml 里有一份 `[node]`：

```toml
[node]
id   = "auto-or-set-on-install"   # 安装时自动生成（secrets.token_urlsafe(6)）
role = "worker"                    # worker | backup | portal
```

`role` 不同节点行为：
| role | 本地 agent | 写操作 | 用途 |
|---|---|---|---|
| worker（默认） | 允许 | 增删改 agent | 跑业务的真实节点 |
| backup | 禁止 | 只接 backup.* | 镜像其他 worker 的备份 |
| portal | 禁止 | 全部转发 | 纯 UI 聚合 / 对外面向 |

**仅 worker 节点可以注册 agent**。backup / portal 角色 `POST /api/agents` / `POST /api/restore`
直接 400（架构层拦住，不是权限拦截）。

`id` 字段由 `python -m xusi install` 自动生成并写回 toml（保形追加，不重写文件、不动注释）。
手动改也行（id 是机器身份，改完请同步更新对端 peer 名册）。

### 节点显示名（可改）

每节点另有一个 `etc/node.json`，只存 `name`（默认是 socket.gethostname）：

```http
PATCH /api/node
Authorization: Bearer <admin-token>
Content-Type: application/json

{"name": "北京·主服务器 · 创新药集群"}
```

UI 顶栏点节点名即可改名（admin only）。id / role 不让改（API 里也不让覆盖）。

### 自报（peer 之间无鉴权也能拿到）

```http
GET /api/peer/id
→ 200
{"id":"abc123","name":"北京·主服务器","role":"worker",
 "version":"1.2.1","url":"http://10.0.0.1:8601"}
```

不鉴权——peer 之间在建立信任之前就要先拿到对方自报。仅返回公开字段，**永不返回 secret / token**。

### 跨节点 SSO：`[cluster].secret`

```toml
[cluster]
# 留空 = 单节点模式（今天的行为，无任何变化）
# 设值 = 同密钥的所有 xusi 互信 token（任一节点签发、所有节点通用）
secret = "IxY7...32字节以上随机..."
```

机制：`authtok.new_token()` 检测 `cfg.cluster_secret`：
- **留空** → 同今天一样随机 `secrets.token_urlsafe(32)`，明文存 etc/tokens.json，等值比较。
- **已设** → 签发 HS256-JWT（载荷 `{label, role, agents, iat, jti, kpr:"xusi"}`），同密钥的所有
  xusi 都能验。`verify()` 先验 JWT；失败回退明文等值（覆盖 secret 由空转非空那一刻的遗留 token）。

**撤销跨节点 token** 的简明语义：同密钥信任所有 token，撤销靠两件事——
① 各节点在自己的 tokens.json 里前缀移除（仅本节点签发的）；② 万一要全集群失效，所有
节点同时改 `[cluster].secret` 让旧的 token 全部失效。这是分布式 token 联邦的典型妥协。

切到 WebUI 上：跨 xusi 切换 = 在浏览器里跳转 `peer.url + '/?mtoken=' + currentToken`。
同密钥就直接登入；非同密钥落回对方自己登录页。

### 集群视图

```http
GET /api/cluster
Authorization: Bearer <any-token>
→ 200
{"self": {...}, "peers": []}
```

`peers[]` 来自 Phase 2 的 `etc/peers.toml`；每个 peer 含 `id/name/url/ok/info/error/latency_ms`。

---

## 7c. 集群互联 Phase 2：peer 名册 + 跨节点读

**前置**：每台 xusi 的 `etc/xusi.toml` 都设 `[cluster].secret = "<同一段 hex>"`——空值仍是单节点模式，所有 peer 操作拒绝（HTTP 400 PeerRefused）。

### peer 名册 CRUD

```bash
# CLI
.venv/bin/python -m xusi peers add http://10.0.16.15:8601        # 注册并探活
.venv/bin/python -m xusi peers add http://10.0.16.15:8601 --name "vm-16-15"
.venv/bin/python -m xusi peers list                              # 列出 + 探活（5s 缓存）
.venv/bin/python -m xusi peers probe                             # 强制重探
.venv/bin/python -m xusi peers remove <peer-id>

# API（admin 写；任意 token 读）
GET    /api/peers            → {"cluster": bool, "peers": [...]}
POST   /api/peers            body: {"url": "http://...", "name": "..."}    → 201
DELETE /api/peers/{peer_id}  → 200 {"removed": "..."}
POST   /api/peers/probe      → 200 {"probed": N, "results": [...]}    # 强制重探
```

`peers.toml` 手写亦可：
```toml
[[peers]]
id = "<peer 的 etc/node.id>"
url = "http://10.0.16.15:8601"
name = "vm-16-15"
```

`id` 字段冗余存一份（peer 自报）方便 grep；url 是事实源。

### 跨节点读路径

任何 xusi 的 `/api/agents/{id}/*` 读端点在本机未命中时自动 fan-out 给所有 peer，找到就透传 caller 的 JWT 到目标 peer，peer 用同密钥重验 + 重 enforce 作用域。

**v1 支持（GET，全部）**：
- `/api/agents` —— fan-in：本地 + 全部 peer 的 agent 列表合并，peer 行带 `_via: <peer-id>`
- `/api/agents/{id}` —— 单 agent status
- `/api/agents/{id}/capabilities` / `services`
- `/api/agents/{id}/{status,events,sessions,messages,outbox,logs}` —— 观察全 6 种
- `/api/agents/{id}/tokens` —— 观察台 token 列表
- `/api/agents/{id}/backups` —— 远端 agent 的备份包（一定在远端节点）

**v1 不支持（写路径，留 v2）**：
- `/api/agents/{id}` PATCH/DELETE
- `/api/agents/{id}/{start,stop,pause,resume,restart}` —— 生命周期写
- `/api/agents/{id}/mail` / `backup` / `tokens` POST+DELETE —— 投信、备份、token 撤销
- `/px/{id}/*` `/svc/{id}/*` `/v1/*` `/ui/*` —— 浏览器反代（HTML 重写 + Location 前缀跨节点一致性 v2 再说）

### locality 缓存

`xproxy.resolve(agent_id)` 本地优先 → peer 并发 fan-out。TTL 30s 命中 / 5s 未命中，避免 agent 重命名后短暂不一致、避免恶意 ID 穿透打 peer。

### 失败语义

- peer 不可达 → 502 Bad Gateway（`PeerUnreachable`）；单个 peer 挂掉不影响其他 / 不影响本地
- peer 4xx → 透传同码（如 403：peer 的用户作用域过滤掉 caller 拥有的 agent）
- 集群模式未启用 → 400 PeerRefused（仅 peer 注册路径；读端点直接 404）

### 前端

WebUI 顶栏的节点对话框「其他节点」区显示 peer 列表（绿/红点 + 延迟 + 打开链接）。agent 卡片与详情面板对远端 agent 加 `来自 <peer 名>` 徽章——调用方无需关心 agent 实际在哪台机器。

---

## 8. 管理面 token 的签发（服务器本地 CLI）

```bash
cd /home/htao/work/xusi
.venv/bin/python -m xusi token new <label>                       # 签发（默认 admin）
.venv/bin/python -m xusi token new boss --rotate                  # 旧 PLAIN 全部作废，留 1 把新的
.venv/bin/python -m xusi token list                               # 含完整 token；cluster 模式下 JWT 标 [cluster]，明文 legacy 标 [legacy]
.venv/bin/python -m xusi token revoke <token前缀≥8位>
```

**两种形态（cluster 模式自动选其一）**：

| 形态 | 形态特征 | 跨集群 | 说明 |
|---|---|---|---|
| **JWT**（默认） | `xxx.yyy.zzz` 三段 | ✓ | 同 `[cluster].secret` 的所有节点互信；推荐 |
| **PLAIN** | 无点的随机串 | ✗（仅本机） | 单节点遗留形态，cluster 模式仅供本地向后兼容 |

`token list` 在 cluster 模式下会自动在每行末尾标 `[cluster]` / `[legacy]`，方便一眼分辨。

**`--rotate` 的语义**：签发新 JWT 前先把同 role 的旧 JWT 全部撤销（PLAIN 不动——它在 cluster 模式本来就不参与跨集群通信）。意图是用户层面始终只看见一把 active token，避免「这把该用哪把」的混淆。

**对应 HTTP API**（仅 admin）：
- `GET /api/tokens` —— 列出（含 `kind` 字段：`local`/`cluster`/`legacy`）
- `POST /api/tokens` —— 签发（body: `{label, role, agents, rotate}`）
- `DELETE /api/tokens/{prefix≥8位}` —— 撤销

**跨集群的 PLAIN 透明处理**：如果你手头只有一把 PLAIN legacy token，`fetch_json` 和 `forward_to_peer` 都会**当场签短期 JWT**（默认 5 分钟 TTL）发给 peer —— 你无需重签即可访问对端 agent 的读端点。caller 是 JWT 时则透传不重签。

---

## 9. 错误响应

| HTTP | 场景 |
|---|---|
| 400 | 业务校验失败（mission 为空、端口不可用、密钥池缺大脑、agent 未运行时暂停/观察等），`detail` 有中文说明 |
| 401 | 缺 token / token 无效 |
| 403 | （历史场景：user 越权——已删除；管理面 token 始终放行） |
| 404 | agent / 资源不存在 |
| 502 | 反代目标不可达（agent 已停止或暂停——暂停中属正常） |

---

## 10. 安全说明

- 管理面监听 `0.0.0.0:8601`，是**唯一**需要放行的对外端口；agent 默认仅 `127.0.0.1`，自建服务也建议仅 `127.0.0.1`。
- 三档凭证互不相通：admin token（`[cluster].secret`）管 `/api/*` + 反代入口；api token（`etc/tokens.json`，存 sha256）**只**进反代入口 `/px /svc /v1 /ui`，调 `/api/*` 一律 401；agent webui token 仅该 agent 有效。外部反代服务（手机 App / 第三方客户端）只拿 api token，admin token 永不外泄。
- api token 的明文仅签发响应里出现一次，文件存 hash（sha256）；600 权限。吊销即时生效（删记录即拒）。
- LLM api_key 由管理面代持（`etc/brains.toml`，600），签发给用户的 token 只授予观察台权限，接触不到 key。
- `/svc` 反代里客户端的 `Authorization` 绝不透传给 agent 自建服务（声明 `token_file` 则服务端替换注入，否则删除）。
- `?mtoken=`（管理面 / api token）与 `?token=`（观察台 / api token）会进访问日志，仅建议浏览器一次性使用；脚本/App 一律用 Bearer 头。
- 管理操作全量审计：`etc/audit.jsonl`（谁、何时、对哪个 agent、做了什么，含 `token.new` / `token.revoke`）；`/svc` 对 agent 服务的写调用同样入审计（`svc.write`）。

---

## 11. services.json：agent 自建服务的自声明约定（agent → 管理面，文件通道）

**约定的告知通道**：创建 agent 时管理面自动在 `workspace/playbook/对外服务接入.md`
播种一份「对外接口 playbook」——与 init 播种的 llm-调用/工具与环境 等基础经验条目
同类同位，agent 的经验机制自然读到它（已存在不覆盖）——
agent 据此知道"支持本协议 = 获得正式对外入口"，其余自由发挥；不写清单的服务由
动态发现兜底收编（`auto-{port}` 临时命名，可发现性差，正式入口以清单为准）。

agent 想把自建服务（API/看板/工具页）暴露给管理面用户时，写一份清单文件即可，
**管理面每次请求实时读取**——换端口、轮换 token、上下线，管理面自动跟随，无需重启。

**声明的字段只是"提示"，不是门槛**——管理面的发现是三层兜底，agent 声明永远优先：

1. **services.json 声明**（权威）：port / token_file / openapi 按声明用；
2. **字段未声明或失效时按候选补齐**：
   - `openapi`：先验证声明路径，404/坏内容则按候选探测
     （`/openapi.json` `/api/openapi.json` `/v1/openapi.json` `/docs/openapi.json`
     `/swagger.json` …，带服务 token），命中即用（结果缓存 60s）；
   - `token_file`：未声明则按候选搜索
     （`workspace/data/api_token.txt`、`workspace/data/api_tokens.json`
     （JSON 里取 `tokens[]` 首个启用的、admin 优先）、`service_token.txt` 等），
     找到即注入——**token 内容仍每次实时读**，轮换自动跟随；
3. **完全没写清单的服务**：管理面扫 agent 单元（systemd cgroup）内进程的监听端口
   （观察台端口除外），HTTP 探活后以 **`auto-{port}`** 名义收编进发现结果与
   `/svc` 路由——agent 什么都不写也能被外部访问；agent 日后在清单声明同端口，
   即由清单条目（含正式命名）接管，`auto-` 条目自动消失。

所有探测只读（HTTP GET / `/proc` / 文件存在性），不写 agent 任何文件、不干预其运行；
网络探测只发生在发现/列表路径，反代转发热路径零额外开销。

**文件位置**（两处任选，同名时 workspace 侧优先）：

- `<instance>/workspace/data/services.json` —— 推荐：agent 的 run_shell 顺手写的位置
- `<instance>/data/services.json`

**格式**（UTF-8 JSON 数组）：

```json
[
  {
    "name": "my-api",
    "port": 8710,
    "title": "我的服务（UI 显示名）",
    "base_path": "",
    "openapi": "/openapi.json",
    "probe": "/health",
    "token_file": "workspace/data/api_token.txt",
    "note": "任意备注"
  }
]
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✓ | 路由键 `/svc/{agent-id}/{name}/*`。取名自由（中文/大写/数字/`-`/`_` 均可，客户端自动 URL 编码），唯一要求是能安全作 URL 路径段：不含空格与 `/ \ ? # %`、非 `.`/`..`、1–64 字符；**保持稳定**（改了外部入口就变） |
| `port` | ✓ | 服务监听的本地端口；管理面转发永远走 `127.0.0.1:{port}`。**建议 8700–8799**（8602–8699 是 agent 分配池，撞上会被警告） |
| `title` | | UI 显示名，缺省 `name` |
| `base_path` | | 服务挂在子路径时前拼（如 `/api`） |
| `openapi` | | OpenAPI 自描述路径，缺省 `/openapi.json`；`false` = 无自描述（WebUI 转手填模式） |
| `probe` | | 探活路径（相对 base_path），缺省 `/` |
| `token_file` | | 服务自身的 Bearer token 文件，**相对 agent home**（如 `workspace/data/api_token.txt`）。管理面服务端读取注入，**绝不回显给客户端**；禁绝对路径与 `..` |
| `note` | | 备注，UI 展示 |

（未知字段忽略。要不要拦某类方法由服务自己实现——管理面全功能透传，不替 agent 决策。）

**要点**：

- 服务建议绑 `127.0.0.1`（不写 host 或显式 `--host 127.0.0.1`）——对外只经管理面 8601 这一个端口，
  服务 token 也不必发给任何人（管理面按 token_file 或候选搜索服务端注入）；
- 清单声明**建议写**（命名稳定、可读性好），但漏写/写错不阻断接入——三层兜底会补齐
  （见上）；`auto-{port}` 命名在 agent 补写清单后自动升级为正式名；
- 非法输入逐级降级不炸管理面：缺文件=空清单、坏 JSON=该文件忽略、坏条目=跳过，
  错误信息出现在 `GET /api/agents/{id}/services` 的 `errors` 里（WebUI 服务 tab 也会显示）；
- 有 OpenAPI 的服务（FastAPI 自带 `/openapi.json`，或候选路径探测命中）在 WebUI 里自动获得
  **动态 API 探索器**：端点列表 → 按定义生成表单（含写方法的请求体表单）→
  发送（任意方法，写方法记审计）→ 响应按 content-type 渲染（JSON 美化/表格切换、markdown 渲染）。
