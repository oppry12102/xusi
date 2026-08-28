# xuseek-v2 版本仓库

管理员把不同版本的 xuseek-v2 源码打包成 zip 投放到 `versions/` 目录（**整个目录
不入 git**——zip 里是私有源码），创建 agent 时即可按版本号选用。WebUI「新建
agent」对话框与 `POST /api/agents` 的 `source_version` 字段都从这里取版本
（清单接口：`GET /api/versions`）。

## 命名约定

```
xuseek-v2-<版本号>.zip
```

版本号以字母或数字开头，仅含 `字母 数字 . _ -`，≤64 位（`main` 为保留值，请勿用作版本号）。例如：

- `xuseek-v2-v2.3.0.zip`
- `xuseek-v2-20260821.zip`

不符合命名的文件会被清单静默忽略。

## 打包方法

在 xuseek-v2 的 git 检出目录里（自动排除运行时产物）：

```bash
cd /path/to/xuseek-v2
git archive --format=zip -o /path/to/xusi/versions/xuseek-v2-v2.3.0.zip HEAD
```

或用 zip（显式排除）：

```bash
zip -r /path/to/xusi/versions/xuseek-v2-v2.3.0.zip . \
    -x ".venv/*" ".git/*" "__pycache__/*" "*.pyc"
```

## 包内结构（两种都认）

- 源码根（含 `xuseek.sh`）直接在压缩包根部；或
- 包在唯一的一级子目录里（如 `xuseek-v2/xuseek.sh`）。

`.venv` / `.git` / `__pycache__` / `*.pyc` 成员解压时自动跳过；
绝对路径、`..`、符号链接成员一律不落地（防 zip-slip）。

## 语义

- **缺省（不选版本）→ 本仓库最新版**：源码解压到 `instances/<id>/xuseek-v2/`，
  每实例一份私有副本（首次启动各自构建 `.venv`）——实例目录自洽，**可单独迁移**
  （整个 `instances/<id>/` 拷走即可）；实例之间互不影响，不同 agent 可跑不同版本。
  删除 agent 时副本随实例目录一起进 `.trash`。实际版本记入注册表，创建后不可改。
- **显式版本** → 用该版本（同样是私有副本）。
- **共享主源码**（`etc/xusi.toml` 的 `source_dir`，`source_version="main"` 显式选择，
  或仓库为空时的缺省回落）→ 本地已在 → 直接用（**不需要 GitHub**）；不在 → 创建时
  试 GitHub 拉取。**共享主源码将逐步废弃**——仅现存 agent 与过渡期显式选择在用，
  新建 agent 一律默认私有副本。`GET /api/versions` 的 `default_ready` 标示其是否本地就绪。
- `main` 是保留值（显式选共享主源码），请勿用作版本号。
- 已有 agent 不受影响。
- **存量 agent 升级内核**（「创建后不可改」约束的是创建流程；升级是目录级操作）
  的标准做法见 [kernel-upgrade.md](kernel-upgrade.md)。
