# 墟司（xusi）—— xuseek 智能体管理面

> 一个自洽目录，管理多个墟寻（xuseek-v2）自主体：创建/启停/暂停/删除。
> **xusi 与 agent 之间只有一条写通道：管理邮箱**——投信/收信；只读观察收窄为
> 两条（详情页事件流/会话 banner：GET /v1/events·status，token 缺失自动签发；
> 会话索引读磁盘 sessions.jsonl）。
> **彻底本地化管理**：互联由 xuseek 内核自己完成（根智能体 + `[[roots]]` 出生
> 交割，见内核 `docs/interconnect.md`）——xusi 不参与、不设公告板。
> agent 的对外呈现（观察台、自建服务）也是 agent 自家业务，xusi 不参与。

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

外部接入方式见 [`docs/api.md`](docs/api.md)（也在线提供：`GET /api/docs.md`）。
小型实验任务的现成 mission 见 [`docs/mission-examples.md`](docs/mission-examples.md)。

## 凭证设计（单档）

只有一档凭证：**admin token** = `etc/xusi.toml` 的 `[admin].secret`，
由 `xusi install` / `xusi init` 生成。它通吃所有 `/api/*` 端点。

agent 侧的凭证（观察台、自建服务、根智能体等）全部由 agent 自己管理，xusi 不签
不发不撤不碰——那是 xuseek 自家业务。例外之一：**详情页只读观察**（/v1/events、
/v1/status）在 `data/webui_tokens.json` 缺失时，xusi 自动签发一枚 `xusi-observe`
token 写进该文件（merge 不覆盖，内核免重启生效）；例外之二：**根智能体 token**
（创建时按管理员输入经 `[[roots]]` 渲染进出生 config，内核首次启动一次性交割到
`workspace/playbook/根智能体.json`，此后死键）——xusi 只是把它抄进出生配置的信使。

**xusi 不再签发、不再撤销任何其它 agent 侧的 token。**

## 互联（xuseek 自家业务）

互联由 xuseek 内核自己完成（v2.7.12+）：根智能体提供目录服务，实例间两两直连，
协议见内核 `docs/interconnect.md`。xusi 只做一件与互联有关的事——**创建时把管理员
给的根地址与 token 抄进出生 config 的 `[[roots]]` 段**（WebUI「新建 agent」对话框
可选；仅 v2.7.12+ 内核）。缺省根写在 `etc/xusi.toml` 的 `[[default_roots]]`
（模板见 `etc/xusi.toml.example`）——创建对话框自动预填、可删改。此后互联的
一切（目录、登记、token 轮换、断线恢复）都与 xusi 无关；存量 agent 要接入互联，
投信把根地址与 token 发给它让它自己加段。

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
出生配置，此后归 agent 自治；**唯一例外**：改参接口按密钥池手术式重渲染
`[brain]`+`[brains.*]` 段，下次呼吸生效，其余段绝不触碰）；只读观察两条
HTTP GET（/v1/events、/v1/status——详情页事件流/工具统计/会话 banner，
token 缺失自动签发），会话索引与 Boot 自述读磁盘：

- **投信**：追加 `<home>/data/mailbox.jsonl`（sender=admin，与内核 post() 同语义，
  双写 mailbox_log.jsonl 保历史；daemon 5s 轮询唤醒）；
- **收信**：读 `<home>/data/outbox.jsonl`（内核 send_mail 工具写，sender=brain；
  只读展示，无后台处理）。

systemd 进程与信号（spawn/stop/SIGSTOP/SIGCONT/journalctl）是宿主职责，不算通信。

## 目录

```
xusi/
├── xusi/                管理面源码（Python 3.12，stdlib + fastapi/uvicorn）
│   ├── api/             路由（agent_routes / backup_routes / meta_routes / auth / models）
│   ├── agentops.py      agent 全生命周期 + 投信/收信（邮箱写通道）+ 只读观察与会话
│   ├── systemdctl.py    systemd 用户单元封装（spawn 注入 PyPI 镜像 env）
│   ├── registry.py      注册表（agent 簿记 + 期望态）etc/agents.json（600）
│   ├── brains.py        密钥池 → 创建时渲染一次 agent config.toml（厂段 models=[...] 展开为每模型一个平级条目）
│   ├── ports.py         端口三重检验（注册表∪内核监听∪bind试探）
│   ├── authtok.py       管理面凭证（verify(admin token) → rec）
│   ├── backup.py        本地备份（SIGSTOP 冻结窗快照）
│   └── webui/           单文件管理页
├── etc/
│   ├── xusi.toml        监听/端口段/版本仓库路径 + [admin].secret（admin token）
│   ├── brains.toml      主密钥池（管理员维护；600，模板见 brains.toml.example；
│   │                     一家多模型写 models=[...]，展开为平级大脑（不分级））
│   ├── agents.json      注册表（agent 簿记 + 期望态 + 创建快照）
│   └── audit.jsonl      管理操作审计
├── instances/<id>/      每个 agent 一个 home（config.toml, data/, workspace/；
│                        选了版本的 agent 还有私有源码副本 xuseek-v2/）
├── instances/.trash/    删除后的遗留（管理员自行清理）
├── versions/            xuseek-v2 版本仓库（不入 git；管理员投放 zip，约定见 docs/versions.md）
└── docs/                api.md（管理面 API 文档）· mission-examples.md（实验任务）
```

