# 多副本远程零管理（remote）

> 控制端 fan-out：多台远端 xusi 的批量管理，纯 ssh/scp。讨论稿：
> `/home/ubuntu/work/讨论稿-远程零管理.md`。

**形态**：远端机器是「零管理」的哑执行机——`~/work/xusi` 自洽目录由控制端推送维护，
没有 venv、没有常驻服务、没有监听端口、没有 git pull。控制端（本机 repo）是
全队唯一的事实源：代码、内核版本仓库、机器清单都在这里。

```
控制端（唯一有代码、有思想的地方）                      远端（哑执行机 × N）
/home/ubuntu/work/xusi ──scp 代码tar(约0.5MB)──▶ ~/work/xusi/ （代码+数据自洽目录，唯一落盘）
 WebUI「远端机器」页 / xusi remote <cmd> ──ssh──▶ python3.12 -m xusi <cmd>
 etc/hosts.toml（清单，600，页面与 CLI 同源）   agent 单元由用户 systemd 守护（linger 已开）
```

远端唯一依赖：sshd + python3.12+ + systemd（+ docker，仅容器运行时）。
CLI 全路径纯标准库——fastapi/httpx 只有 `serve` 用，远端不需要它们。

## 1. 机器清单 `etc/hosts.toml`

控制端仓库的 `etc/`（gitignored、600）。WebUI「远端机器」页与 CLI 同源同一份文件：

```toml
[[host]]
name = "VM-0-8-ubuntu"          # 显示名（remote --on 用它）
host = "43.163.222.19"
port = 22
user = "ubuntu"
password = "Ht430022"           # 先明文（文件 600）；也可 key = "~/.ssh/id_ed25519"
# dir = "~/work/xusi"                # 远端自洽目录（缺省 ~/work/xusi）
# python = "python3.12"         # 远端 python（缺省 python3.12）
# brains = "seeds/vm0-brains.toml"  # 该机密钥池播种文件（缺省 = 控制端自己的 etc/brains.toml）
```

安全要点：
- 控制端**不存 admin token**——远端 CLI 直调进程内函数，鉴权就是 ssh 登录。
- 推送 tar 结构性排除 `etc/` `instances/` `.git` `.venv`——数据目录永远不可能被覆盖。
- ssh 首连 `accept-new` 入 known_hosts，之后自动验证指纹。

## 2. 三步工作流

### ① 接入新机

```bash
# 1. 把机器写进清单（WebUI「远端机器」页，或手编 etc/hosts.toml）
# 2. 一条命令接入
python3 -m xusi remote install --on VM-0-8-ubuntu
```

install 四步（幂等，已就绪的跳过）：
1. `python3.12` + `python3.12-venv`（deadsnakes PPA——与主流一致、可持续升级）
2. `loginctl enable-linger`（ssh 断开会话死 → agent 单元死的坑）
3. 推代码 tar（xusi/ + docs/ + versions/——内核版本仓库随之发布）
4. 播种密钥池 brains.toml（600）+ `doctor --mode cli` 自检

前置：sudo 免密（或密码同登录密码，install 用 `echo | sudo -S`）。

### ② 日常管理

```bash
python3 -m xusi remote status                # 全队 agent 一览（--json 供脚本）
python3 -m xusi remote create --on VM-0-8-ubuntu \
    --name 探针 --mission @mission.txt --brains glm
python3 -m xusi remote stop --on VM-0-8-ubuntu agent-xxxx
python3 -m xusi remote delete --on VM-0-8-ubuntu agent-xxxx
python3 -m xusi remote mail --on VM-0-8-ubuntu agent-xxxx "注意休息"
python3 -m xusi remote mailbox --on VM-0-8-ubuntu agent-xxxx
python3 -m xusi remote observe-token --on VM-0-8-ubuntu agent-xxxx  # 观察台 token
python3 -m xusi remote doctor               # 全队自检（--on 单机）
```

`remote create` 的参数与本地 `xusi create` 完全相同（`--on` 放最前；`@file` 参数
自动 scp 到远端）；`--spec agent.json` 与 `POST /api/agents` body 同构。

### ③ 升级 / 迁移

```bash
python3 -m xusi remote upgrade --on VM-0-8-ubuntu   # 重推代码 tar
# （控制端 versions/ 放了新内核 zip 后 upgrade 一次 = 全队内核版本发布）

python3 -m xusi remote backup --on VM-0-8-ubuntu agent-xxxx
# → etc/remote-backups/<机器名>/<包名>.tar.gz
python3 -m xusi remote restore --on 另一台 --from 包路径 [--new-id …] [--port …]
```

## 3. 远端机器上的样子

```
~/work/xusi/          代码 + 数据自洽目录（agent 单元 ExecStart 锚定这里）
  xusi/          管理面代码（控制端推来，本机零维护）
  docs/ versions/
  etc/           brains.toml（密钥池）、agents.json（注册表）、backups/
  instances/     agent 家目录
```

