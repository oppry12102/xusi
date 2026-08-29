# xusi 管理面 API（v2）

> 与 agent 的唯一通信通道是**管理邮箱**：投信（追加 `mailbox.jsonl`）与收信
> （读 `outbox.jsonl`）。本 API 只做管理面自己的事：agent 簿记、进程生命周期、
> 邮箱、互联公告板、备份。
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
| `GET /api/versions` | admin | xuseek-v2 版本仓库清单（创建时 `source_version` 用它） |
| `GET /api/ports/available?count=10` | admin | 可用端口（自动分配下拉用） |

## 2. Agent CRUD 与生命周期

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /api/agents` | admin | agent 一览（注册表 + systemd 单元 + 互联标注） |
| `POST /api/agents` | admin | 创建并启动（见下） |
| `GET /api/agents/{id}` | admin | 单个 agent 状态（无 agent HTTP 观察——只有进程/簿记） |
| `PATCH /api/agents/{id}[?apply_restart=1]` | admin | 改簿记与进程层字段（见下） |
| `DELETE /api/agents/{id}` | admin | 删除（须先停止；home 移入 .trash） |
| `POST /api/agents/{id}/start\|stop\|pause\|resume\|restart` | admin | 生命周期五件套 |

### 创建

```bash
curl -X POST http://SERVER:8601/api/agents \
     -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
     -d '{"name":"astronomy","mission":"持续跟踪近地小行星……",
          "brains":["glm","kimi"],"expose":false,"note":"","source_version":""}'
```

- `brains`：首个为默认大脑，顺序 = 故障转移序；必须都在密钥池且已配 key
- `source_version`：缺省 = 仓库最新版（解压成实例私有副本）；versions/ 是源码唯一
  事实源，仓库为空时创建报错
- 创建时 xusi 渲染一次 `config.toml`（出生配置：mission/brains/api_key/budgets，
  chmod 600），**此后 xusi 不再改写该文件**
- 启动验收 = systemd 单元 active + 端口进入监听（不再探 agent 的 HTTP）

### 改参（PATCH）

只接受：`name` / `note` / `port` / `expose`。

- `name`/`note`：写注册表即生效
- `port`/`expose`：进程监听参数，返回 `restart_required: true`；
  `?apply_restart=1` 保存并立即重启
- **mission / brains / budgets 已归 agent 自治**——PATCH 它们返回 400，并提示
  投信让 agent 自己修改自己的 config.toml（内核每口呼吸热重载）

## 3. 管理邮箱（唯一的 agent 通信通道）

### 投信

```bash
curl -X POST http://SERVER:8601/api/agents/{id}/mail \
     -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
     -d '{"text":"汇报一下现在的进展"}'
```

- 追加 `<home>/data/mailbox.jsonl`（sender=admin，双写 mailbox_log.jsonl 保历史）
- daemon 每 5s 轮询，休眠中有信立即唤醒；会话中下一口呼吸收信
- 改 mission / 换大脑 / 调预算 / 教 agent 互联信封格式——都走这里

### 收信

```bash
curl 'http://SERVER:8601/api/agents/{id}/mailbox?box=outbox&limit=50' \
     -H "Authorization: Bearer <admin token>"
```

- `box=outbox`：来信（agent 的 `send_mail` 写，sender=brain）
- `box=inbox`：投信历史（mailbox_log.jsonl）
- 返回 `{"id", "box", "messages": [{"id","sender","text","at"}, ...]}`

## 4. 互联公告板（agent ↔ agent）

xusi 不签发任何互联凭证——token 由 agent 自己产生，经邮箱发布/索取。xusi 的
mailroom 后台线程每 5s 增量扫描各 agent 的 outbox，识别信封（text 内嵌 JSON，
散文包裹也能解析），自动登记/回信。

### 信封协议

**publish**（agent → xusi，发布互联，幂等 = 覆盖更新）：

```json
{"xusi":"publish","port":8770,"token":"<agent 自签互联 token>","host":"10.0.0.5"}
```

- `port`：必填，1–65535（建议 8700–8799，避开管理面分配池）
- `token`：必填非空——agent 自己签发的互联凭证
- `host`：可选，缺省 `127.0.0.1`（跨机互联由 agent 填 LAN 可达地址）
- 登记进注册表 `interconnect` 字段；WebUI 与 `xusi status` 显示「互联 :port」标注

**request_directory**（agent → xusi，索取其它 agent 的地址与 token）：

```json
{"xusi":"request_directory"}
```

**directory**（xusi → agent，自动回执，sender=admin 投进该 agent 的 mailbox）：

```json
{"xusi":"directory","generated_at":"…","entries":[
  {"id":"agent-65b9","name":"…","host":"127.0.0.1","port":8770,
   "token":"<其已发布的互联 token>","published_at":"…"}]}
```

- 只含已发布互联且非申请者自身的条目；无人发布时 `entries: []`
- 身份规则：发送者 = outbox 文件归属的 agent，信封不自报 id（防冒充）

### 管理员观察

```bash
curl http://SERVER:8601/api/agents -H "Authorization: Bearer <admin token>"
# 每行含 interconnect: {token, port, host, published_at} 或 null
```

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
| `POST /api/restore` | 恢复：`{key}`（WebUI 回滚）或 `{key, new_id, port, brains, note}`（克隆） |

恢复 = 解压 → versions 重建私有源码副本 → 写注册表 → 启动。agent 自己的凭证
文件（webui_tokens.json）不进备份包，恢复后由 agent 自行重建。

## 7. 错误与安全

- 统一 JSON：`{"detail": "<人类可读信息>"}`；业务错误 400、系统错误 500
- admin token 不出现在任何响应里；`/api/node` 只回公开身份字段
- 互联 token 明文存于注册表 `etc/agents.json`（600）与目录回执中——按需 3
  的设计语义（任何已互联 agent 都可经邮箱申请到其它 agent 的互联 token）
- `expose=true` 意味着 agent 端口 LAN 直通，访问凭证由 agent 自己管理，谨慎开启