## 运维要点

- **掉线保护（两层）**：① 进程载体 `Restart=always` / 容器 `unless-stopped`——
  崩溃/误杀 5s 内自动拉起；② 管理面启动时 reconcile——机器重启后按注册表
  期望态（running/stopped/paused）拉齐。
- **双运行时**：每个 agent 可跑 systemd 直跑（默认）或 docker 容器（host 网络，
  需内核 ≥ v2.7.19 + docker 环境），界面一致、仅多「容器/系统」徽章。
  **切换 = 停止 → 改参 → 启动**（状态全在实例目录，只换进程载体）；容器模式的
  镜像 tag 含内核版本，升级内核自动重建（构建含内核 selftest 门禁）。前置、
  目录布局与排障见 `docs/container-runtime.md`。
- **暂停** = SIGSTOP 冻结大脑（它自起的后台服务继续跑；容器模式同语义——
  exec 进容器只冻 daemon 主进程）；停止/重启一律优雅停，轮边界把会话落盘后再退。
- **改参边界**：管理面可改簿记（name/note）、暴露开关（expose，需重启）、
  运行时（runtime——须停止态，切换后不自动启动）与大脑成员/默认
  （brains——手术式重渲染 config.toml 的 [brain]+[brains.*] 段，
  **下次呼吸生效、不重启**；其余段逐字节保留）。**端口创建后固定**——agent
  对外联络 = ip+port，改端口等于换地址，要换只能删了重建（或克隆）。mission /
  预算在创建后归 **agent 自治**——投信让它自己改 config.toml（内核每口呼吸
  热重载；改前建议让 agent 自行备份）。
- **密钥轮换**：改 `etc/brains.toml` → 对 agent 做一次 PATCH（改 brains 或任意
  字段触发重渲染）→ 下次呼吸生效。卡片上点大脑 chip 可直接切换默认。
- **备份**：停止态可用；运行中为 SIGSTOP 冻结窗快照（jsonl 均为追加型文件，一致性
  风险低；容器模式同走冻结窗）。备份 meta 记 runtime 随包走（旧包默认 systemd；
  docker 备份恢复到无 docker 机器会早失败）。agent 自己的凭证文件
  （webui_tokens.json）不进备份包，恢复后由 agent 自行重建。
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
- **只读观察依赖 httpx**：仅观察通道（events/status）用到，函数内惰性 import——
  venv 缺它只废观察、其余功能不受影响；`pip install httpx` 补齐。
- **WebUI 渲染纪律**：agent 的一切输出（会话总结/事件字段/来信）都是不可信内容——
  详情页全量 `esc()` 转义后才进 DOM，新增展示字段不得例外（agent 可写自己的
  data/，不转义就是 agent → 管理员浏览器的存储型 XSS 通道）。
- **admin token 轮换**：`xusi init --rotate` 或改 `etc/xusi.toml` 的 `[admin].secret` →
  `systemctl --user restart xusi`，浏览器重新登录一次。

## 与 xuseek-v2 的关系

- **xuseek-v2**：agent 的源码与运行时（`--home` 挂接 `instances/<id>`，目录即自主体）。
  xusi 只在创建时渲染一次 `config.toml`（出生配置：mission/brains/api_key/budgets +
  可选 [[roots]] 根智能体段 + 可选的附加配置自由 TOML），此后该文件、该目录里的
  任何东西都归 agent 自己。三个例外：① 改参按密钥池
  手术式重渲染 `[brain]`+`[brains.*]` 段（下次呼吸生效，agent 在这两段的手改
  会被覆盖）；② 详情页只读观察在 `data/webui_tokens.json` 缺失时自动补签一枚
  `xusi-observe` token（merge 不覆盖）；③ 投信追加 `data/mailbox*.jsonl`。
