# 墟司（xusi）—— xuseek 智能体管理面

> 一个自洽目录，管理多个墟寻（xuseek-v2）自主体：创建/启停/暂停/改参/观察/删除，
> token 签发，**单一对外端口（8601）反代所有 agent**。外部安卓 App（观墟台 voidhub）
> 与本地 WebUI 走同一套带 token 鉴权的接口。

## 三分钟上手

管理面作为 systemd 用户服务常驻（`xusi.service`，开机自启）：

```bash
python3 -m xusi install      # 建 venv → 写 etc/xusi.toml → 启 systemd 服务 → 打印 admin token
systemctl --user status xusi # 管理面状态
python3 -m xusi status       # agent 一览
python3 -m xusi doctor       # 环境自检
```

打开 `http://<服务器IP>:8601/?mtoken=<admin token>` ——URL 带 token 打开即认证
（存本浏览器、地址栏参数自动清除），之后直接开 `http://<服务器IP>:8601/` 即可。
认证后点「＋ 新建 agent」。

给外部用户/App 的接入方式见 [`docs/api.md`](docs/api.md)（也在线提供：`GET /api/docs.md`）。
小型实验任务的现成 mission 见 [`docs/mission-examples.md`](docs/mission-examples.md)。

## 凭证设计（三档独立）

管理面用三档凭证，**互不相通、各管各的**——任何一档泄露都只炸自己这一档
的爆炸半径：

| 凭证 | 文件 / 位置 | 谁签发 | 用途 |
|---|---|---|---|
| **admin token** | `etc/xusi.toml` 的 `[cluster].secret` | `xusi install` / `xusi init` | 管理面全权（任何 `/api/*` 端点 + 反代入口） |
| **api token** | `etc/tokens.json`（明文存盘，600；admin 视角） | `POST /api/tokens`（admin） | **只**进反代入口（`/px /svc`，及 `/v1/health` 探活），给外部反代服务用 |
| **agent webui token** | `instances/<id>/data/webui_tokens.json` | agent CLI（`xuseek token new`） | 仅该 agent 的 `/v1 /ui /px` |

**admin token** 与 **api token** 的关键边界：

- admin token → 任何 `/api/*` 端点（写操作只有它能进）+ 反代入口
- api token → **只**进 `/px /svc`（`/v1` 仅放行 `health` 探活——api token
  不绑 agent，路由不了具体目标）；**任何 `/api/*` 都拒**（含
  `GET /api/tokens` 自己）；不能让外部服务借它篡改管理面
- agent webui token → 跟具体 agent 绑死，不能跨 agent

**admin token 不出现在外部世界**。手机 App / 外部服务 / 浏览器扩展永远只
拿 api token，反代走 `/px` `/svc` `/v1` `/ui` 即可——他们压根不知道有
admin token 存在。

集群内所有 xusi 配同一个 `cluster_secret` 即互相视为同一集群：

- 本机拿 secret → 任何 agent 的读/写、PATCH/DELETE/lifecycle 全通
- 远端 agent → 同一 secret 在 peer 端 `verify()` 也通过 → 自动通配

api token 不跨节点同步（每节点一份 `tokens.json`，外部服务绑定到具体入口）。
要跨节点请在每台各签发一次。

**没有 JWT、没有 invitation**。`etc/tokens.json` 在 2026-08-25 起重启用，
**新 schema**（对象含 `tokens` 数组，明文存盘）。老格式（顶层 list 含 admin
token）启动时一次性迁移：把第一条 PLAIN admin token 接管成 `cluster_secret`，
老文件改名 `.migrated.*`——这条迁移在新格式落盘后**自动跳过**（migration
run 看见顶层是对象就 no-op）。

新建 xusi：`python3 -m xusi init --secret <secret>` 把已有集群的 secret 同步进
`etc/xusi.toml` 即可加入。

### api token 速查

