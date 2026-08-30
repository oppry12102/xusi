"""xuseek-v2 版本仓库：versions/ 下的 zip 包由管理员投放，创建 agent 时按版本号选用。
versions/ 是 xuseek-v2 源码的**唯一事实源**（每 agent 一份实例私有副本）。

约定：文件名 xuseek-v2-<版本号>.zip（如 xuseek-v2-v2.3.0.zip）；包内是源码根
（xuseek.sh 所在目录）——在压缩包根部、或包在唯一的一级子目录里都认。

选定版本后解压一份**实例私有副本**到 instances/<id>/xuseek-v2/：实例之间
互不影响，实例目录自洽、可单独迁移。

解压是防御式的：绝对路径 / .. / 符号链接成员一律跳过（防 zip-slip），
.venv / .git / __pycache__ / *.pyc 不落地，xuseek.sh 强制可执行。
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .config import get_config

# 实例内私有源码副本的目录名（agentops 也引用）
SRC_DIR_NAME = "xuseek-v2"

_ZIP_RE = re.compile(r"^xuseek-v2-(?P<v>[A-Za-z0-9][A-Za-z0-9._-]*)\.zip$")
_VER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 这些目录/后缀不落地（运行时产物或仓库元数据，解压时自动剔除）
_SKIP_PARTS = {".venv", ".git", "__pycache__", ".pytest_cache", "node_modules"}


class VersionError(RuntimeError):
    """版本仓库业务错误（用户可读；API 层转 400）。"""


def repo_dir() -> Path:
    return get_config().versions_dir


_NUMCORE_RE = re.compile(r"(\d+(?:\.\d+)*)")


def numeric_core(v: str) -> tuple[int, ...] | None:
    """版本号的数值核心（取首个数字段逐段转 int）：'v2.5.5'→(2,5,5)，
    '2.7.5'→(2,7,5)。无数字段（如 'dev'）返回 None。

    跨版本语义阈值比较用——版本仓库的命名历史有 v 前缀混用（v2.5.5 与
    2.7.5 并存），直接按字符串段比会得出 v2.5.5 > 2.7.5。"""
    m = _NUMCORE_RE.search(v or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def at_least(v: str, floor: str) -> bool:
    """v >= floor 的数值核心比较。v 解析不出数字段按 True——自打包/开发版
    多为近期源码，按新版语义处理（宁可走新格式被旧核忽略，也不按旧格式
    让新核静默丢限额）。"""
    a, b = numeric_core(v), numeric_core(floor)
    return True if a is None or b is None else a >= b


def _sort_key(v: str):
    """自然段序：按 . _ - 切段，数字段按数值比较（v2.10 > v2.9）。
    同数值核心版本的后缀平局裁决用（2.7.5-10 > 2.7.5-2）。"""
    return tuple((0, int(p)) if p.isdigit() else (1, p)
                 for p in re.split(r"[._-]", v))


def _rank(v: str):
    """仓库清单排序键（list_versions 用）：

    - 无数值核心、或剥掉 v/V 前缀后仍非数字开头的（自打包/开发版，如
      dev2026）：排最尾，缺省版本不会误选它。注意这与 at_least 相反——
      at_least 把解析不出数字段的按「新版」处理（预算语义），两条路各取
      所需，别混用；
    - 其余（含 v 前缀的正式版）：数值核心优先（v 前缀混用时按字符串段比
      会把无前缀包如 2.8.0 误判为最旧，缺省版本选择跟着错），同核心再按
      自然段序比后缀（2.7.5-10 > 2.7.5-2）。"""
    nc = numeric_core(v)
    stem = v[1:] if v[:1] in ("v", "V") else v
    if nc is None or not stem[:1].isdigit():
        return (0, (), v)
    return (1, nc, _sort_key(v))


def list_versions() -> list[dict]:
    """仓库清单（版本号新→旧）：[{version, file, size_bytes, mtime}]。"""
    d = repo_dir()
    out: list[dict] = []
    if d.is_dir():
        for f in d.iterdir():
            m = _ZIP_RE.match(f.name)
            if not (m and f.is_file()):
                continue
            st = f.stat()
            out.append({
                "version": m.group("v"),
                "file": f.name,
                "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
    out.sort(key=lambda r: _rank(r["version"]), reverse=True)
    return out


def zip_for(version: str) -> Path:
    """版本号 → zip 路径。校验命名（防路径穿越）与存在性，失败抛 VersionError。"""
    v = (version or "").strip()
    if not _VER_RE.match(v):
        raise VersionError(
            f"非法版本号 {version!r}（允许字母数字开头，仅字母数字 . _ -，≤64 位）")
    p = repo_dir() / f"xuseek-v2-{v}.zip"
    if not p.is_file():
        avail = "、".join(r["version"] for r in list_versions()) or "（仓库为空）"
        raise VersionError(
            f"版本仓库里没有版本 {v}。可用：{avail}（仓库目录 {repo_dir()}，"
            f"投放方法见 docs/versions.md）")
    return p


def extract(version: str, dest: Path) -> Path:
    """把版本的源码解压到 dest（如 instances/<id>/xuseek-v2），返回 dest。

    dest 已存在视为冲突（创建流程只在全新 home 里调用）；失败时尽量不留半成品。
    """
    zp = zip_for(version)
    if dest.exists():
        raise VersionError(f"目标目录已存在：{dest}（实例源码副本应在全新 home 里解压）")
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / f".{dest.name}.extracting"
    if staging.exists():          # 上一次异常退出留下的残骸
        shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir(parents=True)
        root = _unzip(zp, staging)
        if root != staging:
            # 源码根在压缩包的一级子目录里：提升出来，staging 里剩下的杂物一并丢弃
            promoted = staging / ".promoted"
            root.rename(promoted)
            root = promoted
        root.rename(dest)         # 同目录 rename：原子落位
    finally:
        shutil.rmtree(staging, ignore_errors=True)   # 成功清杂物，失败清半成品
    return dest


def _unzip(zp: Path, into: Path) -> Path:
    """安全解压到 into，返回 xuseek.sh 所在的源码根目录。"""
    try:
        zf = zipfile.ZipFile(zp)
    except zipfile.BadZipFile as e:
        raise VersionError(f"不是有效的 zip 包：{zp.name}（{e}）") from e
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = PurePosixPath(info.filename).parts
            if info.filename.startswith(("/", "\\")) or ".." in parts:
                continue                                   # 防 zip-slip
            if stat.S_ISLNK(info.external_attr >> 16):
                continue                                   # 符号链接不落地
            if any(p in _SKIP_PARTS for p in parts) or info.filename.endswith(".pyc"):
                continue
            target = into.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            mode = info.external_attr >> 16 & 0o777
            if mode:
                os.chmod(target, mode)
    root = _locate_root(into)
    sh = root / "xuseek.sh"
    sh.chmod(sh.stat().st_mode | 0o111)   # 无条件保证可执行（ExecStart 直接跑它）
    return root


def _locate_root(base: Path) -> Path:
    """源码根 = 含 xuseek.sh 的目录：压缩包根部，或任一一级子目录。"""
    if (base / "xuseek.sh").exists():
        return base
    for p in sorted(base.iterdir()):
        if p.is_dir() and (p / "xuseek.sh").exists():
            return p
    raise VersionError(
        f"压缩包里找不到 xuseek.sh（应打包 xuseek-v2 源码根目录；"
        f"打包方法见 docs/versions.md）")
