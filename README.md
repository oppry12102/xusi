# 墟司（xusi）—— xuseek 智能体管理面

> 一个自洽目录，管理多个墟寻（xuseek-v2）自主体：创建/启停/暂停/删除。
> **xusi 与 agent 之间只有一条写通道：管理邮箱**——投信/收信；只读观察收窄为
> 两条（详情页事件流/会话 banner：GET /v1/events·status，token 缺失自动签发；
> 会话索引读磁盘 sessions.jsonl）。
> agent 间的互联 token 由 agent 自己发行，经邮箱发布登记，xusi 只做公告板；
> agent 的对外呈现（观察台、自建服务）是 agent 自家业务，xusi 不参与。

## 三分钟上手

管理面作为 systemd 用户服务常驻（`xusi.service`，开机自启）：

```bash
python3 -m xusi install      # 建 venv → 写 etc/xusi.toml → 启 systemd 服务 → 打印 admin token
systemctl --user status xusi # 管理面状态
python3 -m xusi status       # agent 一览（含互联标注）
python3 -m xusi doctor       # 环境自检
```

打开 `http://<服务器IP>:8601/?mtoken=<admin token>` ——URL 带 token 打开即认证
（存本浏览器、地址栏参数自动清除），之后直接开 `http://<服务器IP>:8601/` 即可。
认证后点「＋ 新建 agent」。

外部接入方式见 [`docs/api.md`](docs/api.md)（也在线提供：`GET /api/docs.md`）。
小型实验任务的现成 mission 见 [`docs/mission-examples.md`](docs/mission-examples.md)。

## 凭证设计（单档）

只有一档凭证：**admin token** = `etc/xusi.toml` 的 `[admin].secret`，
由 `xusi install` / `xusi init` 生成。它通吃所有 `/api/*` 端点。

agent 侧的凭证（观察台、自建服务等）全部由 agent 自己管理，xusi 不签不发不撤不碰——
那是 xuseek 自家业务。例外之一：**详情页只读观察**（/v1/events、/v1/status）在
`data/webui_tokens.json` 缺失时，xusi 自动签发一枚 `xusi-observe` token 写进该文件
（merge 不覆盖，内核免重启生效）；例外之二：**互联 token**（agent ↔ agent 直连用）：
agent 自己发行，经管理邮箱发布/索取，xusi 作为公告板存储与转述（见下）。

**xusi 不再签发、不再撤销任何其它 agent 侧的 token。**

## 互联（agent ↔ agent）

xusi 只当**公告板**，不替 agent 做任何决定：

1. 想互联的 agent 自生成 token + 互联端口，经管理邮箱发 **publish 信封**：

   ```json
   {"xusi":"publish","port":8770,"token":"<自签互联 token>","host":"10.0.0.5"}
   ```

   （`host` 可省略 = 127.0.0.1，跨机互联自己填 LAN 可达地址。）xusi 自动登记，
   WebUI 的 agent 卡片出现「互联 :8770」标注；重复发布 = 覆盖更新。

2. 想找别人联：经管理邮箱发 **request_directory 信封**，xusi 自动回执当前所有
   已互联 agent 的 `{id, name, host, port, token}`：

   ```json
   {"xusi":"request_directory"}
   ```

信封解析宽容（JSON 可以包在散文里）；普通来信照常展示给管理员，不参与处理。

## 架构

```
                    ┌──────────────── 外部（局域网/互联网）────────────────┐
                    │      浏览器 WebUI（xusi 管理页，:8601）               │
                    └───────────────────────┬───────────────────────────────┘
                                            │ :8601（xusi）
                    ┌───────────────────────▼───────────────────────────────┐
                    │  墟司 xusi（xusi.service, Restart=always）             │
                    │  /api/* 管理 · 注册表 etc/agents.json                 │
                    │  密钥池 etc/brains.toml（仅创建时渲染一次）              │
                    │  admin token etc/xusi.toml [admin].secret             │
                    │  mailroom 线程：5s 扫各 agent outbox（互联信封）        │
                    └──────┬─────────────┬────────┬──────────────────────────┘
                     systemd-run 单元  管理邮箱  只读观察（详情页）
                     Restart=always  mailbox ⇄  GET /v1/events·status
                                      outbox    （token 缺失自动签发）
                    ┌──────▼─────────────▼────────▼──────────────────────────┐
                    │  xuseek-v2 agent ×N：instances/<id>/（目录即自主体）   │
                    │  systemd 单元 xusi-a-<id>；对外呈现（观察台/服务）归    │
                    │  agent 自治，xusi 不参与                              │
                    └────────────────────────────────────────────────────────┘
```

**与 agent 之间只有一条写通道——管理邮箱**，绝不 import xuseek 代码、绝不调
xuseek CLI、绝不反代、绝不改写 config.toml（创建时渲染一次
出生配置，此后归 agent 自治）；只读观察两条 HTTP GET
（/v1/events、/v1/status——详情页事件流/工具统计/会话 banner，token 缺失
自动签发），会话索引读磁盘：

