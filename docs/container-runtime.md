# 容器运行时（docker）—— agent 的第二种跑法

> xusi 每个 agent 可选两种运行时：**systemd**（默认，系统进程）与 **docker**
> （容器，host 网络）。两种方式在界面上完全一致，仅多一个「容器/系统」徽章；
> 切换 = **停止 → 改参 → 启动**。本文是容器模式的运维手册（内核侧的镜像设计
> 见 xuseek-v2 的 `DOCKER.md` / `docs/container.md`）。

## 前置

1. 内核版本：**xuseek-v2 ≥ v2.7.38**（入口 shim `docker-entrypoint.sh` 自该
   版本起才有——shim 优先跑实例目录自己的 `xuseek.sh`，是现行 compose 模板的
   硬前提；旧内核镜像直跑 `/app` 副本会静默跑旧代码）。旧内核创建 docker
   agent 直接 400；存量 agent 先走 `docs/kernel-upgrade.md` 升级。
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
│   └── xuseek-v2/              内核私有副本：运行的代码（shim 直跑）+ build context
│       └── .venv/              首启自建 venv（bind mount 持久；丢了自愈重装一次）
└── .compose/
    └── xusi-a-<agent-id>/compose.yaml   管理面渲染（容器内不可见）
```

- **compose.yaml 由管理面渲染在实例目录之外**（`instances/.compose/<unit>/`，
  600 权限）——容器只挂载了实例根 `/data`（跑的代码就在里面），渲染文件不在
  挂载里，容器内大脑看不到也改不到。
- **容器运行用户 = 管理面用户**（compose 的 `user:` 行，缺省取管理面进程的
  uid + 主组 gid；`[manager].docker_user` 可改）。内核模板默认 root，但 root
  写进 `/data` 的文件宿主属主是 root——管理面（普通用户）就写不了
  mailbox.jsonl / webui_tokens.json（投信与观察台 token 签发直接断）。
  钉成管理面用户后：容器内大脑的能力与 systemd 模式**完全对齐**（同 uid，
  含可改自己那份内核代码），落盘文件属主一致。显式设 `"0:0"` 即恢复
  容器内 root（大脑近似宿主 root——对应隔离讨论的 root 档，谨慎使用）。
- **低端口绑定靠宿主 sysctl，不是 cap_add**：普通用户 bind 1024 以下端口
  会被内核拒绝。试过 compose `cap_add: NET_BIND_SERVICE`——但 Linux 语义上
  `--cap-add` 只扩大 bounding set，非 root 进程 CapEff 仍为 0，实测无效已
  回退。正确做法是取消「特权端口」概念：宿主
  `sysctl net.ipv4.ip_unprivileged_port_start=0`（持久化
  `/etc/sysctl.d/99-unprivileged-ports.conf`；`remote install` ⑤ 缺省铺好，
  `xusi doctor` 有对应体检项）。host 网络下容器内普通用户即可直听 80/443，
  无需任何 capability 或转发规则。
- **spawn 每次重渲染**：路径/端口/镜像 tag 恒与注册表一致（expose 切换后
  `--host` 变化自然生效）；不要手改——手改的内容下次 spawn 就被覆盖。
- **镜像 fleet 共享**：`xuseek:<version>`（与实例内容解耦——跑的是实例目录
  自己的 launcher/源码/`.venv`，镜像只是 python/uv/系统工具 + 兜底副本）。
  同版本 N 个实例只构建一次，第二个起免构建；升级内核**不重建镜像**。容器是
  可弃的一次性运行时，实例状态全在 bind mount，重建/升级零状态损失。

## 创建 / 切换

- 创建：对话框「运行时」选 Docker 容器（或 API `runtime:"docker"`）。
  首次 spawn 时若镜像缺失会**同步构建**（分钟级；构建含内核 selftest 门禁，
  失败即 spawn 失败并带构建输出尾部）——构建在 up 之前完成，不挤占验收窗
  （docker 档 360s，首启自建 venv 也在窗内）。
- 切换：**停止 → 改参选运行时 → 启动**。运行中切换会被 400 拒绝（新旧载体
  会抢同一端口）；切换时旧载体防御性清理（docker → `compose down` + 清渲染
  目录；镜像 fleet 共享、不按实例删），切换后不自动启动。双向都可切——状态
  全在实例目录，换载体不丢任何东西。
- 切换**零预处理**：两种运行时同用实例目录里那条 `xuseek-v2/.venv`，解释器
  失配（宿主 ↔ 镜像 python）由首启自愈识别并原地重建（实测容器方向 6.1s、
  宿主方向 16.2s，含全量依赖重装；指纹确定性往返）。历史上「切到 docker 要
  删宿主 venv」的保护（c65dd09，防真目录遮蔽 v2.7.37 兼容软链）已随软链
  时代退役——现在删了反而毁掉可直接平移的环境缓存。

## 语义对齐（与 systemd 模式逐项对照）

| 事项 | systemd | docker |
|---|---|---|
| 崩溃自动拉起 | `Restart=always` | `restart: unless-stopped` |
| 停止 | 瞬态单元回收 | `compose down`（容器回收，镜像保留） |
| 暂停/续跑 | SIGSTOP/SIGCONT 主进程 | **同语义**：exec 进容器只冻 daemon 主进程（不用 `docker pause`——那会连大脑自起的服务一起冻） |
| 日志 | journalctl | `docker logs --tail`（json-file 10m×3 轮转，防写穿磁盘） |
| 备份冻结窗 | SIGSTOP/SIGCONT | 同走冻结窗（按 runtime 分派） |
| 优雅停 | TimeoutStopSec=20 | `stop_grace_period: 30s` |
| `.venv` 解释器路径 | `<实例>/xuseek-v2/.venv`（真 venv，xuseek.sh 首启自建） | **同一个东西**（同路径同内容；解释器失配自愈原地重建） |

UI 上的状态徽章/自动重启次数/在线时长/暂停徽章全部走同一份 `process` 字段
形状——前端零差异。`.venv` 在两个运行时下是**同一个东西**（同路径同内容，
不再是 v2.7.37 的兼容软链层）：大脑自起脚本（watchdog/采集器）硬编码的解释器
路径恒有效；`rm -rf .venv` 也只是「重装一次环境」（实测 17~51s 自愈，数据
零损伤）。

## 内核升级（容器版）

**`docs/kernel-upgrade.md` 的 playbook 原样可用**：停机 → 解压新版本目录 →
rename → 改注册表 `source_version` → spawn。**镜像不动**（launcher/源码/
`.venv` 都随实例目录，跑的从来不是镜像里的代码）——「忘重建镜像」这一整类
事故从结构上消失。`.venv 平移`两种运行时同用（都是真目录，`mv` 进新树即可；
依赖变了自愈补装，python 没变则秒级）。镜像重建仅当换基础镜像（python
版本）或系统依赖变化——此时实例 venv 因 python 小版本变自动重装（正确行为，
无需干预）。旧镜像留盘不碍事，`docker image prune` 统一清理。

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
- **大脑改内核代码**：改的就是自己实例目录里的 `xuseek-v2/xuseek`（shim
  优先跑这份）——重启生效、镜像无关、改坏只影响它自己；改 `pyproject.toml`
  加依赖也不用碰镜像：venv 自愈按新依赖清单现装（见内核 DOCKER.md）。
- **容器内 pip 装包**：完全缺省语义——装进实例自己的 `.venv`，`pip list`/
  `uninstall` 正常，daemon 与子进程处处可导。历史上的「三件套」方案
  （`HOME=/data` + `PIP_TARGET` + `PYTHONPATH`，2026-09-10~09-11 短暂上线）
  已随 v2.7.38 入口 shim 退役（始末见 `docs/agent-lifecycle.md`）。`HOME`
  仍钉 `/data`：uv/pip 缓存落 bind mount（`/data/.cache/uv`），也避开裸
  user 起容器时 Docker 落 `HOME=/` 的坑；`pip --user` 在 venv 里仍是死路
  （venv 禁 user-site），报错可行动（去掉 `--user` 即可）。
