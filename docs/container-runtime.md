# 容器运行时（docker）—— agent 的第二种跑法

> xusi 每个 agent 可选两种运行时：**systemd**（默认，系统进程）与 **docker**
> （容器，host 网络）。两种方式在界面上完全一致，仅多一个「容器/系统」徽章；
> 切换 = **停止 → 改参 → 启动**。本文是容器模式的运维手册（内核侧的镜像设计
> 见 xuseek-v2 的 `DOCKER.md` / `docs/container.md`）。

## 前置

1. 内核版本：**xuseek-v2 ≥ v2.7.19**（Dockerfile 自该版本起才有）。旧内核
   创建 docker agent 直接 400；存量 agent 先走 `docs/kernel-upgrade.md` 升级。
2. 本机 docker 环境：daemon + compose 插件；**管理面用户要能访问
   `/var/run/docker.sock`**（`sudo usermod -aG docker <管理面用户>` 后**重新
   登录**——组权限在会话启动时固定，只重启 xusi 服务不够）。
   `xusi doctor` 有对应检查；创建/切换时同样前置校验，失败给可行动提示。
3. 网络：仅 **host 网络**（Linux 服务器主路径）——容器直接绑宿主机真实端口，
   与端口段 1:1 分配零差异；大脑自起的服务直连局域网。bridge 备选不在首版。

## 目录布局

```
instances/
├── <agent-id>/                 实例目录（与裸机同一目录语义，迁移原样搬走）
│   ├── config.toml             （容器内 = /data/config.toml）
│   ├── data/  workspace/       （bind mount，容器写入即宿主可见）
│   └── xuseek-v2/              内核私有副本：build context + /app/xuseek 活挂载
└── .compose/
    └── xusi-a-<agent-id>/compose.yaml   管理面渲染（容器内不可见）
```

- **compose.yaml 由管理面渲染在实例目录之外**（`instances/.compose/<unit>/`，
  600 权限）——容器只挂载了实例根 `/data` 与内核代码 `/app/xuseek`，
  渲染文件不在任何挂载里，容器内大脑看不到也改不到。
- **容器运行用户 = 管理面用户**（compose 的 `user:` 行，缺省取管理面进程的
  uid + 主组 gid；`[manager].docker_user` 可改）。内核模板默认 root，但 root
  写进 `/data` 的文件宿主属主是 root——管理面（普通用户）就写不了
  mailbox.jsonl / webui_tokens.json（投信与观察台 token 签发直接断）。
  钉成管理面用户后：容器内大脑的能力与 systemd 模式**完全对齐**（同 uid，
  含可改自己那份内核代码），落盘文件属主一致。显式设 `"0:0"` 即恢复
  容器内 root（大脑近似宿主 root——对应隔离讨论的 root 档，谨慎使用）。
- **cap_add: NET_BIND_SERVICE**：普通用户 bind 1024 以下端口会被内核拒绝
  （agent-8e09 实测 Permission denied）。这个窄能力只放开特权端口绑定——
  大脑想对外服务（监听 80/443）不再依赖转发规则。加/删该行后下次 spawn
  重建容器即生效（镜像不变，重建秒级）。
- **spawn 每次重渲染**：路径/端口/镜像 tag 恒与注册表一致（expose 切换后
  `--host` 变化自然生效）；不要手改——手改的内容下次 spawn 就被覆盖。
- **镜像 tag 含内核版本**：`xuseek-agent-<id>:<source_version>`。容器是
  可弃的一次性运行时，实例状态全在 bind mount，重建/升级零状态损失。

## 创建 / 切换

- 创建：对话框「运行时」选 Docker 容器（或 API `runtime:"docker"`）。
  首次 spawn 时若镜像缺失会**同步构建**（分钟级；构建含内核 selftest 门禁，
  失败即 spawn 失败并带构建输出尾部）——构建不挤占 90s 验收窗
  （验收只量「容器 active + 端口监听」）。
- 切换：**停止 → 改参选运行时 → 启动**。运行中切换会被 400 拒绝（新旧载体
  会抢同一端口）；切换时旧载体防御性清理（docker → `compose down` + 清渲染
  目录；镜像保留），切换后不自动启动。双向都可切——状态全在实例目录，
  换载体不丢任何东西。

## 语义对齐（与 systemd 模式逐项对照）

| 事项 | systemd | docker |
|---|---|---|
| 崩溃自动拉起 | `Restart=always` | `restart: unless-stopped` |
| 停止 | 瞬态单元回收 | `compose down`（容器回收，镜像保留） |
| 暂停/续跑 | SIGSTOP/SIGCONT 主进程 | **同语义**：exec 进容器只冻 daemon 主进程（不用 `docker pause`——那会连大脑自起的服务一起冻） |
| 日志 | journalctl | `docker logs --tail`（json-file 10m×3 轮转，防写穿磁盘） |
| 备份冻结窗 | SIGSTOP/SIGCONT | 同走冻结窗（按 runtime 分派） |
| 优雅停 | TimeoutStopSec=20 | `stop_grace_period: 30s` |

UI 上的状态徽章/自动重启次数/在线时长/暂停徽章全部走同一份 `process` 字段
形状——前端零差异。

## 内核升级（容器版）

**`docs/kernel-upgrade.md` 的 playbook 原样可用**：停机 → 解压新版本目录 →
rename → 改注册表 `source_version` → spawn。区别只在拉起那一步——镜像 tag
变了 → 自动重建（含 selftest 门禁，比裸机多几分钟构建）。docker agent 跳过
`.venv 平移`步骤也安全（venv 烘培在镜像里，实例目录里没有 .venv）。旧镜像
留盘不碍事，`docker image prune` 统一清理。

## 降级表现与排障

- **docker daemon 挂掉**：docker agent 的状态查询返回 `unknown`（**不是**
  not-found——管理面据此区分「容器不存在」与「查不到」，删除/拉起不误判）；
  卡片显示已停止、日志读取出错文案，全部可读降级，systemd agent 完全无感。
  daemon 恢复后容器 `unless-stopped` 自动回活，reconcile 兜底。
- **构建失败**：输出尾部在错误信息里（含 selftest 失败点）；create 会全量
  回滚，start/reconcile 可重试。构建参数镜像源在 `etc/xusi.toml` 的
  `[manager]` 段（`docker_pip_index` / `docker_apt_mirror` / `docker_extras`）。
- **大陆机器新装 docker 的两个源坑**：① `docker pull` 基础镜像走 Docker Hub
  慢/不通——这是 daemon 级配置，xusi 代码管不到：`/etc/docker/daemon.json`
  配 `registry-mirrors` 后 `sudo systemctl restart docker`；② apt/pip 源由
  xusi 渲染进构建，缺省即国内镜像（apt 腾讯 / pip 清华），无需任何配置——
  海外机器显式设空串 `docker_apt_mirror = ""`、`docker_pip_index = ""` 关闭。
- **healthcheck unhealthy ≠ 停止**：host 网络下 healthcheck 打的是宿主机
  回环 `/v1/health`；暂停（SIGSTOP）期间它会失败但只标记 unhealthy，
  不会触发重启。
- **端口冲突**：host 网络 = 宿主机真实端口，与 systemd 同走注册表分配三重
  检验；最坏表现是内核绑不上 → 容器崩溃循环 + 验收超时（错误附日志尾部）。
- **大脑改内核代码**：`/app/xuseek` 活挂载自它自己的 `xuseek-v2/xuseek`——
  改的就是自己这份，重启生效、镜像重建不丢、改坏只影响它自己；改
  `pyproject.toml` 想持久新依赖要重建镜像（见内核 DOCKER.md）。
