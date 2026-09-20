# 内核 v2.7.79 上游修改建议（来自 xusi 创建流程重设计落地实测）

> 2026-09-20 · 墟司（xusi）管理面收敛到内核 v2.7.79（facts.db 事实账时代）时的
> 实测发现，供内核仓跟进。部署侧已临时修补（见 §4），**用户手里的原始 zip
> 未动**——上游修掉后重新出包即可覆盖。

## 问题 1（高，必须修）：selftest 条件登记 → 计数 off-by-one，镜像构建门禁必挂其一

### 现象

同一份 v2.7.79 源码跑 `xuseek selftest`：

- 在**跑着 serve 进程的机器**（开发机/裸机自检）：`共 479 项检查，失败 0 项`；
- 在**构建容器**（Dockerfile 的构建期门禁，无 serve）：`共 480 项检查，失败 1 项`
  ——失败项恰是元检查「文档自检计数与实测一致」：`README=479 架构=479 实测=480`。

结果：Dockerfile 里 `RUN ./xuseek.sh selftest` 这道升级门禁在构建容器里
**必挂**，xusi 的 docker 创建路径整体被卡死（2026-09-20 实测撞上）。

### 根因

`xuseek/selftest.py` sec_stall 里有一条 check 被**条件登记**：

```python
        # 陈旧脉搏 + serve 不在 → 放行（boot 残留不是停滞——重启风暴防线，
        # 一次性眼睛与常驻眼睛同一条状态判据）。本机真跑着 serve 时用例不可控，
        # 跳过（CI/构建容器里确定性执行）
        if _schk_mod._serve_alive():
            print("  - 判活脚本 boot 残留放行用例跳过：本机有 serve 在跑（环境不可控）")
        else:
            _r_residue = _sp_stall.run(...)
            check("判活脚本：陈旧脉搏 + serve 不在 → 放行（boot 残留不是停滞，不重启风暴）",
                  _r_residue.returncode == 0)
```

`check()` 无条件把名字追加进 `CHECKS`，元检查拿 `len(CHECKS) + 1` 与
README/架构文档里的固定数字 479 比对——条件登记让总数随环境漂移
（有 serve 479 / 无 serve 480），**两个环境必有一边对不上**。

内核自己早已有这份教训：`hard_fail()` 的 docstring 明说「环境探测类用例的真失败
用——失败进 FAILURES，但计数不进 CHECKS，否则元检查按总数对文档，有/无该环境
的机器永远对不上（off-by-one 之源）」。本处是同一教训的镜像违规：不是真失败，
而是把**登记**本身做成了条件。

### 建议修法（跳过执行、照常登记——与 hard_fail 同一条纪律）

```diff
--- a/xuseek/selftest.py
+++ b/xuseek/selftest.py
@@ -2755,10 +2755,12 @@
               _r_stale.returncode == 1
               and facts.last_of_type("session_stalled") is None)
         # 陈旧脉搏 + serve 不在 → 放行（boot 残留不是停滞——重启风暴防线，
-        # 一次性眼睛与常驻眼睛同一条状态判据）。本机真跑着 serve 时用例不可控，
-        # 跳过（CI/构建容器里确定性执行）
+        # 一次性眼睛与常驻眼睛同一条状态判据）。本机真跑着 serve 时用例不可控：
+        # 跳过**执行**但照常登记（计数必须稳定——check() 无条件进 CHECKS，
+        # 条件登记会让文档计数检查在有/无 serve 的机器上 off-by-one 互斥失败）
         if _schk_mod._serve_alive():
             print("  - 判活脚本 boot 残留放行用例跳过：本机有 serve 在跑（环境不可控）")
+            check("判活脚本：陈旧脉搏 + serve 不在 → 放行（boot 残留不是停滞，不重启风暴）", True)
         else:
             _r_residue = _sp_stall.run([sys.executable, str(_schk), str(cfg.home), "1800"],
                                        capture_output=True, timeout=30)
             check("判活脚本：陈旧脉搏 + serve 不在 → 放行（boot 残留不是停滞，不重启风暴）",
                   _r_residue.returncode == 0)
```

配套（计数从此恒为 480）：

