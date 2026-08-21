# xuseek-v2 版本仓库

管理员把不同版本的 xuseek-v2 源码打包成 zip 投放到 `versions/` 目录（**整个目录
不入 git**——zip 里是私有源码），创建 agent 时即可按版本号选用。WebUI「新建
agent」对话框与 `POST /api/agents` 的 `source_version` 字段都从这里取版本
（清单接口：`GET /api/versions`）。

## 命名约定

```
xuseek-v2-<版本号>.zip
```

版本号以字母或数字开头，仅含 `字母 数字 . _ -`，≤64 位。例如：

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

- **选定版本创建** agent → 源码解压到 `instances/<id>/xuseek-v2/`，每实例一份
  私有副本（首次启动各自构建 `.venv`）：实例之间、与共享主源码互不影响，
  不同 agent 可跑不同版本。删除 agent 时副本随实例目录一起进 `.trash`。
- **不选版本** → 与从前一样用共享主源码（`etc/xusi.toml` 的 `source_dir`，
  缺失时自动从 GitHub 拉取）。
- 已有 agent 不受本仓库影响；版本在创建后不可改。
