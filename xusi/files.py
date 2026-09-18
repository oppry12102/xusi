"""实例目录文件通道：只开放 upload/（可写·投递区）与 workspace/（只读）。

设计原则——管理面不干预 LLM：workspace 是 agent 领地，只许看（浏览/预览/
下载/打包），一个字节不写；upload/ 是管理面领土，向 agent 单向投递文件。
agent 取件路径：内核 file_read/file_write 以 workspace 为牢（够不到实例根），
但 shell / runpy 工具 cwd=workspace、无 chroot——`../upload/<文件>` 可达，
上传后的「投信告知」就按这个路径写法告知 agent。

删除一律软删：挪进 etc/.trash/fs/<agent-id>/…（管理面领土，不占实例体积、
不进备份包），误删可捞回。

安全模型（_safe）三道闸：
1. 首段白名单——路径第一段必须是 upload / workspace，实例目录其余
   （data/、config.toml、xuseek-v2/ 源码副本）彻底不可见；
2. 逐段名校验——拒空段、`.`、`..`、控制字符、超长名（防拼路径绕过）；
3. resolve 后 containment——符号链接指向开放区外的一律拒绝
   （与 backup.py 的逃逸防线同口径）。

三层共用同一份实现：api/files_routes.py（本地 HTTP）、__main__.py 的
fs-* 子命令（远端零管理机经 ssh 执行的就是这份代码，校验在远端发生）。
所有写操作落盘前记 audit（agent.fs.*）。
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path

from .agentops import AgentError, audit
from .config import get_config
from .registry import get_agent

# 开放区：首段白名单（实例目录的其余部分不可见）
OPEN_ROOTS = ("upload", "workspace")
WRITE_ROOT = "upload"

_TEXT_CAP = 256 * 1024        # 文本预览封顶（超出截断打标，下载看全量）
_EDIT_CAP = 2 * 1024 * 1024   # 在线编辑封顶（更大的文本请下载改完再传）
_NAME_MAX = 255               # 单段文件名上限（ext4 同款）
_PATH_MAX = 4096              # 相对路径总长上限
_LIST_CAP = 2000              # 单目录列条目封顶（防十万文件的目录炸 JSON）
_CHUNK = 1024 * 1024          # 流式写盘块大小

# raw 模式允许 inline 的 Content-Type 白名单：只有位图。svg 可带脚本、
# html/pdf 同理——同源 inline 等于把管理员浏览器交给 agent 文件摆布，
# 一律 octet-stream + attachment 落盘。文本预览走 JSON（前端转义）无此虑。
INLINE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}

_BAD_SEG = re.compile(r"[\x00-\x1f\x7f]")


def _check_seg(seg: str) -> str:
    """单段文件名校验（拼路径的最小单元）：拒空、`.`、`..`、控制字符、超长。"""
    if not seg or seg in (".", "..") or _BAD_SEG.search(seg):
        raise AgentError(f"非法路径段：{seg!r}")
    if len(seg) > _NAME_MAX:
        raise AgentError(f"路径段超长（>{_NAME_MAX} 字符）：{seg[:32]}…")
    return seg


def _norm(rel: str) -> list[str]:
    """相对路径 → 段列表（去首尾空白、去首部 /、滤空段与 `.`）。"""
    rel = (rel or "").strip().lstrip("/").rstrip("/")
    if len(rel) > _PATH_MAX:
        raise AgentError(f"路径超长（>{_PATH_MAX} 字符）")
    return [s for s in rel.split("/") if s and s != "."]


def _agent(agent_id: str) -> dict:
    a = get_agent(agent_id)
    if not a:
        raise AgentError(f"agent 不存在: {agent_id}")
    return a


def _safe(agent_id: str, rel: str, *, write: bool = False) -> Path:
    """三道闸（见模块 docstring）后的绝对路径。根视图（rel 为空）在这里
    就拒——列目录函数对空路径单独合成根视图，不吃 _safe。"""
    parts = _norm(rel)
    if not parts:
        raise AgentError("路径不能为空")
    if parts[0] not in OPEN_ROOTS:
        raise AgentError(f"只开放 {' 与 '.join(OPEN_ROOTS)} 目录（实例目录其余部分不可见）")
    if write and parts[0] != WRITE_ROOT:
        raise AgentError("workspace 只读（agent 领地，管理面不干预）——写操作只收 upload/ 路径")
    for p in parts:
        _check_seg(p)
    home = get_config().instance_home(agent_id)
    base = (home / parts[0]).resolve()
    p = (home / "/".join(parts)).resolve()
    if p != base and base not in p.parents:
        raise AgentError(f"路径越界或符号链接指向开放区外：{rel}")
    return p


def _writable(rel: str) -> bool:
    """该相对路径是否落在可写区（upload/ 前缀）——预览响应带回前端控制按钮。"""
    return _norm(rel)[:1] == [WRITE_ROOT]


# ── 列目录 ─────────────────────────────────────────────────────────────

def list_dir(agent_id: str, rel: str = "") -> dict:
    """列目录。rel 为空 = 根视图（两个开放根的合成视图，不是实例根本身）：
    upload/ 顺手自建（管理面领土，建自己的目录不算副作用）；workspace/
    只报存在性（首口呼吸前由内核自建，缺省列空 + exists=false）。"""
    _agent(agent_id)
    home = get_config().instance_home(agent_id)
    if not _norm(rel):
        up = home / WRITE_ROOT
        up.mkdir(parents=True, exist_ok=True)
        ws = home / "workspace"
        return {"id": agent_id, "path": "", "entries": [
            {"name": WRITE_ROOT, "dir": True, "size": None,
             "mtime": up.stat().st_mtime if up.exists() else None,
             "note": "投递区 · 可写（agent 经 shell 工具 ../upload/ 取件）"},
            {"name": "workspace", "dir": True, "size": None,
             "mtime": ws.stat().st_mtime if ws.exists() else None,
             "note": "agent 领地 · 只读" if ws.exists()
                     else "agent 领地 · 只读（尚未创建——首口呼吸后由内核自建）"},
        ]}
    root = (home / _norm(rel)[0]).resolve()
    d = _safe(agent_id, rel)
    if not d.exists():
        return {"id": agent_id, "path": rel, "exists": False, "entries": []}
    if not d.is_dir():
        raise AgentError(f"不是目录：{rel}")
    entries: list[dict] = []
    try:
        it = os.scandir(d)
    except OSError as e:
        raise AgentError(f"读取目录失败：{e}") from None
    with it:
        for ent in it:
            try:
                st = ent.stat(follow_symlinks=False)
            except OSError:
                continue   # 竞态消失的条目：跳过不炸
            is_dir = stat.S_ISDIR(st.st_mode)
            symlink = ent.is_symlink()
            if symlink:
                # 指向开放区内某目录的符号链接按目录呈现（可下钻）；
                # 指向区外/断链的按文件挂 🔗 标记（点开时 _safe 会给出明确报错）
                try:
                    tgt = Path(ent.path).resolve()
                    if tgt.is_dir() and (tgt == root or root in tgt.parents):
                        is_dir = True
                except OSError:
                    pass
            entries.append({"name": ent.name, "dir": is_dir,
                            "size": None if is_dir else st.st_size,
                            "mtime": st.st_mtime, "symlink": symlink})
            if len(entries) >= _LIST_CAP:
                break
    entries.sort(key=lambda e: (not e["dir"], e["name"].casefold()))
    return {"id": agent_id, "path": rel, "exists": True,
            "truncated": len(entries) >= _LIST_CAP, "entries": entries}


# ── 读文件 ─────────────────────────────────────────────────────────────

def read_text(agent_id: str, rel: str) -> dict:
    """文本预览：截断到 _TEXT_CAP 打标；二进制探测（前 8KB 含 \\x00 或
    utf-8 解不动）→ binary=true（前端只给下载）。editable = 可写区内的
    小文本（在线编辑按钮的显隐依据）。"""
    p = _safe(agent_id, rel)
    if not p.exists():
        raise AgentError(f"文件不存在：{rel}")
    if not p.is_file():
        raise AgentError(f"不是文件：{rel}")
    size = p.stat().st_size
    with p.open("rb") as f:
        raw = f.read(_TEXT_CAP + 1)
    binary = b"\x00" in raw[:8192]
    text = ""
    if not binary:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            binary = True   # 非 UTF-8（GBK/Latin-1 等）——按二进制引导下载
    truncated = size > _TEXT_CAP
    if truncated and text:
        text = text[:_TEXT_CAP]
    return {"id": agent_id, "path": rel, "name": p.name, "size": size,
            "binary": binary, "text": text,
            "truncated": truncated,
            "writable": _writable(rel),
            "editable": (not binary) and _writable(rel) and size <= _EDIT_CAP}


def open_raw(agent_id: str, rel: str) -> dict:
    """raw/下载用：返回绝对路径与 inline 判定（位图白名单内才 inline），
    FileResponse 的包装在路由层（本地）/ CLI 层（远端）做。"""
    p = _safe(agent_id, rel)
    if not p.exists():
        raise AgentError(f"文件不存在：{rel}")
    if not p.is_file():
        raise AgentError("目录请用 zip 下载（fs/zip?path=…）")
    return {"path": p, "name": p.name, "size": p.stat().st_size,
            "inline": p.suffix.lower() in INLINE_TYPES}


def zip_dir(agent_id: str, rel: str) -> tuple[Path, Path]:
    """目录打 zip：临时目录里 make_archive，返回 (zip 路径, 临时目录)——
    调用方负责流出后删临时目录（路由层挂 BackgroundTask）。

    打包前扫符号链接逃逸（followlinks=False 的 walk + resolve 验 containment，
    与 backup.py 同防线）：workspace 是 agent 任意写的，混进指向区外的链接
    就会把区外文件（config.toml 的 key 等）打进来。"""
    p = _safe(agent_id, rel)
    if not p.is_dir():
        raise AgentError(f"不是目录：{rel}")
    root = p.parent.resolve()
    for dirpath, dirnames, filenames in os.walk(p, followlinks=False):
        for n in dirnames + filenames:
            full = Path(dirpath) / n
            if full.is_symlink():
                tgt = full.resolve()
                if tgt != root and root not in tgt.parents:
                    raise AgentError(f"符号链接指向开放区外，拒绝打包：{full.relative_to(p.parent)}")
    tmpdir = Path(tempfile.mkdtemp(prefix="xusi-fs-zip-"))
    try:
        arc = shutil.make_archive(str(tmpdir / "bundle"), "zip",
                                  root_dir=str(p.parent), base_dir=p.name)
    except OSError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise AgentError(f"打包失败：{e}") from None
    return Path(arc), tmpdir


# ── 写操作（全部只收 upload/ 路径，_safe(write=True) 把关）─────────────

def _atomic_write(dest: Path, data: bytes) -> None:
    """原子落盘：同目录临时文件 + os.replace——agent 经 ../upload/ 读文件
    永远是完整的，读不到半截。"""
    tmp = dest.with_name(f".{dest.name}.tmp-{uuid.uuid4().hex[:6]}")
    try:
        with tmp.open("wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise AgentError(f"写入 {dest.name} 失败：{e}") from None


def put_bytes(agent_id: str, rel: str, data: bytes, *, overwrite: bool = False) -> dict:
    """写一个上传文件（rel = 含文件名的完整路径；父目录自动建——CLI fs-put
    与 HTTP multipart 共用；重名不覆盖时拒）。"""
    p = _safe(agent_id, rel, write=True)
    if p.exists() and not overwrite:
        raise AgentError(f"已存在（要覆盖请显式传 overwrite）：{rel}")
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(p, data)
    audit("agent.fs.put", agent=agent_id, path=rel, bytes=len(data),
          overwrite=overwrite)
    return {"id": agent_id, "path": rel, "size": len(data)}


def mkdir(agent_id: str, rel: str) -> dict:
    """新建目录（支持多级；父目录自动建）。"""
    p = _safe(agent_id, rel, write=True)
    if p.exists():
        raise AgentError(f"已存在：{rel}")
    try:
        p.mkdir(parents=True)
    except OSError as e:
        raise AgentError(f"创建目录失败：{e}") from None
    audit("agent.fs.mkdir", agent=agent_id, path=rel)
    return {"id": agent_id, "path": rel, "created": True}


def write_text(agent_id: str, rel: str, text: str) -> dict:
    """新建/保存文本文件（≤ _EDIT_CAP；父目录必须已存在——新建文件请在
    已列出的目录里做，深层路径先建目录）。"""
    if len(text) > _EDIT_CAP:
        raise AgentError(f"文本超长（>{_EDIT_CAP // 1024}KB）——请下载改完再上传")
    p = _safe(agent_id, rel, write=True)
    if p.is_dir():
        raise AgentError(f"目标是目录：{rel}")
    if not p.parent.exists():
        raise AgentError("父目录不存在——先建目录再新建文件")
    _atomic_write(p, text.encode("utf-8"))
    audit("agent.fs.write", agent=agent_id, path=rel, chars=len(text))
    return {"id": agent_id, "path": rel, "size": len(text.encode("utf-8"))}


def move(agent_id: str, src: str, dst: str) -> dict:
    """重命名/移动（upload/ 内；拒覆盖既有目标、拒搬进自己的子树）。"""
    sp = _safe(agent_id, src, write=True)
    dp = _safe(agent_id, dst, write=True)
    if sp == dp:
        raise AgentError("源与目标相同")
    if not sp.exists():
        raise AgentError(f"源不存在：{src}")
    if dp.exists():
        raise AgentError(f"目标已存在：{dst}")
    if not dp.parent.exists():
        raise AgentError("目标父目录不存在")
    if sp in dp.parents:
        raise AgentError("不能把目录搬进它自己的子树里")
    try:
        os.rename(sp, dp)
    except OSError as e:
        raise AgentError(f"移动失败：{e}") from None
    audit("agent.fs.move", agent=agent_id, frm=src, to=dst)
    return {"id": agent_id, "frm": src, "to": dst}


def delete(agent_id: str, rel: str) -> dict:
    """软删：挪进 etc/.trash/fs/<agent-id>/<原相对路径>-<时间戳>（拒删
    upload/ 根）。目录整棵挪（shutil.move 跨文件系统安全）。"""
    p = _safe(agent_id, rel, write=True)
    home = get_config().instance_home(agent_id)
    if p == (home / WRITE_ROOT).resolve():
        raise AgentError("不能删除 upload/ 根")
    if not p.exists():
        raise AgentError(f"不存在：{rel}")
    was_dir = p.is_dir()
    ts = time.strftime("%Y%m%dT%H%M%S")
    # 相对路径里的目录层级在回收站原样保留（upload/ 段截掉——回收站已按
    # agent-id 分家）；同名同秒再删加随机后缀防撞
    rel_keep = "/".join(_norm(rel)[1:])
    dest = get_config().trash_dir / "fs" / agent_id / f"{rel_keep}-{ts}-{uuid.uuid4().hex[:4]}"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
    except OSError as e:
        raise AgentError(f"删除失败（原文件未动）：{e}") from None
    audit("agent.fs.delete", agent=agent_id, path=rel, dir=was_dir,
          trash=str(dest))
    return {"id": agent_id, "deleted": rel, "was_dir": was_dir,
            "moved_to": str(dest)}
