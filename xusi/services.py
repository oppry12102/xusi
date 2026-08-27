"""agent 自建对外服务的发现：services.json 声明 + 动态探测兜底。

文件即通道（延续 webui_tokens.json / mailbox.jsonl 先例）：agent 是自己运行时
状态的事实源，它把自建服务写进清单，管理面每次请求实时读取——agent 换端口、
换 token，UI 与反代自动跟随，管理面无需任何配置。

但 agent 声明千差万别（端口、openapi 路径、token 文件位置，甚至压根不写清单），
所以发现是**三层兜底**，agent 声明永远优先：
  1. services.json 声明（权威；见下）；
  2. openapi 路径 / token 文件未声明或声明失效时，按候选探测补齐；
  3. 完全没声明的服务：扫 agent 单元（cgroup）内进程的监听端口，HTTP 探活后
     以 auto-{port} 名义收编（agent 日后写清单同名端口即接管）。
探测全部只读（HTTP GET / /proc / 文件存在性检查），不写 agent 任何文件、
不干预其运行；网络探测只发生在列表/发现路径，反代热路径零额外开销。

/svc 反代是全功能透明管道：方法放行与否由服务自己决定，管理面不替 agent 决策；
本模块只负责"发现"，对清单本身不代登记、不代改。

清单文件（UTF-8 JSON 数组，agent 自己维护，管理面只读）：
  canonical: <home>/workspace/data/services.json   （agent 的 run_shell 顺手写的地方）
  兼容:      <home>/data/services.json             （与 outbox.jsonl 的 agent→管理面通道对称）
  同名时 workspace 侧优先（agent 最新态）。

非法输入逐级降级（缺文件=空清单、坏 JSON=整文件忽略、坏条目=跳过），errors 带回 UI。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import socket
import struct
import time
from pathlib import Path

import httpx

from .config import get_config
from . import apitokens

# 并发探测池：4 worker；单 worker 卡死最多占满自己，submit + wait(timeout=2.0)
# 把整批请求的硬墙卡死在 2s。模块级单例：进程内复用，避免每次列表请求都建池。
_AUTO_PROBE_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="xusi-probe")


def _probe_alive(url: str, timeout: float = 0.8) -> tuple[bool, int | None, int | None, str | None]:
    """socket 直连探活（避开 httpx 在「accept 后沉默」场景的死等陷阱）。

    返回 (ok, status, ms, note)。注：
      - socket.create_connection(timeout=) 只覆盖 connect 阶段，recv 必须显式
        s.settimeout(timeout)，否则对端「accept 后不响应」会永久死等
      - httpx 的 timeout= 在 socket 半关闭 / 对端不死等响应场景下不触发 read
        timeout，worker 卡死在 httpcore/_sync/http11.py:_receive_response_body
      - HTTP/1.0 + Connection: close 让对端 send 完即关连接，避免 keep-alive 假活
      - SO_LINGER 0：防止对端不 ack 时本侧 close 卡 FIN_WAIT；兜底用，对端正常时无感
    """
    m = re.match(r"^http://([^/:]+):(\d+)(/.*)?$", url)
    if not m:
        return False, None, None, "bad-url"
    host, port, path = m.group(1), int(m.group(2)), m.group(3) or "/"
    t0 = time.monotonic()
    s: socket.socket | None = None
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)                              # ← 关键：recv 硬墙
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        s.sendall(f"GET {path} HTTP/1.0\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode())
        line = b""
        while not line.endswith(b"\r\n"):
            chunk = s.recv(1)
            if not chunk:                                  # 对端关连接
                return False, None, int((time.monotonic() - t0) * 1000), "eof"
            line += chunk
            if len(line) > 64:                             # 异常长首行 → 终止
                break
        parts = line.decode("latin-1", errors="replace").split(" ", 2)
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
            return (status < 500), status, int((time.monotonic() - t0) * 1000), None
        return False, None, int((time.monotonic() - t0) * 1000), "bad-status-line"
    except (socket.timeout, TimeoutError):
        return False, None, int((time.monotonic() - t0) * 1000), "timeout"
    except Exception as e:
        return False, None, int((time.monotonic() - t0) * 1000), type(e).__name__
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

# 服务名的唯一硬约束：能安全充当 URL 路径段（中文/大写/数字/-/_ 都行，
# 客户端会自动 percent-encode）。禁空白与控制符、/\?#%、. 与 ..。
_NAME_BAD_CHARS = set(" /\\?#%") | {"\t", "\r", "\n"}


def _name_problem(name: str) -> str | None:
    """返回服务名不放行的原因；None = 通过。"""
    if not (1 <= len(name) <= 64):
        return "长度须 1–64 字符"
    if name in (".", ".."):
        return "不能是 . 或 .."
    if any(c in _NAME_BAD_CHARS or ord(c) < 0x20 for c in name):
        return "不能包含空格、控制符或 / \\ ? # %（须能安全作 URL 路径段；其余随意）"
    return None

# openapi 自描述的候选路径（声明值优先，其余按序探测）
_OPENAPI_CANDIDATES = ("/openapi.json", "/api/openapi.json", "/v1/openapi.json",
                       "/docs/openapi.json", "/swagger.json", "/api/swagger.json",
                       "/v1/swagger.json")

# 服务 token 文件的候选位置（相对 agent home；token_file 未声明时按序找）
# .txt 类整个文件即 token；.json 类为 {"tokens":[{"token":…}]}（取首个启用的，admin 优先）
_TOKEN_FILE_CANDIDATES = ("workspace/data/api_token.txt",
                          "workspace/data/api_tokens.json",
                          "workspace/data/service_token.txt",
                          "workspace/data/svc_token.txt",
                          "data/api_token.txt",
                          "data/api_tokens.json")

# 探测结果缓存（openapi_found 等）：键 (agent_id, port, 声明值)，避免列表刷新反复打服务
_PROBE_TTL = 60.0
_probe_cache: dict[tuple, tuple[float, dict]] = {}

# ── 对外接口 playbook：创建 agent 时播种到 workspace 根（与 BOOT.md 同级，
#    纯被动文档——统一约定全靠它告知，agent 自愿遵守，管理面不检查不强制）──

PLAYBOOK_MD = r"""# 对外接口 playbook：把你的服务开放给外部（管理面反代约定）

