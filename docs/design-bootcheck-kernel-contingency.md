# design:bootcheck 内核候补形——「采用了却没跑」实证触发后开工

- 状态:**候补**(不排期;触发条件见下;由 proposal-peer-letter-boot-validity.md 评审记录裁定留档)
- 日期:2026-10-08 · 设计人:内核侧(v2.9.37 基线)
- 触发条件:社区先行实跑(BOOT 纪律 + 探针自落账,见提案评审记录)一轮后,台账出现「`workspace/bootcheck.d/` 在场但该口无 bootcheck 行」的实证——即**每口注入的教学路径也漏气**——且量级不小。届时内核收 exec 底座,本文件即施工图。
- 定位一句话:内核只保证「每口必跑、结果必达、开口必见」三下;判断内容(脚本)归大脑/社区,解释与紧迫度归大脑。**零配置、零铃、零状态。**

## §1 形状(相对提案 §3 的四重简化)

1. **零配置**——目录在场=采用,约定优于配置。不加 `[bootcheck]` 节:提案原 `fail`/`fact` 两键过不了内核进段判据(配置面只放「管理员比机器多知道什么」,fail="bell" 唯一合理值、fact=true 不可关,都是铁律非偏好);`timeout` 也不是管理员知识,是设计常数(见 §2)。config.py / config.template.toml / `_KNOWN_TOP` **零改动**。采用路径=大脑或社区写个目录,**连管理员都不用惊动**。
2. **零铃**——不碰门铃。开局时序证明:探针在 opening 组装前跑,失败行落在唤醒基线之后、大脑第一读之前,开口行还标红——**发现必发生在醒着的时候**,「睡眠中被铃唤醒」的场景构造不出来。要不要立刻复查( short sleep)是大脑既有全权;机器按铃=替大脑定紧急程度=逼办的机器版。wake 代数**零新增来源**(比提案 §9「只增不改」更强)。
3. **顺序执行**——`subprocess.run` 每件超时,照 run_shell 的 `_run` 形状;不做并行池/总额度。最坏 5 件全坏 5s/件=25s,仅全坏时发生;正常全绿亚秒。
4. **零状态**——无 Daemon 内存态、无 edge-trigger、无失败集比对。每口照跑照落,红的次数=落账的行数——账本自然冗余即天然 edge 信息,大脑读账自见「这是第几红了」。机器不做去重。

## §2 探针契约(模块 docstring 写明)

- 位置:`$XUSEEK_HOME/workspace/bootcheck.d/`(v2.9.37 正字法全形);只收**直接文件**(不递归),点开头文件跳过;目录缺/空 = 零探针零行零噪声(自愿采用的极限形)。
- 执行:按文件名排序逐件;`.py` → `[sys.executable, path]`,其余直接执行(shebang+exec 位);cwd=workspace;env 与 run_shell 子进程同源(`_subprocess_env`:XUSEEK_HOME/PYTHONPATH/venv PATH——探针内 `from xuseek import facts` 直落账可用)。
- 判定:exit 0=绿;非零=红;**stdout 末非空行=detail**(截 200,error[:200] 家法);红而 stdout 空 → `f"exit={code}"`;超时(5s/件,设计常数 `PROBE_TIMEOUT_S=5`)→ 红,detail=`"超时(>5s)"`;起不来 → 红,detail=`f"无法执行:{e}"`[:200]。
- **fail-open**:探针崩/超时/不可执行都不阻断开局,红行照落即走——底座不绑架呼吸。
- **落账**:机器逐件 `facts.safe_append("bootcheck", name=<workspace 相对路径如 "bootcheck.d/02-registry.py">, ok=<bool>, detail=<str>)`——name 用 workspace 相对形(大脑面正字法:相对路径锚 workspace)。

## §3 内核挂点(两处,~60 行)

1. **新模块 `xuseek/bootcheck.py`**(~60 行):
   - `run_and_record() -> list[dict]`:扫描→逐件跑→逐件落账→返回全部行;`summary_fails(rows) -> list[str]` 给红名单。
   - 模块只 import facts/config,不 import daemon/doorbell——删掉本模块即删掉本特性(解耦自检)。
2. **`xuseek/daemon.py` 挂 2 行**:真呼吸分支、park 检查(249-261)的 `continue` **之后**、`daemon_facts.opening(...)`(264)之前:
   ```python
   _boot_rows = bootcheck.run_and_record()
   result = self._run_session(opening=daemon_facts.opening(
       boot_fails=bootcheck.summary_fails(_boot_rows)))
   ```
   park 分支零改动→驻留静默不被探针打破;探针行 type 不在唤醒谓词(mail/bell)里→wake 代数零交互。

## §4 开口红字

`daemon_facts.opening()` 加可选参 `boot_fails: list[str] | None = None`:非空时索引行尾追加 ` | bootcheck: 红 2(bootcheck.d/a.py、b.py…)`(件名截前 3+…);空/None 无此段(绿不扰)。与脏死标注同类:机器报事实,不解释、不逼办。默认参→既有调用方(selftest 等)零改动。

## §5 测试(selftest +5 → 657;e2e +S10)

- selftest 新 section(仿 relay_eye 范式造载体→调函数→facts 查账;真子进程范式):
  1. 目录缺→返回 []、零行(零噪声);
  2. 三探针端到端(绿/红/睡死):行形四键、detail=stdout 末行、超时 ok=False、cwd=workspace;
  3. fail-open:不可执行探针→红行照落不抛出;
  4. opening 注入:红名单含「bootcheck: 红」,None 无此段;
  5. 源码接线:daemon.py 恰一次 `run_and_record()` 且在 park continue 之后(grep 邻近 opening)。
  - README/architecture 计数 652→657 跟涨(元检查自动守)。
- e2e S10(前九场景零影响——bootcheck.d 到 S10 才被大脑创建):口 N 大脑 run_shell 建目录写红探针(纯文件操作即采用,零配置实证)→ 口 N+1 红行落账+opening 红字+**零 bell 行**+节律不被打扰(睡满 SCEN_GAP)→ 口 N+2 改绿 → 口 N+3 全绿无红字→end with stop→驻留 6s 行数冻结(park 不跑)→来信唤醒口照跑。
- 手验:临时 home 建目录写 `import sys; sys.exit(1)` 起一口→`sqlite3 data/facts.db "SELECT body FROM facts WHERE type='bootcheck'"` 红行在、无 bell 行、下口 opening 含红字。

## §6 明示不做(即使触发收编也做)

- 不做铃(§1.2,永久);不做并行池/总额度(§1.3,除非实测 ≥20 件探针);不做 config 节(§1.1,永久——除非实跑证明 timeout=5s 常数不够用,那也是改常数不是加旋钮);不做 32 件封顶(目录归大脑所有,自担其噪);不代写五探针(社区件);不做 park 期跑探针(驻留静默是柱子)。
