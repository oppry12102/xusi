# 墟司（xusi）—— xuseek 智能体管理面

> 一个自洽目录，管理多个墟寻（xuseek-v2）自主体：创建/启停/暂停/改参/观察/删除，
> token 签发，**单一对外端口（8601）反代所有 agent**。外部安卓 App（观墟台 voidhub）
> 与本地 WebUI 走同一套带 token 鉴权的接口。

## 三分钟上手

管理面作为 systemd 用户服务常驻（`xusi.service`，开机自启）：

```bash
python3 -m xusi install      # 建 venv → 安装并启动 systemd 服务 → 打印 admin token
systemctl --user status xusi # 管理面状态
python3 -m xusi status       # agent 一览
python3 -m xusi doctor       # 环境自检
```

打开 `http://<服务器IP>:8601/?mtoken=<admin token>` ——URL 带 token 打开即认证
（存本浏览器、地址栏参数自动清除），之后直接开 `http://<服务器IP>:8601/` 即可。
认证后点「＋ 新建 agent」。

给外部用户/App 的接入方式见 [`docs/api.md`](docs/api.md)（也在线提供：`GET /api/docs.md`）。
小型实验任务的现成 mission 见 [`docs/mission-examples.md`](docs/mission-examples.md)。

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
                    │  审计 etc/audit.jsonl · 管理面 token etc/tokens.json   │
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
│   ├── api.py           全部路由（含反代两种路由）
│   ├── agentops.py      agent 全生命周期（唯一实现三条通道的地方）
│   ├── systemdctl.py    systemd 用户单元封装
│   ├── registry.py      注册表（参数唯一事实源）etc/agents.json
│   ├── brains.py        密钥池 → 渲染 agent config.toml
│   ├── ports.py         端口三重检验（注册表∪内核监听∪bind试探）
│   ├── authtok.py       管理面 token（统一 admin）
│   ├── proxy.py         反代核心（前缀路由 + token 路由 + HTML 重写）
│   └── webui/           单文件管理页
├── etc/
│   ├── xusi.toml        监听/端口段/源码路径
│   ├── brains.toml      主密钥池（管理员维护；600，模板见 brains.toml.example）
│   ├── agents.json      注册表（agent 档案 + 期望态 + token 记录）
│   ├── tokens.json      管理面 token（600）
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

## 与三个系统的关系

- **xuseek-v2**：agent 的源码与运行时（`--home` 挂接 `instances/<id>`，目录即自主体）。
- **观墟台 voidhub**（`~/work/voidhub`）：App 无需改动——host=服务器IP、port=8601、
  token=agent 观察台 token（token 路由自动定向到对应 agent）。
- **管理面 token 与 agent token 的分工**：前者认证"谁能用管理面/哪些 agent"，
  后者是 agent 观察台的访问凭证（经 `GET /api/agents/{id}/tokens` 发给认证过的用户）。