```bash
# 签发（admin token 鉴权）
curl -X POST http://xusi:8601/api/tokens \
     -H "Authorization: Bearer <admin token>" \
     -H "Content-Type: application/json" \
     -d '{"label": "voidhub-bridge"}'
# → {"id":"tk_xxx","token":"<明文>","label":"...","created_at":"..."}
```

```bash
# 列出（admin 视角，含明文——token 明文存盘于 etc/tokens.json，600）
curl http://xusi:8601/api/tokens -H "Authorization: Bearer <admin token>"

# 吊销
curl -X DELETE http://xusi:8601/api/tokens/tk_xxx -H "Authorization: Bearer <admin token>"
```

外部服务拿到 api token 后用它调任一反代入口：
```bash
curl http://xusi:8601/svc -H "Authorization: Bearer <api token>"
curl http://xusi:8601/px/<agent-id>/v1/health -H "Authorization: Bearer <api token>"
curl 'http://xusi:8601/v1/health?token=<api token>'
```

（`/v1` 入口对 api token 只放行 `health` 探活——api token 不绑 agent，
路由不了具体目标；`/v1/* /ui/*` 的完整功能凭 agent 观察台 token。）

## 集群 / peer 名册

集群是**信任的全对称网络**，不区分谁是 hub / 谁是 worker。每个 xusi 都能：
- 完整看到自己的所有 agent
- fan-in peer 名册里其它 xusi 的 agent（peer 名册在 `etc/peers.toml`，两边可
  不一样 —— fan-in 仅在双向注册的范围内求并，不递归 peers-of-peers）
- 通过浏览器「节点对话框 → 打开」直接跳到 peer 的 UI（mtoken 在新 tab 自动
  续上，peer 端 `?mtoken=` 鉴权通过即登录）

加 peer：

```bash
xusi peer add http://<peer>:8601
```

—— 双方 `cluster_secret` 一致就自动互信。

## 架构

```
                    ┌──────────────── 外部（局域网/互联网）────────────────┐
                    │   voidhub App（IP+8601+agent token）   浏览器 WebUI   │
                    └───────────────────────┬───────────────────────────────┘
                                            │ 唯一对外端口 :8601
                    ┌───────────────────────▼───────────────────────────────┐
                    │  墟司 xusi（xusi.service, Restart=always）             │
                    │  /api/* 管理 · /px/{id}/* 前缀反代 · /v1 /ui token路由 │
                    │  注册表 etc/agents.json · 密钥池 etc/brains.toml       │
                    │  cluster_secret etc/xusi.toml [cluster].secret         │
                    │  peer 名册 etc/peers.toml                              │
                    └──────┬──────────────────┬──────────────────┬──────────┘
                     systemd-run 单元   只读 HTTP GET       文件（config/
                     Restart=always     127.0.0.1:<port>    mailbox/token）
                    ┌──────▼──────────────────▼──────────────────▼──────────┐
                    │  xuseek-v2 agent ×N：instances/<id>/（目录即自主体）    │
                    │  systemd 单元 xusi-a-<id>，默认仅听 127.0.0.1:86xx     │
                    └─────────────────────────────────────────────────────────┘
```

**与 agent 之间只有三条通道，绝不 import xuseek 代码**（去耦合的不变式）：

1. **进程与信号** —— systemd 瞬态单元（启停/重启 + SIGSTOP/SIGCONT 暂停续跑）；
2. **只读 HTTP GET** —— `127.0.0.1:<port>/v1/health|status`（观察，绝不写）；
3. **文件** —— 渲染 `config.toml`、追加 `mailbox.jsonl` 投信、读 `webui_tokens.json`、tail journald。

## 目录

