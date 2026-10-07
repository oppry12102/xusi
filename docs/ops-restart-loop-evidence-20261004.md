# 服务空转重启证据:xuseek(8299)与 miro-v2-deepsearch(8710)

调查日期:2026-10-04。起因:排查 9/30 晚死机重启(结论另案:**内存 MCE/CMCI 硬件风暴**,与本两案无因果)时,
发现这两个服务在每次开机周期内以 5 秒级节奏无限重启,单周期累计数万至十几万次。

---

## 一、xuseek.service(端口 8299):双注册抢端口

### 结论

同一个部署(`WorkingDirectory=/home/htao/work/xuseek`)被注册了**两份同名单元**:系统级与用户级(htao)。
开机后两者竞争绑定 `0.0.0.0:8299`,先启动者占住端口,落败方以 `Restart=always/on-failure` + `RestartSec=5`
无限循环,直至下次关机。

### 占住端口的进程(当前健康实例)

```
$ sudo ss -tlnp | grep 8299
LISTEN 0  2048  0.0.0.0:8299  0.0.0.0:*  users:(("python",pid=1326,fd=13))

$ ps -o pid,lstart,user,cmd -p 1326
1326  Wed Sep 30 21:05:10 2026 htao  /home/htao/work/xuseek/.venv/bin/python main.py server --host 0.0.0.0 --port 8299
$ sudo cat /proc/1326/cgroup
0::/system.slice/xuseek.service          ← 系统级单元的进程
```

### 两份单元定义对照

| | 系统级 | 用户级(htao) |
|---|---|---|
| 单元文件 | `/etc/systemd/system/xuseek.service` | `/home/htao/.config/systemd/user/xuseek.service` |
| Description | 墟寻 (xuseek) 主服务 — 本地 8299 HTTP API | xuseek —— LLM 主导的探索回路(HTTP API + WebUI) |
| WorkingDirectory | `/home/htao/work/xuseek` | 同左 |
| ExecStart | `.venv/bin/python main.py server --host 0.0.0.0 --port 8299` | 同左 |
| 重启策略 | `Restart=on-failure, RestartSec=5` | `Restart=always, RestartSec=5` |
| 自启途径 | 系统启动 | htao 开启 lingering(见 `/var/lib/systemd/linger/htao`),user manager 开机必拉起 |

### 失败方日志特征(循环体)

```
systemd[1331]: Started xuseek.service - xuseek —— LLM 主导的探索回路(HTTP API + WebUI)
python:  INFO:     Application startup complete.
python:  ERROR:    [Errno 98] error while attempting to bind on address ('0.0.0.0', 8299): address already in use
python:  INFO:     Application shutdown complete.
systemd[1331]: xuseek.service: Main process exited, code=exited, status=3/NOTIMPLEMENTED
systemd[1331]: xuseek.service: Scheduled restart job, restart counter is at NNNN
```

### 重启计数证据(按开机周期)

| 开机周期 | 循环方 | 管理器 | 计数(采样时刻) |
|---|---|---|---|
| boot -2(9/20 07:28 ~ 9/29 20:02) | 系统级 | systemd[1] | 94340(9/29 20:02:09) |
| boot -1(9/29 20:03 ~ 9/30 21:03) | 系统级 | systemd[1] | 8516(9/30 21:03:42) |
| boot 0(9/30 21:05 ~ 采样时) | 用户级 | systemd[1331] | 48963(10/4 07:24:12) |

boot 0 自洽性:开机至采样 82.3h × 实测 9~10 次/分 ≈ 4.7~4.9 万 ✓ 与计数 48963 吻合。
实测速率(60 秒窗口):9 次/分钟。

哪一方赢取决于开机竞速:boot -2/-1 是用户级占住了口(系统级空转),boot 0 反过来。

---

## 二、miro-v2-deepsearch.service(端口 8710):venv 解释器悬空

### 结论

单元的 `ExecStartPre`/`ExecStart` 经由
`/home/htao/work/xusi/instances/miro-v2-8b65/xuseek-v2/.venv/bin/python` 启动,该文件是指向
**`/usr/local/bin/python3.13`** 的符号链接,而目标解释器**已不存在**(系统现仅有 `/usr/bin/python3.12`,
`/usr/local/bin/` 下无任何 python)。因此每次启动在 EXEC 阶段即失败,状态 203/EXEC,
`Restart=always, RestartSec=5` 无限循环。**该服务自 9/20 前后起从未成功启动过一次。**