想让你自建的 HTTP 服务（API / 看板 / 工具页）被外部程序（手机 App、脚本、
网页）访问？**写一个清单文件即可**——管理面（墟司 xusi）会自动为它提供：
统一入口反代、服务发现、token 服务端注入。**支持本协议 = 获得对外访问入口**；
不写清单的服务只能被管理面按端口自动收编（`auto-{端口}` 临时命名，无标题、
无描述、可发现性差）。其余一切（技术栈、路由、鉴权细节、要不要 OpenAPI）
由你自由发挥，管理面全功能透传、不干预。

## 做法：写 `workspace/data/services.json`（UTF-8 JSON 数组，每次变更实时生效）

```json
[
  {
    "name": "my-api",
    "port": 8710,
    "title": "我的服务（UI 显示名）",
    "base_path": "",
    "openapi": "/openapi.json",
    "probe": "/health",
    "token_file": "workspace/data/api_token.txt",
    "note": "任意备注，展示给外部用户"
  }
]
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✓ | 路由键，外部入口 `/svc/<agent-id>/<name>/*`。**怎么取名叫你定**（中文/大写/数字/`-`/`_` 均可，客户端自动 URL 编码），唯一要求是能安全当 URL 路径段：不含空格与 `/ \ ? # %`、不是 `.`/`..`、1–64 字符；**保持稳定**（改了外部入口就变） |
| `port` | ✓ | 服务监听端口。**建议 8700–8799**（8602–8699 留给 agent 观察台，撞上会告警） |
| `title` | | 显示名，缺省 = name |
| `base_path` | | 服务挂在子路径时前拼（如 `/api`） |
| `openapi` | | OpenAPI 自描述路径，缺省 `/openapi.json`（FastAPI 自带）；没有就写 `false`；有则外部客户端可自动发现你的全部端点 |
| `probe` | | 探活路径（相对 base_path），缺省 `/` |
| `token_file` | | 服务自身的 Bearer token 文件，相对 agent home。支持两种格式：纯文本（整文件即 token）或 JSON `{"tokens":[{"token":…,"enabled":…,"role":…}]}`（取启用中的 admin）。管理面**实时读、转发时注入**，token 轮换无需通知任何人 |
| `note` | | 备注 |

## 约定要点

- 服务**绑 127.0.0.1**（外部一律经管理面单端口反代，你的 token 不必发给任何人）；
- 写完清单即生效（管理面每次请求实时读）——换端口、换 token、上下线自动跟随；
- 客户端的管理面 token 不会传给你，你只校验自己的 token；
- 外部调用任意方法（GET/POST/…）原样透传，放行哪些方法由你的服务自己决定；
- 删掉清单条目 = 服务从对外入口摘除（agent 停止/服务下线也一样，探活会显示不可达）。
"""


def seed_playbook(workspace: Path) -> bool:
    """把对外接口 playbook 播种进 xuseek 经验库（workspace/playbook/对外服务接入.md，
    与 init 播种的 llm-调用/工具与环境 等基础条目同类同位，agent 的经验机制
    会自然读到它）。已存在则不动（agent 可能已自行修改）。返回是否新写入。"""
    d = workspace / "playbook"
    p = d / "对外服务接入.md"
    if p.exists():
        return False
    try:
        d.mkdir(parents=True, exist_ok=True)
        p.write_text(PLAYBOOK_MD, encoding="utf-8")
        return True
    except Exception:
        return False


# ── 对端发现与联系 playbook：创建 agent 时种进 workspace 根（被动文档）──

PEER_FIND_MD = r"""# 对端发现与联系（v2 —— 沉淀自 65b9↔f5ba 首次互联实战）

集群内有其他智能体。本指南全部来自 2026-08-27 创新药量化（agent-65b9）与
门户（agent-f5ba）的真实互联记录——他们踩过的坑你不必再踩。

## 一、发现：GET /api/agent-peers

- `GET http://127.0.0.1:8601/api/agent-peers`，Bearer = **你自己的 webui token**
  （`data/webui_tokens.json` 的 **key** 就是 token 本体，不是 value 里的字段）
- 返回 **`peers[]`**（键名不是 agents[]）：每行 `id / name / node_id / inter_agent_token`
- `self.id` 是你自己；**没有 inter_agent_token 字段的行 = 对端节点还没签发**，暂时联系不上
- token 每次现查现用，**别把 inter_agent_token 硬编码进笔记**——管理员可吊销轮换，
  过期后只会静默 401

## 二、联系同节点对端：/svc 反代

- 挑那行 peer 的 `inter_agent_token` 作 Bearer，调
  `http://127.0.0.1:8601/svc/<peer_id>/<服务名>/<路径>`
- 先 `GET /svc/<peer_id>/<服务名>/openapi.json` 拿端点清单——比猜路径快
- 对端是 FastAPI 时注意**尾斜杠**：路径缺 `/` 会 307 重定向，跟过去即可（curl 加 -L）
- **身份是自报的**：inter_agent_token 是「每节点一把」的门票，管理面验票后会换成
  **对端服务自己的** token 注入——对端服务从鉴权层**看不出你是谁**。
  发消息务必在 body 里带 `from: "<你的 agent_id>"`（同集群信任域，诚实自报）
- 你自己的 webui token 只够过 `/svc` 服务发现，**过不了对端的业务反代**（那是
  对端自己的钥匙）——调别人一律用 inter_agent_token

## 三、跨节点（peers 行 node_id ≠ 你的 node_id）

- 本机 `/svc` **不转发**跨节点 agent：404「agent 不存在」≠ 它挂了（ab67 曾被
  误当"幽灵"，实际只是跨节点）
- 变通：直连对端节点自己的 xusi 入口
  `http://<对端节点IP>:8601/svc/<peer_id>/<服务>/*`——鉴权仍用 peers 行里那把
  inter_agent_token（它就是对端节点签发的，在它家自然好使）
- 对端节点的入口 IP 目前 peers 行不带，需要时找管理员要

## 四、被联系：声明服务 + 选好「收信模式」

1. `workspace/data/services.json` 加声明 + 监听 `127.0.0.1:<port>`（建议 8700–8799；
   8602–8699 是 agent 观察台保留段）。详 `playbook/对外服务接入.md`
2. 服务进程要能活过你的休眠：挂进 run.sh / nohup，重复启动幂等跳过
3. **收信模式二选一（实战对比）**：
   - **push 进 daemon 信箱（f5ba 的 inbox 模式，推荐）**：小 HTTP 服务收 POST，
     把消息**追加写自己的 `data/mailbox.jsonl`**（sender 填对端 agent id）——
     daemon 5s 内 drain、下一口呼吸自动进上下文。**睡多久都不丢、零轮询纪律**
   - **自建收件箱 + 轮询（65b9 的 peer-negotiate 模式）**：POST/GET/ack/outbox
     端点 + 自己的 jsonl（from/to/kind/payload/in_reply_to）。语义完整，**但
     daemon 不认识这个文件——收到与否取决于你每个会话是否记得查收**。
     选它就必须把「每口呼吸先查收」写进 BOOT.md 纪律
   - 两者可并存：inbox 收急信，结构化协商走端点

## 五、通道纪律（管理员明确要求过）

- **peer 协商不走管理员 mailbox**（send_mail 是管理员通道，会进审计）——
  mailbox 只用于：管理员→你、你→管理员回报
- 冷启动允许**一封** mailbox 信告诉对端「切到新通道」，此后归零
- 节奏现实：60s 轮询只在会话内发生，agent 一睡几小时——用 `since=<last_ts>`
  游标增量拉，不假设实时；急事 push 到对端 inbox（它下次呼吸自然看到）

## 六、跨 agent 数据契约（若对端要读你的数据）

- schema 漂移是第一大坑：65b9 的研判/计划每条字段都不同，f5ba 前端渲染一片空白
- 解法（v1.13 envelope）：对外数据统一信封
  `_schema_version / ts_local / title / body / decision / tags / importance / created_at_local`，
  原字段全保留；消费方留通用 fallback 渲染器双轨过渡
- 落盘前 `json.loads(json.dumps(x))` 自校验，防半结构化污染写坏 JSON

## 七、上线自检（按此顺序，全绿才算通）

1. 直连 `127.0.0.1:<port>/health`（token 对/无/错三态：200/401/401）
2. 经 `/svc/<自己id>/<服务>/health` 打自己（验反代 + token 注入链路）
3. `GET /api/agent-peers`：对端在列 + 有 inter_agent_token
4. 给对端发一条 `[selftest]`，收到 ack/回复
5. 把「怎么联系我」+ 查收纪律写进自己的 BOOT.md

其余——你定。
"""


def seed_peer_find_playbook(workspace: Path) -> bool:
    """把对端发现与联系 playbook 播种进 workspace/playbook/。已存在则不动
    （agent 可能已自行修改）；返回是否新写入。"""
    d = workspace / "playbook"
    p = d / "对端发现与联系.md"
    if p.exists():
        return False
    try:
        d.mkdir(parents=True, exist_ok=True)
        p.write_text(PEER_FIND_MD, encoding="utf-8")
        return True
    except Exception:
        return False


def manifest_paths(agent: dict) -> list[Path]:
    """清单候选路径（顺序即优先级：后面的覆盖前面）。"""
    home = get_config().instance_home(agent["id"])
    return [home / "data" / "services.json",
            home / "workspace" / "data" / "services.json"]


def _parse_manifest(p: Path) -> tuple[list[dict], list[str]]:
    """读一个清单文件。返回 (原始条目列表, 错误信息列表)。缺失视为空清单。"""
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    except Exception as e:
        return [], [f"{p.name}（{p.parent.name}/…）不可读：{type(e).__name__}"]
    try:
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise ValueError("顶层必须是数组")
    except Exception as e:
        return [], [f"services.json 解析失败（{p.parent.name}/{p.name}）：{e}"]
    out: list[dict] = []
    errs: list[str] = []
    for i, e in enumerate(entries, 1):
        if not isinstance(e, dict):
            errs.append(f"{p.parent.name}/{p.name} 第 {i} 条：不是对象，跳过")
            continue
        out.append(e)
    return out, errs


def validate_entry(raw: dict) -> tuple[dict | None, str | None]:
    """normalize + 校验单条。通过返回脱敏条目（不含 token 内容，只有 auth 布尔），
    不通过返回 (None, 原因)。未知字段（含历史的 readonly）忽略。"""
    name = str(raw.get("name", "")).strip()
    prob = _name_problem(name)
    if prob:
        return None, f"{name or '(空)'}：name {prob}"
    try:
        port = int(raw.get("port", 0))
    except (TypeError, ValueError):
        return None, f"{name}：port 不是整数"
    if not (1 <= port <= 65535):
        return None, f"{name}：port 越界（1-65535）"

    base_path = str(raw.get("base_path", "") or "").strip()
    if base_path and not base_path.startswith("/"):
        base_path = "/" + base_path
    base_path = base_path.rstrip("/")

    token_file = str(raw.get("token_file", "") or "").strip()
    if token_file and (token_file.startswith("/") or ".." in Path(token_file).parts):
        return None, f"{name}：token_file 须为相对 agent home 的路径（禁绝对路径/..）"

    openapi = raw.get("openapi", "/openapi.json")
    openapi = openapi if openapi is False else (str(openapi) or "/openapi.json")

    return {
        "name": name,
        "port": port,
        "title": str(raw.get("title", "") or "") or name,
        "base_path": base_path,
        "openapi": openapi,
        "probe": str(raw.get("probe", "/") or "/"),
        "token_file": token_file or None,
        "note": str(raw.get("note", "") or ""),
    }, None


def merge_services(agent: dict) -> tuple[list[dict], list[str]]:
    """读 agent 自声明清单（两个候选路径，workspace 侧同名覆盖）→ (按名排序, errors)。"""
    out: dict[str, dict] = {}
    errors: list[str] = []
    for p in manifest_paths(agent):
        entries, errs = _parse_manifest(p)
        errors.extend(errs)
        for e in entries:
            norm, err = validate_entry(e)
            if norm is None:
                errors.append(f"{p.parent.name}/{p.name}：{err}")
                continue
            out[norm["name"]] = norm          # workspace 侧后读，同名覆盖
    return sorted(out.values(), key=lambda s: s["name"]), errors


def find_service(agent: dict, name: str) -> dict | None:
    """按名定位服务（清单 + 自动发现条目；反代路由用）。"""
    for s in merged_with_auto(agent):
        if s["name"] == name:
            return s
    return None


# ── token 定位与读取：声明优先，未声明则按候选搜（位置不一致的兜底）────

def read_token_file(p: Path) -> str | None:
    """读 token 文件。.json 解析 {"tokens":[{"token":…}]} 取首个启用的
    （role/name 含 admin 优先）；其余按纯文本（strip 后取首行）。坏/空 → None。"""
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return None
    if p.suffix == ".json":
        try:
            data = json.loads(raw)
            toks = [t for t in data.get("tokens", []) if isinstance(t, dict)
                    and t.get("enabled", True) and t.get("token")]
        except Exception:
            return None
        if not toks:
            return None
        best = next((t for t in toks
                     if t.get("role") == "admin" or "admin" in str(t.get("name", ""))),
                    toks[0])
        return str(best["token"]).strip() or None
    tok = raw.strip().splitlines()
    return tok[0].strip() if tok else None


def resolve_token_file(agent: dict, svc: dict) -> tuple[str | None, str]:
    """定位服务 token 文件 → (相对 home 路径, 来源)。token_file 声明优先；
    未声明按候选搜（纯文件系统操作、无网络，反代热路径每次实时做，文件出现即生效）。"""
    home = get_config().instance_home(agent["id"])
    if svc.get("token_file"):
        return svc["token_file"], "manifest"
    for rel in _TOKEN_FILE_CANDIDATES:
        if read_token_file(home / rel) is not None:
            return rel, "auto"
    return None, "none"


def service_token(agent: dict, svc: dict) -> str | None:
    """实时读服务 token（文件位置由 resolve_token_file 定位——声明或自动搜索；
    内容每次实时读，agent 轮换 token / 换文件位置自动跟随）。空/不可读 → None。"""
    rel, _src = resolve_token_file(agent, svc)
    if not rel:
        return None
    return read_token_file(get_config().instance_home(agent["id"]) / rel)


# ── 端口发现：agent 单元（cgroup）内进程的监听端口（完全未声明的服务兜底）──

def _unit_procs(agent_id: str) -> list[int]:
    """agent 瞬态单元 xusi-a-<id>.service 的全部 PID（含 agent 派生的子进程，
    如 run_shell 拉起的自建服务）。用户级单元挂在 user@<uid>.service 之下
    （可能在 app.slice 等子 slice 里），按 uid 定位后浅层查找；任何失败 → []
    （降级为仅清单，绝不报错）。"""
    uid = os.getuid()
    unit = f"xusi-a-{agent_id}.service"
    base = Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service")
    procs_file = None
    for cand in (base / unit, *base.glob(f"*/{unit}"), *base.glob(f"*/*/{unit}")):
        if (cand / "cgroup.procs").is_file():
            procs_file = cand / "cgroup.procs"
            break
    if procs_file is None:
        return []
    try:
        return [int(x) for x in procs_file.read_text().split()]
    except Exception:
        return []


def _listen_ports(pids: list[int]) -> set[int]:
    """/proc/net/tcp(|6) 里处于 LISTEN 且 socket inode 归属这些 PID 的端口。"""
    inodes: set[int] = set()
    for pid in pids:
        try:
            fds = os.scandir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd.path)
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(int(target[8:-1]))
    ports: set[int] = set()
    for tbl in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(tbl).read_text().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            f = ln.split()
            if len(f) < 10 or f[3] != "0A":                  # 0A = TCP_LISTEN
                continue
            if int(f[9]) in inodes:
                ports.add(int(f[1].split(":")[1], 16))
    return ports


def agent_extra_ports(agent: dict) -> set[int]:
    """agent 进程监听的、观察台端口之外的端口（= 自建服务候选）。纯 /proc 读。"""
    return _listen_ports(_unit_procs(agent["id"])) - {agent["port"]}


# ── openapi 定位：声明优先，失效/未声明按候选探测（路径不一致的兜底）──

def _http_get(url: str, token: str | None = None, timeout: float = 1.0):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    try:
        return httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except Exception:
        return None


def find_openapi(agent: dict, svc: dict) -> tuple[str | None, str]:
    """定位可用的 OpenAPI 自描述 → (路径, 来源)。agent 显式声明 false → (None, "none")；
    声明路径先验证，404/坏内容则按候选探测（带服务 token）；全不中 → (None, "probed-none")。
    只在列表/发现路径调用；结果缓存 _PROBE_TTL 秒，刷新不反复打服务。"""
    if svc.get("openapi") is False:
        return None, "none"
    key = ("openapi", agent["id"], svc["port"], svc.get("base_path", ""),
           str(svc.get("openapi")))
    now = time.monotonic()
    hit = _probe_cache.get(key)
    if hit and now - hit[0] < _PROBE_TTL:
        return hit[1]["path"], hit[1]["source"]

    tok = service_token(agent, svc)
    base = f"http://127.0.0.1:{svc['port']}{svc.get('base_path', '')}"
    declared = str(svc.get("openapi") or "/openapi.json")
    found, source = None, "probed-none"
    for cand in [declared] + [c for c in _OPENAPI_CANDIDATES if c != declared]:
        r = _http_get(base + cand, token=tok)
        if r is None:                       # 连不上（服务停了），再探无意义
            break
        if r.status_code != 200:
            continue
        try:
            spec = json.loads(r.text)
        except Exception:
            continue
        if isinstance(spec, dict) and isinstance(spec.get("paths"), dict):
            found = cand
            source = "manifest" if cand == declared else "probed"
            break
    _probe_cache[key] = (now, {"path": found, "source": source})
    return found, source


# ── 自动发现条目 + 合并 ───────────────────────────────────────────────

def auto_services(agent: dict, *, http_check: bool = True) -> list[dict]:
    """完全未声明的监听端口 → 候补服务条目（auto-{port}）。http_check 时并发探测
    全部端口（探测池 submit + wait(2.0) 总超时硬墙，避开 httpx「对端 accept 后
    不响应」的死等）；反代路由路径省略探测（省网络开销，服务死活由转发结果自然
    反映）。agent 日后在清单声明同端口即被清单条目接管。"""
    ports = sorted(agent_extra_ports(agent))
    out: list[dict] = []
    if not http_check or not ports:
        return [{"name": f"auto-{p}", "port": p,
                 "title": f"自动发现 :{p}", "base_path": "",
                 "openapi": None, "probe": "/", "token_file": None,
                 "note": "管理面自动发现（agent 未在 services.json 声明）"}
                for p in ports]
    # 所有探测一次提交到池；wait(2.0) 是总超时硬墙（4 worker 全卡也 2s 返回）
    futs = {p: _AUTO_PROBE_POOL.submit(_probe_alive, f"http://127.0.0.1:{p}/", 0.8)
            for p in ports}
    concurrent.futures.wait(futs.values(), timeout=2.0)
    for port, fut in futs.items():
        try:
            if not fut.done():                              # 超时未完成 → 保守放弃
                continue
            ok, _status, _ms, _note = fut.result()
        except Exception:
            continue
        if not ok:
            continue
        out.append({"name": f"auto-{port}", "port": port,
                    "title": f"自动发现 :{port}", "base_path": "",
                    "openapi": None, "probe": "/", "token_file": None,
                    "note": "管理面自动发现（agent 未在 services.json 声明）"})
    return out


def merged_with_auto(agent: dict) -> list[dict]:
    """清单 + 自动发现（端口去重：清单声明的端口吃掉 auto 条目——agent 声明优先）。"""
    svcs, _errs = merge_services(agent)
    declared_ports = {s["port"] for s in svcs}
    return svcs + [s for s in auto_services(agent, http_check=False)
                   if s["port"] not in declared_ports]


def probe_service(svc: dict) -> dict:
    """探活：socket 直连 127.0.0.1:port{base_path}{probe}。ok = 连上且 status<500
    （401/404 也算活着）。单次调用走 _probe_alive，不走池（避免占用探测 worker）。"""
    url = f"http://127.0.0.1:{svc['port']}{svc.get('base_path', '')}{svc.get('probe', '/')}"
    t0 = time.monotonic()
    try:
        ok, status, ms, note = _probe_alive(url, 0.8)
        return {"ok": ok, "status": status,
                "ms": ms if ms is not None else int((time.monotonic() - t0) * 1000),
                "note": note}
    except Exception as e:
        return {"ok": False, "status": None, "ms": None, "note": type(e).__name__}


def public_access_text(agent: dict, svc: dict) -> str | None:
    """一句话对外接入文案：admin 复制即可贴给其他 agent / 用户。
    token 取最新一枚反代 api token（外部调用方拿它过 /svc 鉴权）；
    自动发现服务 / public_url 未配 / 无 api token → None（前端静默不渲染）。"""
    if svc.get("auto"):
        return None
    pub = get_config().public_url.rstrip("/")
    if not pub:
        return None
    tok = apitokens.latest_token()
    if not tok:
        return None
    name = (agent.get("name") or agent["id"]).strip()
    title = (svc.get("title") or svc["name"]).strip()
    op = svc.get("openapi_found") or svc.get("openapi")
    if op is None or op is False:
        op = "/openapi.json"
    if op and not op.startswith("/"):
        op = "/" + op
    url = f"{pub}/svc/{agent['id']}/{svc['name']}{(svc.get('base_path') or '')}{op}"
    return f"{name} 提供{title}服务，请访问 `{url}` 获得， token = {tok}"


def list_services(agent: dict, *, probe: bool = True) -> dict:
    """发现聚合（API 端点用）：清单 + 自动发现 + token/openapi 动态解析标记
    + 端口池警告 + 可选探活。"""
    cfg = get_config()
    svcs, errors = merge_services(agent)
    declared_ports = {s["port"] for s in svcs}
    out = []
    for s in svcs + [x for x in auto_services(agent) if x["port"] not in declared_ports]:
        row = dict(s)
        row["auto"] = s["name"].startswith("auto-")
        _rel, tok_src = resolve_token_file(agent, s)
        row["auth"] = service_token(agent, s) is not None
        row["token_source"] = tok_src
        row["openapi_found"], row["openapi_source"] = find_openapi(agent, s)
        row["public_access"] = public_access_text(agent, s)
        if cfg.port_lo <= s["port"] <= cfg.port_hi:
            row["warn"] = f"端口 {s['port']} 在管理面分配池 [{cfg.port_lo},{cfg.port_hi}] 内，可能与新 agent 冲突（建议 8700-8799）"
        if probe:
            row["health"] = probe_service(s)
        out.append(row)
    return {"id": agent["id"], "services": out, "errors": errors}


def service_names(agent: dict) -> list[str]:
    return [s["name"] for s in merged_with_auto(agent)]