- **投信**：追加 `<home>/data/mailbox.jsonl`（sender=admin，与内核 post() 同语义，
  双写 mailbox_log.jsonl 保历史；daemon 5s 轮询唤醒）；
- **收信**：读 `<home>/data/outbox.jsonl`（内核 send_mail 工具写，sender=brain；
  mailroom 后台线程 5s 增量扫描，识别互联信封 → 登记/自动回信）。

systemd 进程与信号（spawn/stop/SIGSTOP/SIGCONT/journalctl）是宿主职责，不算通信。

## 目录

```
xusi/
├── xusi/                管理面源码（Python 3.12，stdlib + fastapi/uvicorn）
│   ├── api/             路由（agent_routes / backup_routes / meta_routes / auth / models）
│   ├── agentops.py      agent 全生命周期 + 投信/收信（邮箱写通道）+ 只读观察与会话
│   ├── mailroom.py      互联信箱：outbox 增量扫描 + 信封解析/登记/回执
│   ├── systemdctl.py    systemd 用户单元封装（spawn 注入 PyPI 镜像 env）
│   ├── registry.py      注册表（簿记 + 互联标注）etc/agents.json（600）
│   ├── brains.py        密钥池 → 创建时渲染一次 agent config.toml
│   ├── ports.py         端口三重检验（注册表∪内核监听∪bind试探）
│   ├── authtok.py       管理面凭证（verify(admin token) → rec）
│   ├── backup.py        本地备份（SIGSTOP 冻结窗快照）
│   └── webui/           单文件管理页
├── etc/
│   ├── xusi.toml        监听/端口段/版本仓库路径 + [admin].secret（admin token）
│   ├── brains.toml      主密钥池（管理员维护；600，模板见 brains.toml.example）
│   ├── agents.json      注册表（agent 簿记 + 期望态 + 互联标注）
│   ├── outbox_state.json  mailroom 扫描偏移（600，纯簿记）
│   └── audit.jsonl      管理操作审计
├── instances/<id>/      每个 agent 一个 home（config.toml, data/, workspace/；
│                        选了版本的 agent 还有私有源码副本 xuseek-v2/）
├── instances/.trash/    删除后的遗留（管理员自行清理）
├── versions/            xuseek-v2 版本仓库（不入 git；管理员投放 zip，约定见 docs/versions.md）
└── docs/                api.md（管理面 API 文档）· mission-examples.md（实验任务）
```

## 运维要点

- **掉线保护（两层）**：① agent 单元 `Restart=always`——崩溃/误杀 5s 内自动拉起；
  ② 管理面启动时 reconcile——机器重启后按注册表期望态（running/stopped/paused）拉齐。
- **暂停** = SIGSTOP 冻结大脑（它自起的后台服务继续跑）；停止/重启一律优雅停，
  轮边界把会话落盘后再退。
- **改参边界**：管理面只能改簿记（name/note）与进程监听（port/expose，需重启）。
  mission / 大脑 / 预算在创建后归 **agent 自治**——投信让它自己改 config.toml
  （内核每口呼吸热重载；改前建议让 agent 自行备份）。
- **密钥轮换**：改 `etc/brains.toml` → 投信把新 key 给 agent → agent 自己改 config。
- **备份**：停止态可用；运行中为 SIGSTOP 冻结窗快照（jsonl 均为追加型文件，一致性
  风险低）。agent 自己的凭证文件（webui_tokens.json）不进备份包，恢复后由 agent
  自行重建。
- **xuseek-v2 版本仓库**：管理员把版本源码打包投放 `versions/xuseek-v2-<版本号>.zip`
  （打包方法见 `docs/versions.md`；**整个 versions/ 目录不入 git**——zip 含私有源码）。
  versions/ 是源码**唯一事实源**：新建 agent **一律**从版本仓库解压成实例私有副本
  （`instances/<id>/xuseek-v2/`，各自构建 .venv，实例间互不影响，实例目录自洽**可单独
  迁移**，删除时随 home 进 .trash），实际版本记入注册表、创建后不可改。仓库为空时
  无法创建 agent（doctor 会 FAIL 提醒投放）。
- **删除**：必须先显式停止（运行/暂停态拒绝删除，防误删）→ 注册表除名 →
  目录移入 `instances/.trash/`（遗留数据由管理员自行 rm）。
- **暴露面**：`expose=true` 让 agent 直接监听 `0.0.0.0:<port>`——对外的访问凭证
  由 agent 自己管理，谨慎开启。
- **admin token 轮换**：`xusi init --rotate` 或改 `etc/xusi.toml` 的 `[admin].secret` →
  `systemctl --user restart xusi`，浏览器重新登录一次。

## 与 xuseek-v2 的关系

- **xuseek-v2**：agent 的源码与运行时（`--home` 挂接 `instances/<id>`，目录即自主体）。
  xusi 只在创建时渲染一次 `config.toml`（出生配置：mission/brains/api_key/budgets），
  此后该文件、该目录里的任何东西都归 agent 自己（唯一例外：详情页只读观察在
  `data/webui_tokens.json` 缺失时自动补签一枚 `xusi-observe` token，merge 不覆盖）。