```
xusi/
├── xusi/                管理面源码（Python 3.12，stdlib + fastapi/uvicorn/httpx）
│   ├── api/             路由（agent_routes / peer_routes / proxy_routes / ...）
│   ├── agentops.py      agent 全生命周期（唯一实现三条通道的地方）
│   ├── systemdctl.py    systemd 用户单元封装
│   ├── registry.py      注册表（参数唯一事实源）etc/agents.json
│   ├── brains.py        密钥池 → 渲染 agent config.toml
│   ├── ports.py         端口三重检验（注册表∪内核监听∪bind试探）
│   ├── authtok.py       管理面凭证（verify(cluster_secret) → rec）
│   ├── proxy.py         本地/远端调度（resolve + forward_to_peer + /px 反代）
│   ├── peers.py         peer 名册 + 探活
│   ├── backup.py        本地备份（远端走 forward）
│   └── webui/           单文件管理页
├── etc/
│   ├── xusi.toml        监听/端口段/源码路径 + [cluster].secret（admin token）
│   ├── tokens.json      反代入口凭证（api token，明文存盘 600；admin 签发/吊销）
│   ├── brains.toml      主密钥池（管理员维护；600，模板见 brains.toml.example）
│   ├── agents.json      注册表（agent 档案 + 期望态 + token 记录）
│   ├── peers.toml       peer 名册（[[peers]]，两边各管自己的）
│   └── audit.jsonl      管理操作审计
├── instances/<id>/      每个 agent 一个 home（config.toml, data/, workspace/；
│                        选了版本的 agent 还有私有源码副本 xuseek-v2/）
├── instances/.trash/    删除后的遗留（管理员自行清理）
├── versions/            xuseek-v2 版本仓库（不入 git；管理员投放 zip，约定见 docs/versions.md）
└── docs/                api.md（外部 API 文档）· mission-examples.md（实验任务）
```

## 运维要点

- **掉线保护（两层）**：① agent 单元 `Restart=always`——崩溃/误杀 5s 内自动拉起；
  ② 管理面启动时 reconcile——机器重启后按注册表期望态（running/stopped/paused）拉齐。
- **暂停** = SIGSTOP 冻结大脑（它自起的后台服务继续跑）；停止/重启一律优雅停，
  轮边界把会话落盘后再退，cgroup 内子服务一并停止。
- **改参**：mission/brains/budgets 写 config.toml 热重载（下一口呼吸生效，不打断）；
  port/expose 需重启，界面/`?apply_restart=true` 一键执行。
- **密钥轮换**：改 `etc/brains.toml` → 对 agent 做任意 PATCH 触发重渲染 → 热生效。
- **xuseek-v2 版本仓库**：管理员把版本源码打包投放 `versions/xuseek-v2-<版本号>.zip`
  （打包方法见 `docs/versions.md`；**整个 versions/ 目录不入 git**——zip 含私有源码）。
  新建 agent **缺省即取仓库最新版**：源码解压成实例私有副本（`instances/<id>/xuseek-v2/`，
  各自构建 .venv，实例间互不影响，实例目录自洽**可单独迁移**，删除时随 home 进 .trash），
  实际版本记入注册表、创建后不可改。共享主源码（`source_dir`）**将逐步废弃**：仅现存
  agent 与显式 `source_version="main"`（或仓库为空的缺省回落）在用。已有 agent 不受影响。
- **删除**：必须先显式停止（运行/暂停态拒绝删除，防误删）→ 注册表除名 →
  目录移入 `instances/.trash/`（遗留数据由管理员自行 rm）。
- **暴露面**：默认一切外部访问经 8601 反代；`expose=true` 才让 agent 直接监听 `0.0.0.0:<port>`。
- **cluster_secret 轮换**：改了 `etc/xusi.toml` 的 `[cluster].secret` → `systemctl --user restart xusi`。
  跨集群轮换要同步改所有节点 + 让所有浏览器重新登一次。

## 与三个系统的关系

- **xuseek-v2**：agent 的源码与运行时（`--home` 挂接 `instances/<id>`，目录即自主体）。
- **观墟台 voidhub**（`~/work/voidhub`）：App 无需改动——host=服务器IP、port=8601、
  token=agent 观察台 token（token 路由自动定向到对应 agent）。
- **管理面 token 与 agent token 的分工**：管理面凭证就是 `cluster_secret`（全员 admin、
  跨节点通配）；agent 观察台 token（每 agent 独立签发）只用来认证 agent 自己的 `/v1/* /ui/*`。