没有 xusi.service、没有 8601 端口。管理员登上去也只有一件事可做——看
`python3.12 -m xusi status`；一切操作都从控制端发。

## 4. WebUI 远程总控

控制端 WebUI 顶栏有**看板范围下拉框**（本机 / 每台远端机器）与「⛭ 远端机器」
配置页（hosts.toml 同源维护）。切到某台远端机器后：

- **卡片网格**渲染该机全部 agent（状态/运行时/大脑/端口同款徽章），卡片操作
  （启动/停止/暂停/重启/删除/备份/详情）全部经控制端中转 ssh 作用于远端；
- **「＋ 新建 agent」**对话框选「目标机器」= 远端时，提交即远端创建
  （同步等待 1-3 分钟；从备份克隆暂只支持本机）；
- **未接入的主机**显示空态 + 「一键接入」按钮（2-5 分钟，幂等）；
- **远端详情抽屉**：状态 + 信箱（投信/收信）+ 会话索引——全是文件通道；
  事件流/工具统计/Boot/日志不反代，「观测台 ↗」直连远端 agent 端口
  （浏览器需可达该机器，token 由管理面现取）。
- 刷新节奏：本机 15s / 远端 30s。

浏览器全程只与控制端 :8601 说话，不直连远端、不存远端凭据。

## 5. 链路竞速与连接复用

海外机器直连实测：新建 ssh 连接 2-4s（握手+认证），复用通道单命令 ~0.7s。
连接层做了两件事：

- **ControlMaster 保温连接**：每台机器每条链路一条长连接（ControlPersist=120s），
  后续命令全部免握手。看板切换从 4-8s 降到 ~0.7s（多条命令合并后）。
- **链路竞速**：每台机器可配多个链路候选，并行计时探测（冷连接全程），最快者
  当选并缓存 `etc/link_cache.json`（TTL 10 分钟）；连接层失败（ssh rc 255/超时）
  使缓存失效，下一次调用重新竞速——代理哪天变快会自动换过去。写命令不做自动
  重试（避免双发），由 WebUI 轮询自然重试。

```toml
[[host]]
name = "VM-0-8-ubuntu"
host = "43.163.222.19"
user = "ubuntu"
password = "Ht430022"
proxy = "socks5h://127.0.0.1:1080"   # 候选链路：本机 socks5h/socks5/http 代理（需 nc）
# via = "HK-relay"                   # 或经清单里另一台机器做 ssh 跳板（双密码支持）
```

只配直连的机器不探测；`proxy`/`via` 配了才参与竞速（WebUI「远端机器」页有代理列）。
传输也不走 scp：上传/下载都是 ssh+cat 单往返，免第二条连接。

## 6. 存量部署收编（自动化）

已有本地墟司部署的机器（有 .venv、有 serve、有 agent）不用重装——清单里加
条目（name/host/user/password 即可），然后一条命令收编：

```bash
python3 -m xusi remote adopt --on tx-bj-3
```

或 WebUI：范围切到该机器，空态会显示「**一键收编**」（status 探测到 serve 单元
的 WorkingDirectory 即报告 `adoptable_root`，与「一键接入」按钮区分）。

`adopt` 自动化四步（幂等，可重跑）：
1. **探测部署根**：serve 单元的 WorkingDirectory 优先（serve 已停的机器单元
   文件仍在），退而查常见路径
2. **回写清单**：dir/python 自动补进 hosts.toml（与缺省一致的字段不写，清单最小）
3. **升级**：git checkout → git pull；否则推 tar（与 `remote upgrade` 同一分派）
4. **停 + 禁 serve（单头原则）**：`systemctl --user disable --now xusi.service`
   ——避免双头管理（本机 WebUI 改动不进控制端 audit）与 reconcile 线程和远程
   操作在「停单元→改期望态」窗口互相打架。agent（systemd 单元/docker 容器）
   不受影响；5. doctor 验证。

既有 agent 零迁移：注册表/实例目录原样接管，收编后出现在全队 status。

```toml
[[host]]
name = "tx-bj-3"
host = "49.232.26.253"
user = "ubuntu"
password = "…"
dir = "/home/ubuntu/work/xusi"      # adopt 自动回写（与缺省一致时不写）
python = ".venv/bin/python"         # adopt 自动回写（venv 存在时）
```

## 7. 已知边界

- **观察闭环**：CLI-only 机器没有 serve 的 observe token 自动签发——用
  `remote observe-token` 手动签发（写 data/webui_tokens.json，内核每请求重读）。
- **agent 变更随呼吸生效**：remote 的 CRUD 与 WebUI/API 是同一套 agentops 实现，
  语义完全一致（创建渲染一次出生 config，此后归 agent 自治）。
- **控制端是单点**：控制端 repo 丢 = 管理能力停（agent 本身不受影响，单元照跑）。
  控制端自己照常 `xusi backup --all` 自保。
- 密码先明文阶段：清单 600；接口 GET /api/hosts 会回明文（admin 鉴权）。后续
  若要收紧，改成「只写不回显」并建议 key 优先。
