# 墟司（xusi）· 外部访问 API 文档

> 墟司是多个墟寻（xuseek-v2）自主体的管理面：**一个端口（默认 8601）承载全部访问**——
> 管理 API、各 agent 观察台的反代、本地 WebUI。外部安卓 App（观墟台 voidhub）与
> WebUI 走同一套 token 鉴权接口。
>
> - **Base URL**：`http://<服务器IP>:8601`（管理面监听 0.0.0.0:8601）
> - **协议**：HTTP + JSON（UTF-8）；交互式文档 `GET /docs`（Swagger）
> - 所有时间戳为 UTC ISO8601（`Z` 结尾）

---

## 1. 鉴权：两层 token

| 层 | 用途 | 形态 | 获取 |
|---|---|---|---|
| **管理面 token** | 调用 `/api/*`、`/px/{id}/*` | `Authorization: Bearer <token>` 或 `?mtoken=<token>`（浏览器用，会进访问日志，勿外发） | 管理员在服务器签发（见 §8） |
| **agent 观察台 token** | 访问某个 agent 的观察台（`/v1/*`、`/ui/*`） | `Authorization: Bearer <token>` 或 `?token=<token>` | `GET /api/agents/{id}/tokens`（经管理面认证后获取） |

管理面 token 两种角色：
- **admin**：全权（创建/删除/改参/启停/签发 token）；
- **user**：仅能访问 `agents` 范围内的 agent（观察、投信、经 `/px` 访问、取该 agent 的 token）。

未带 token / token 无效：

```http
HTTP/1.1 401 Unauthorized
{"detail": "missing or invalid manager token"}
```

无权访问该 agent（user 越范围、user 调管理操作）：

```http
HTTP/1.1 403 Forbidden
```

---

## 2. 总路由图

| 路径 | 鉴权 | 说明 |
|---|---|---|
| `/api/health` | 无 | 管理面探活 |
| `/api/*` | 管理面 token | 管理 API（§3–§6） |
| `/px/{agent-id}/*` | 管理面 token | 前缀反代到该 agent（§7.1，自动注入 agent token） |
| `/v1/*`、`/ui/*` | agent 观察台 token | token 路由反代（§7.2，App 直连形态） |
| `/` | 可 `?mtoken=<管理面token>` 直达 | WebUI 管理页（URL 带 token 打开即认证并存本浏览器，地址栏参数自动清除） |
| `/docs`、`/api/openapi.json` | 无 | Swagger / OpenAPI |
| `/api/docs.md` | 无 | 本文档 |

---

## 3. 元信息

```bash
curl -s http://SERVER:8601/api/health
# {"ok":true,"service":"xusi","version":"1.0.0","agents":1,"at":"..."}

curl -s -H "Authorization: Bearer $T" http://SERVER:8601/api/whoami
# {"label":"admin","role":"admin","agents":["*"]}

curl -s -H "Authorization: Bearer $T" http://SERVER:8601/api/brains
# [{"name":"deepseek","base_url":"https://api.deepseek.com","model":"deepseek-v4-flash","has_key":true}, ...]
# api_key 永不回显

curl -s -H "Authorization: Bearer $T" "http://SERVER:8601/api/ports/available?count=5"
# {"range":[8602,8699],"ports":[8602,8603,8604,8605,8606]}
```

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
| `note` | str? | 备注 |

创建过程（同步，约数秒）：初始化实例目录 → 播种经验库 → 渲染 config.toml（含所选大脑 key，600 权限）→ systemd 拉起（**Restart=always 掉线保护**）→ 健康验收 → 签发首个观察台 token（label `xusi-proxy`）。失败自动回滚。成功返回 `201` + agent 档案。

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

### 投信（影响大脑的唯一通道，user 可用）

```bash
curl -s -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"text":"汇报一下现在的进展"}' http://SERVER:8601/api/agents/{id}/mail
# → {"posted":true,"id":"...","at":"..."}   休眠中约 5 秒内被轮询唤醒
```

---

## 6. agent 观察台 token（admin 签发；user 可取授权范围内的）

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

## 7. 反代：外部访问 agent 的两种方式（同一端口 8601）

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

---

## 8. 管理面 token 的签发（服务器本地 CLI）

```bash
cd /home/htao/work/xusi
.venv/bin/python -m xusi token new <label>                          # user（默认无范围）
.venv/bin/python -m xusi token new alice --role user --agents astronomy-7f3k,astock-9k2d
.venv/bin/python -m xusi token new boss --role admin                # 管理员
.venv/bin/python -m xusi token list                                 # 含完整 token
.venv/bin/python -m xusi token revoke <token前缀≥8位>
```

---

## 9. 错误响应

| HTTP | 场景 |
|---|---|
| 400 | 业务校验失败（mission 为空、端口不可用、密钥池缺大脑、agent 未运行时暂停/观察等），`detail` 有中文说明 |
| 401 | 缺 token / token 无效 |
| 403 | user 越权（管理操作或范围外 agent） |
| 404 | agent / 资源不存在 |
| 502 | 反代目标不可达（agent 已停止或暂停——暂停中属正常） |

---

## 10. 安全说明

- 管理面监听 `0.0.0.0:8601`，是**唯一**需要放行的对外端口；agent 默认仅 `127.0.0.1`。
- token 明文存于服务器上 600 权限文件（`etc/tokens.json` / 各 agent `data/webui_tokens.json`），管理员可随时读回、撤销即时生效。
- LLM api_key 由管理面代持（`etc/brains.toml`，600），签发给用户的 token 只授予观察台权限，接触不到 key。
- `?mtoken=`（管理面）与 `?token=`（观察台）会进访问日志，仅建议浏览器一次性使用；脚本/App 一律用 Bearer 头。
- 管理操作全量审计：`etc/audit.jsonl`（谁、何时、对哪个 agent、做了什么）。