```
$ ls -la …/miro-v2-8b65/xuseek-v2/.venv/bin/python
lrwxrwxrwx htao htao 25 Sep 12 16:42 …/.venv/bin/python -> /usr/local/bin/python3.13
$ ls /usr/local/bin/python3.13
ls: cannot access '/usr/local/bin/python3.13': No such file or directory
```

### 单元定义要点

- 文件:`/home/htao/.config/systemd/user/miro-v2-deepsearch.service`(htao 用户级)
- `WorkingDirectory=/home/htao/work/xusi/instances/miro-v2-8b65/workspace`
- `ExecStart=…/miro-v2-8b65/xuseek-v2/.venv/bin/python services/server.py`(FastAPI @0.0.0.0:8710)
- `ExecStartPre=…/.venv/bin/python …/workspace/scripts/ensure_kimi_in_config.py`
- `Restart=always, RestartSec=5`

注:`/home/htao` 对 oppry 不可读,以上经 sudo 读取;203/EXEC 来自 "Control process"(即 ExecStartPre)。

### 失败方日志特征(循环体)

```
systemd[1331]: Starting miro-v2-deepsearch.service - miro-v2 deep-search —— 墟寻第二代深度搜索服务(FastAPI 直连 @0.0.0.0:8710,2026-08-31 反代取消)...
systemd[1331]: miro-v2-deepsearch.service: Control process exited, code=exited, status=203/EXEC
systemd[1331]: miro-v2-deepsearch.service: Failed with result 'exit-code'.
systemd[1331]: miro-v2-deepsearch.service: Scheduled restart job, restart counter is at NNNN
```

### 重启计数证据(按开机周期)

| 开机周期 | 管理器 | 计数(采样时刻) |
|---|---|---|
| boot -2 | systemd[1465] | 156757(9/29 20:02:07;9 天 × 5s 周期 ≈ 15.6 万,跑满) |
| boot -1 | systemd[1324] | 17152(9/30 21:03:46) |
| boot 0 | systemd[1331] | 56494(10/4 07:24:17) |

boot 0 自洽性:82.3h × 12 次/分 ≈ 5.9 万 ✓ 与计数 56494 吻合。实测速率:12 次/分钟(= 5 秒周期跑满)。

### 追加发现:即便修好解释器,还会撞 8710

```
$ sudo ss -tlnp | grep 8710
LISTEN 0  2048  0.0.0.0:8710  0.0.0.0:*  users:(("python",pid=6320,fd=11))
```

PID 6320(htao,自 9/30 21:05 起)运行 `/app/.venv/bin/python services/server.py`,已占用 8710——
功能上正是 deep-search 本身(疑似手工/其他途径拉起的替代实例)。修复解释器前需先厘清 8710 归属,
否则新实例将复刻 8299 的抢端口循环。

---

## 三、影响评估

1. **与 9/30 死机无因果**(死机为内存硬件 MCE 风暴,另案)。但两循环合计每天空转 ~10 万次进程创建,
   持续烧 CPU 并刷屏 journal——journal 因此膨胀,全量扫描需分钟级,是本次排查明显变慢的直接原因。
2. 端口现状(2026-10-04):8299 ✅ 健康(系统级 xuseek);8611 ✅(htao,`/data/xuseek-v2`,另部署);
   8710 ⚠️ 由来历待核实的 `/app` 进程占用。

## 四、处置建议(均涉 htao 家目录,建议先与其确认)

1. **xuseek 用户级副本**:保留系统级(带 `User=/NoNewPrivileges/Restart=on-failure`,更合理),禁用用户级:
   ```bash
   systemctl --user -M htao@.host disable --now xuseek.service
   ```
2. **miro-v2-deepsearch**:先核对 8710 上 `/app` 进程的来历与角色;若为替代实例,直接
   `disable --now` 本单元;若需恢复本单元,重装 python3.13 或用 python3.12 重建 venv(改大版本需重建 venv)。
3. 处置后验收:`journalctl -f | grep -E "xuseek|miro"` 应无循环;两个 restart counter 应停涨。