```diff
--- a/README.md
+++ b/README.md
@@ -31,7 +31,7 @@
-自检（无网络，479 项契约测试）：
+自检（无网络，480 项契约测试）：

--- a/docs/architecture.md
+++ b/docs/architecture.md
@@ -315,7 +315,7 @@
-xuseek selftest    # 479 项，全程无网络；所有落盘在临时 home，不碰真实实例
+xuseek selftest    # 480 项，全程无网络；所有落盘在临时 home，不碰真实实例
```

## 问题 2（低，建议修）：入口 shim 的 stall_watch 定位不含 xusi 布局

### 现象

xusi 布局（实例内核副本在 `<home>/xuseek-v2/`）的容器里，stall_watch 恒跑
镜像副本而非实例自己的那份：

```
$ docker exec xusi-a-<id> ps aux | grep stall
1000  8  0.1  python3 /app/xuseek/stall_watch.py
```

`docker-entrypoint.sh` 里 launcher 的定位**两种布局都查**（裸克隆在前、
xusi 布局在后），但 stall_watch 的定位只查裸克隆布局就回落 /app：

```sh
    _WATCH="${XUSEEK_HOME:-/data}/xuseek/stall_watch.py"
    [ -f "$_WATCH" ] || _WATCH="/app/xuseek/stall_watch.py"
```

同版本镜像副本功能等价，但破了「每个 agent 跑自己这份内核」的语义——
实例内核被大脑改过（比如自打 stall_watch 补丁/调参）时不会生效，与 launcher
的行为不一致。

### 建议修法（与 launcher 循环同构，一条备选路径即可）

```diff
--- a/docker-entrypoint.sh
+++ b/docker-entrypoint.sh
@@ -11,7 +11,8 @@
 if [ -n "${XUSEEK_STALL_S:-}" ]; then
     _WATCH="${XUSEEK_HOME:-/data}/xuseek/stall_watch.py"
+    [ -f "$_WATCH" ] || _WATCH="${XUSEEK_HOME:-/data}/xuseek-v2/xuseek/stall_watch.py"
     [ -f "$_WATCH" ] || _WATCH="/app/xuseek/stall_watch.py"
     [ -f "$_WATCH" ] && python3 "$_WATCH" &
 fi
```

## 通用建议：环境相关用例的登记纪律

问题 1 的病根是「条件登记」没有硬约束。建议在 selftest.py 头部（`check`/
`hard_fail` 定义处）把纪律写成一行约定，并按它再扫一遍全部 `check(` 调用点
（尤其 `if` 分支、`for` 循环、`except` 里的登记）：

- `check(name, cond)` **无条件登记**，环境不可控时跳过**执行**、按 `True`
  登记（可选地打印跳过说明）；
- 环境探测类用例的**真失败**走 `hard_fail`（失败但不进计数）；
- 文档计数检查的固定数字从此只依赖「登记总数」，不再随任何环境漂移。

## 部署侧现状（墟司已做的临时修补）

- `versions/xuseek-v2-v2.7.79.zip`（xusi 版本仓库用）已含问题 1 的修复 +
  两处文档计数更新；其余与原始包逐字节一致。
- 用户手里的原始包 `/home/ubuntu/work/xuseek-v2.7.79.zip` **未动**——
  上游修复后重新出包，替换 versions/ 里的 zip 即可。
- 问题 2 未在部署侧修补（等上游），影响仅为 watch 脚本跑镜像副本，
  判活功能不受影响。

## 上游跟进（2026-09-20 晚）

- 问题 1、问题 2 均已由上游修复并出包：**v2.7.80**（commit `ac90981`，
  tag 已推送）。
- 通用建议同批落地：check/hard_fail 定义处写明登记纪律（无条件登记 /
  跳过执行按 True 登记 / 真失败走 hard_fail），并全量扫过 check() 调用点，
  仅此一处条件登记，已收口。
- 新包已入 versions/：`xuseek-v2-v2.7.80.zip` + `xuseek-v2-v2.7.80.tar.gz`
  （自检 480 项，有/无 serve 环境计数恒定）。部署侧可删除临时的
  `xuseek-v2-v2.7.79.zip`（含局部修补的旧包）。
