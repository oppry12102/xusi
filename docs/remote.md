# 多副本远程零管理（remote）

> 控制端 fan-out：多台远端 xusi 的批量管理，纯 ssh/scp。讨论稿：
> `/home/ubuntu/work/讨论稿-远程零管理.md`。

**形态**：远端机器是「零管理」的哑执行机——`~/xusi` 自洽目录由控制端推送维护，
没有 venv、没有常驻服务、没有监听端口、没有 git pull。控制端（本机 repo）是
全队唯一的事实源：代码、内核版本仓库、机器清单都在这里。

```
控制端（唯一有代码、有思想的地方）                      远端（哑执行机 × N）
/home/ubuntu/work/xusi ──scp 代码tar(约0.5MB)──▶ ~/xusi/ （代码+数据自洽目录，唯一落盘）
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
# dir = "~/xusi"                # 远端自洽目录（缺省 ~/xusi）
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
~/xusi/          代码 + 数据自洽目录（agent 单元 ExecStart 锚定这里）
  xusi/          管理面代码（控制端推来，本机零维护）
  docs/ versions/
  etc/           brains.toml（密钥池）、agents.json（注册表）、backups/
  instances/     agent 家目录
```

没有 xusi.service、没有 8601 端口。管理员登上去也只有一件事可做——看
`python3.12 -m xusi status`；一切操作都从控制端发。

## 4. 已知边界

- **观察闭环**：CLI-only 机器没有 serve 的 observe token 自动签发——用
  `remote observe-token` 手动签发（写 data/webui_tokens.json，内核每请求重读）。
- **agent 变更随呼吸生效**：remote 的 CRUD 与 WebUI/API 是同一套 agentops 实现，
  语义完全一致（创建渲染一次出生 config，此后归 agent 自治）。
- **控制端是单点**：控制端 repo 丢 = 管理能力停（agent 本身不受影响，单元照跑）。
  控制端自己照常 `xusi backup --all` 自保。
- 密码先明文阶段：清单 600；接口 GET /api/hosts 会回明文（admin 鉴权）。后续
  若要收紧，改成「只写不回显」并建议 key 优先。
